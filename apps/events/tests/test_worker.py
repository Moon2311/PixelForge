"""Worker message handling with fake Kafka clients: commits, retries, DLQ,
fallbacks, deferral, duplicates, invalid messages."""

import uuid
from datetime import timedelta
from unittest import mock

from confluent_kafka import KafkaError
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.events import envelope, topics
from apps.events.models import OutboxEvent, ProcessedEvent
from apps.events.retry import NonRetryableError
from apps.events.worker import Worker

from .helpers import (
    FakeConsumer,
    FakeProducer,
    Flaky,
    TaskRegistrationMixin,
    event_message,
    kafka_message,
)

SETTINGS = {
    "EVENTS_BACKEND": "kafka", "KAFKA_BOOTSTRAP_SERVERS": "localhost:1",
    "EVENTS_RETRY_MAX_ATTEMPTS": 3, "EVENTS_RETRY_BASE_DELAY": 1.0, "EVENTS_RETRY_MULTIPLIER": 2.0,
    "EVENTS_RETRY_JITTER": 0.0, "EVENTS_RETRY_MAX_DELAY": 60.0, "EVENTS_TASK_TIMEOUT": 5.0,
}


@override_settings(**SETTINGS)
class WorkerTest(TaskRegistrationMixin, TestCase):
    def setUp(self):
        self.consumer, self.producer = FakeConsumer(), FakeProducer()
        self.worker = Worker("notifications", consumer=self.consumer, producer=self.producer)

    def handle(self, message, **kwargs):
        msg = kafka_message(message, **kwargs)
        self.worker.handle(msg)
        return msg

    def test_subscribes_to_its_topics_and_the_retry_topic(self):
        self.assertEqual(self.worker.topics, ["order-events", "payment-events", "inventory-events",
                                              "notification-events", "retry-events"])
        self.assertEqual(self.worker.group_id, "pixelforge-notifications")

    def test_success_commits_after_recording_the_event(self):
        seen = []
        self.register("notifications", "test.ok", lambda e: seen.append(e.event_id))
        message = event_message("test.ok")
        self.handle(message, offset=7)
        self.assertEqual(seen, [message["event_id"]])
        self.assertEqual(self.consumer.commits, [("order-events", 0, 7)])
        record = ProcessedEvent.objects.get()
        self.assertEqual((record.consumer_group, record.offset), ("pixelforge-notifications", 7))

    def test_duplicate_delivery_runs_the_task_once(self):
        task = Flaky(failures=0)
        self.register("notifications", "test.ok", task)
        message = event_message("test.ok")
        self.handle(message, offset=1)
        self.handle(message, offset=2)  # published twice, or redelivered after a crash
        self.assertEqual(len(task.calls), 1)
        self.assertEqual(len(self.consumer.commits), 2)

    def test_unhandled_event_type_is_acknowledged(self):
        self.handle(event_message("nobody.cares"))
        self.assertEqual(len(self.consumer.commits), 1)
        self.assertFalse(self.producer.sent)

    def test_retryable_failure_goes_to_the_retry_topic(self):
        self.register("notifications", "test.flaky", Flaky(failures=9))
        message = event_message("test.flaky", aggregate_id="PF-9", correlation_id="req-123")
        before = timezone.now()
        self.handle(message, offset=4, partition=2)
        (retry,) = self.producer.on(topics.RETRY_EVENTS)
        failure = retry["failure"]
        self.assertEqual(retry["message_type"], "retry")
        self.assertEqual(retry["event"], message)  # the original event, untouched
        self.assertEqual(failure["target_group"], "notifications")
        self.assertEqual(failure["task"], "test.flaky")
        self.assertEqual(failure["entity_id"], "PF-9")
        self.assertEqual(failure["attempt"], 1)
        self.assertEqual((failure["original_topic"], failure["original_partition"], failure["original_offset"]),
                         ("order-events", 2, 4))
        self.assertEqual(failure["error_category"], "transient")
        self.assertEqual(failure["error_type"], "ConnectionError")
        self.assertEqual(failure["correlation_id"], "req-123")
        next_retry = envelope.parse_time(failure["next_retry_at"])
        self.assertAlmostEqual((next_retry - before).total_seconds(), 1.0, delta=0.5)  # base delay
        self.assertEqual(failure["first_failed_at"], failure["last_failed_at"])
        # Committed only after the retry message was acknowledged.
        self.assertEqual(self.consumer.commits, [("order-events", 2, 4)])

    def _retry_of(self, message, attempt, due=None, group="notifications", first="2026-01-01T00:00:00+00:00"):
        failure = {"target_group": group, "attempt": attempt, "original_topic": "order-events",
                   "original_partition": 0, "original_offset": 4, "first_failed_at": first,
                   "next_retry_at": (due or timezone.now() - timedelta(seconds=1)).isoformat(),
                   "replay_count": 0, "correlation_id": ""}
        return envelope.failure_message(envelope.RETRY, message, failure)

    def test_due_retry_runs_with_the_next_attempt_and_backs_off_exponentially(self):
        task = Flaky(failures=9)
        self.register("notifications", "test.flaky", task)
        message = event_message("test.flaky")
        before = timezone.now()
        self.handle(self._retry_of(message, attempt=1), logical_topic=topics.RETRY_EVENTS)
        self.assertEqual(task.calls, [2])
        (retry,) = self.producer.on(topics.RETRY_EVENTS)
        self.assertEqual(retry["failure"]["attempt"], 2)
        self.assertEqual(retry["failure"]["first_failed_at"], "2026-01-01T00:00:00+00:00")  # kept
        self.assertEqual(retry["failure"]["original_offset"], 4)  # the first delivery's position
        delay = (envelope.parse_time(retry["failure"]["next_retry_at"]) - before).total_seconds()
        self.assertAlmostEqual(delay, 2.0, delta=0.5)  # 1s, 2s, 4s, ...

    def test_exhausted_retries_go_to_the_dlq(self):
        self.register("notifications", "test.flaky", Flaky(failures=9))
        message = event_message("test.flaky")
        msg = self.handle(self._retry_of(message, attempt=2), logical_topic=topics.RETRY_EVENTS, offset=11)
        self.assertFalse(self.producer.on(topics.RETRY_EVENTS))
        (dead,) = self.producer.on(topics.DEAD_LETTER_EVENTS)
        self.assertEqual(dead["message_type"], "dead_letter")
        self.assertEqual(dead["event"], message)
        self.assertEqual(dead["failure"]["reason"], "retries_exhausted")
        self.assertEqual(dead["failure"]["attempt"], 3)
        self.assertNotIn("next_retry_at", dead["failure"])
        self.assertEqual(self.consumer.commits, [(msg.topic(), 0, 11)])

    def test_non_retryable_failure_goes_straight_to_the_dlq(self):
        self.register("notifications", "test.bad", Flaky(failures=9, error=NonRetryableError("order missing")))
        self.handle(event_message("test.bad"))
        (dead,) = self.producer.on(topics.DEAD_LETTER_EVENTS)
        self.assertEqual((dead["failure"]["reason"], dead["failure"]["attempt"]), ("non_retryable", 1))
        self.assertEqual(dead["failure"]["error_category"], "permanent")

    def test_task_specific_policy(self):
        self.register("notifications", "test.flaky", Flaky(failures=9), max_attempts=1)
        self.handle(event_message("test.flaky"))
        self.assertEqual(len(self.producer.on(topics.DEAD_LETTER_EVENTS)), 1)

    def test_timeout_is_retried(self):
        import time

        self.register("notifications", "test.slow", lambda e: time.sleep(3), timeout=0.2)
        self.handle(event_message("test.slow"))
        (retry,) = self.producer.on(topics.RETRY_EVENTS)
        self.assertEqual((retry["failure"]["error_category"], retry["failure"]["error_type"]),
                         ("timeout", "TaskTimeout"))

    def test_retry_not_due_pauses_the_partition_without_committing(self):
        task = Flaky(failures=0)
        self.register("notifications", "test.ok", task)
        due = timezone.now() + timedelta(seconds=30)
        self.handle(self._retry_of(event_message("test.ok"), attempt=1, due=due),
                    logical_topic=topics.RETRY_EVENTS, offset=5, partition=1)
        self.assertEqual(task.calls, [])
        self.assertEqual(self.consumer.commits, [])
        self.assertEqual(self.consumer.paused, [("retry-events", 1)])
        self.assertEqual(self.consumer.seeks, [("retry-events", 1, 5)])  # read again when due
        # Resumed once due (no sleeping: the poll loop checks the schedule).
        self.worker._paused[("retry-events", 1)] = 0
        self.worker._resume_due()
        self.assertEqual(self.consumer.resumed, [("retry-events", 1)])

    def test_another_groups_retry_is_skipped(self):
        task = Flaky(failures=0)
        self.register("notifications", "test.ok", task)
        self.handle(self._retry_of(event_message("test.ok"), attempt=1, group="images"),
                    logical_topic=topics.RETRY_EVENTS)
        self.assertEqual(task.calls, [])
        self.assertEqual(len(self.consumer.commits), 1)

    def test_kafka_down_falls_back_to_the_outbox(self):
        self.worker.producer = FakeProducer(fail=KafkaError._TRANSPORT)
        self.register("notifications", "test.flaky", Flaky(failures=9))
        message = event_message("test.flaky")
        self.handle(message, offset=3)
        forward = OutboxEvent.objects.get(kind=OutboxEvent.KIND_FORWARD)
        self.assertEqual((forward.topic, str(forward.event_id)), ("retry-events", message["event_id"]))
        self.assertEqual(forward.payload["failure"]["attempt"], 1)
        self.assertEqual(self.consumer.commits, [("order-events", 0, 3)])  # durable in PostgreSQL

    def test_dlq_publish_failure_falls_back_to_the_outbox(self):
        self.worker.producer = FakeProducer(fail=KafkaError._MSG_TIMED_OUT)
        self.register("notifications", "test.bad", Flaky(failures=9, error=NonRetryableError("x")))
        self.handle(event_message("test.bad"))
        forward = OutboxEvent.objects.get(kind=OutboxEvent.KIND_FORWARD)
        self.assertEqual(forward.topic, "dead-letter-events")
        self.assertEqual(len(self.consumer.commits), 1)

    def test_nowhere_to_record_the_failure_blocks_and_redelivers(self):
        self.worker.producer = FakeProducer(fail=KafkaError._TRANSPORT)
        self.register("notifications", "test.flaky", Flaky(failures=9))
        with mock.patch("apps.events.worker.OutboxEvent.objects.create", side_effect=OperationalError("db down")):
            self.handle(event_message("test.flaky"), offset=8)
        self.assertEqual(self.consumer.commits, [])  # never acknowledged
        self.assertEqual(self.consumer.seeks, [("order-events", 0, 8)])
        self.assertEqual(self.consumer.paused, [("order-events", 0)])
        self.assertFalse(OutboxEvent.objects.exists())

    def test_invalid_message_is_dead_lettered(self):
        from .helpers import FakeMessage

        self.worker.handle(FakeMessage("order-events", b"{not json", 0, 12))
        (dead,) = self.producer.on(topics.DEAD_LETTER_EVENTS)
        self.assertEqual(dead["failure"]["reason"], "invalid_message")
        self.assertEqual(dead["event"]["payload"]["raw_preview"], "{not json")
        self.assertEqual(self.consumer.commits, [("order-events", 0, 12)])

    def test_db_error_while_checking_duplicates_is_retried(self):
        self.register("notifications", "test.ok", Flaky(failures=0))
        with mock.patch("apps.events.processing.ProcessedEvent.objects.filter",
                        side_effect=OperationalError("db down")):
            self.handle(event_message("test.ok"))
        self.assertEqual(len(self.producer.on(topics.RETRY_EVENTS)), 1)


@override_settings(**SETTINGS)
class DlqArchiverWorkerTest(TaskRegistrationMixin, TestCase):
    def setUp(self):
        self.consumer, self.producer = FakeConsumer(), FakeProducer()
        self.worker = Worker("dlq-archiver", consumer=self.consumer, producer=self.producer)

    def dead_letter(self, event):
        failure = {"target_group": "notifications", "task": "t", "attempt": 3, "reason": "retries_exhausted",
                   "error_category": "transient", "error_message": "ConnectionError: x",
                   "original_topic": "order-events", "original_partition": 0, "original_offset": 1,
                   "first_failed_at": timezone.now().isoformat(), "last_failed_at": timezone.now().isoformat()}
        return envelope.failure_message(envelope.DEAD_LETTER, event, failure)

    def test_archives_each_dead_letter_message_once(self):
        from apps.events.models import DeadLetterEvent

        letter = self.dead_letter(event_message("test.x"))
        msg = kafka_message(letter, logical_topic=topics.DEAD_LETTER_EVENTS)
        self.worker.handle(msg)
        self.worker.handle(msg)  # redelivered
        row = DeadLetterEvent.objects.get()
        self.assertEqual((row.consumer_group, row.reason, row.attempts), ("notifications", "retries_exhausted", 3))
        self.assertEqual(len(self.consumer.commits), 2)

    def test_archive_failure_never_dead_letters_the_dead_letter(self):
        letter = self.dead_letter(event_message("test.x"))
        with mock.patch("apps.events.dlq.DeadLetterEvent.objects.select_for_update",
                        side_effect=OperationalError("db down")):
            self.worker.handle(kafka_message(letter, logical_topic=topics.DEAD_LETTER_EVENTS, offset=2))
        self.assertEqual(self.consumer.commits, [])
        self.assertEqual(self.consumer.seeks, [("dead-letter-events", 0, 2)])
        self.assertFalse(self.producer.sent)


class EventIdTest(TestCase):
    def test_malformed_envelopes_are_rejected(self):
        for value in (b"[]", b'{"event_type": "x"}', b'{"event_id": "nope", "event_type": "x"}',
                      b'{"message_type": "retry", "event": {}}'):
            with self.assertRaises(envelope.InvalidMessage):
                envelope.loads(value)
        envelope.loads(envelope.dumps(envelope.new_event("x", "t", 1, {}, event_id=uuid.uuid4())))
