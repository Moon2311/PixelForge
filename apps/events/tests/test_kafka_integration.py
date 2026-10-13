"""Integration tests against a real Kafka broker (no mocks of the client).

They run when a broker is reachable at KAFKA_TEST_BOOTSTRAP_SERVERS
(default localhost:9094: the Compose broker's host listener) and are
skipped otherwise. Each test creates its own topics and consumer groups
under a random prefix and deletes the topics afterwards, so the running
Compose workers and other tests are never involved.
"""

import io
import os
import time
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal

from confluent_kafka import ConsumerGroupTopicPartitions, KafkaException, TopicPartition
from confluent_kafka.admin import AdminClient, OffsetSpec
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.events import dlq, envelope, kafka, outbox, registry, topics
from apps.events.models import DeadLetterEvent, OutboxEvent, ProcessedEvent
from apps.events.publisher import OutboxPublisher
from apps.events.worker import Worker

from .helpers import Flaky, TaskRegistrationMixin

BOOTSTRAP = os.environ.get("KAFKA_TEST_BOOTSTRAP_SERVERS", "localhost:9094")


def _broker_available():
    try:
        AdminClient({"bootstrap.servers": BOOTSTRAP, "socket.timeout.ms": 2000}).list_topics(timeout=3)
        return True
    except KafkaException:
        return False


BROKER = _broker_available()


@unittest.skipUnless(BROKER, f"Kafka not reachable at {BOOTSTRAP} (docker compose up kafka)")
class KafkaIntegrationTest(TaskRegistrationMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.prefix = f"it-{uuid.uuid4().hex[:8]}"
        self.settings_override = override_settings(
            EVENTS_BACKEND="kafka",
            KAFKA_BOOTSTRAP_SERVERS=BOOTSTRAP,
            KAFKA_TOPICS={name: f"{self.prefix}.{name}" for name in settings.KAFKA_TOPICS},
            KAFKA_CONSUMER_GROUP_PREFIX=self.prefix,
            KAFKA_TOPIC_PARTITIONS=2,
            KAFKA_SESSION_TIMEOUT_MS=10000,
            EVENTS_RETRY_MAX_ATTEMPTS=3,
            EVENTS_RETRY_BASE_DELAY=0.4,
            EVENTS_RETRY_MULTIPLIER=2.0,
            EVENTS_RETRY_JITTER=0.0,
            EVENTS_RETRY_MAX_DELAY=5.0,
            EVENTS_FORWARD_BACKOFF=0.5,
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        call_command("kafka_create_topics", stdout=io.StringIO())
        self.addCleanup(self._delete_topics)
        self.producer = kafka.producer("it")

    def _delete_topics(self):
        client = kafka.admin()
        futures = client.delete_topics(list(settings.KAFKA_TOPICS.values()), operation_timeout=10)
        for future in futures.values():
            try:
                future.result()
            except KafkaException:
                pass

    # -- helpers -----------------------------------------------------------

    def publish_all(self, publisher=None):
        publisher = publisher or OutboxPublisher(producer=self.producer)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not publisher.publish_once()["claimed"]:
                return
        self.fail("outbox did not drain")

    def run_worker(self, group, until, timeout=30, worker=None):
        worker = worker or Worker(group)
        worker.run(until=until, timeout=timeout)
        self.assertTrue(until(), f"{group} worker stopped before the condition held")
        return worker

    def committed_lag(self, group, logical_topic):
        """Messages of the topic not yet committed by the group."""
        for _ in range(20):
            try:
                return self._committed_lag(group, logical_topic)
            except KafkaException:
                time.sleep(0.25)  # leadership settling right after topic creation
        return self._committed_lag(group, logical_topic)

    def _committed_lag(self, group, logical_topic):
        client = kafka.admin()
        topic = topics.name(logical_topic)
        metadata = client.list_topics(topic, timeout=5)
        tps = [TopicPartition(topic, p) for p in metadata.topics[topic].partitions]
        ends = {tp.partition: f.result().offset
                for tp, f in client.list_offsets({tp: OffsetSpec.latest() for tp in tps}).items()}
        gid = registry.group_id(group)
        result = client.list_consumer_group_offsets([ConsumerGroupTopicPartitions(gid, tps)])[gid].result()
        committed = {tp.partition: max(tp.offset, 0) for tp in result.topic_partitions}
        return sum(ends[p] - committed.get(p, 0) for p in ends)

    def enqueue(self, event_type, aggregate_id="PF-1", logical_topic=topics.NOTIFICATION_EVENTS):
        return outbox.enqueue(event_type, topic=logical_topic, aggregate_type="test",
                              aggregate_id=aggregate_id, payload={"n": 1})

    # -- tests -------------------------------------------------------------

    def test_outbox_to_worker_end_to_end(self):
        calls = []
        self.register("notifications", "it.ok", lambda e: calls.append(e.event_id))
        row = self.enqueue("it.ok")
        self.publish_all()
        row.refresh_from_db()
        self.assertEqual(row.status, OutboxEvent.STATUS_PUBLISHED)
        self.assertIsNotNone(row.kafka_offset)  # acknowledged by the broker
        self.run_worker("notifications", until=lambda: calls)
        self.assertEqual(calls, [str(row.event_id)])
        self.assertTrue(ProcessedEvent.objects.filter(event_id=row.event_id).exists())
        self.assertEqual(self.committed_lag("notifications", topics.NOTIFICATION_EVENTS), 0)

    def test_duplicate_delivery_is_processed_once(self):
        calls = []
        self.register("notifications", "it.ok", lambda e: calls.append(1))
        message = envelope.new_event("it.ok", "test", "PF-1", {})
        for _ in range(3):  # e.g. the publisher died before marking the row published
            kafka.produce_sync(self.producer, topics.name(topics.NOTIFICATION_EVENTS), envelope.dumps(message),
                               key="PF-1")
        self.run_worker("notifications",
                        until=lambda: self.committed_lag("notifications", topics.NOTIFICATION_EVENTS) == 0)
        self.assertEqual(calls, [1])
        self.assertEqual(ProcessedEvent.objects.count(), 1)

    def test_retry_topic_backoff_then_success(self):
        task = Flaky(failures=2)
        stamps = []

        def timed(event):
            stamps.append(time.monotonic())
            task(event)

        self.register("notifications", "it.flaky", timed)
        self.enqueue("it.flaky")
        self.publish_all()
        self.run_worker("notifications", until=lambda: ProcessedEvent.objects.exists())
        self.assertEqual(task.calls, [1, 2, 3])
        # 0.4s then 0.8s (no jitter): delayed through the retry topic, not by sleeping.
        self.assertGreaterEqual(stamps[1] - stamps[0], 0.35)
        self.assertGreaterEqual(stamps[2] - stamps[1], 0.75)
        self.assertEqual(ProcessedEvent.objects.get().attempt, 3)

    def test_exhausted_retries_reach_the_dlq_then_replay(self):
        task = Flaky(failures=99)
        self.register("notifications", "it.broken", task)
        row = self.enqueue("it.broken", aggregate_id="PF-DLQ")
        self.publish_all()
        dlq_topic = topics.DEAD_LETTER_EVENTS
        self.run_worker("notifications", until=lambda: self._topic_size(dlq_topic) >= 1)
        self.assertEqual(task.calls, [1, 2, 3])  # EVENTS_RETRY_MAX_ATTEMPTS
        self.run_worker("dlq-archiver", until=lambda: DeadLetterEvent.objects.exists())
        letter = DeadLetterEvent.objects.get()
        self.assertEqual((str(letter.event_id), letter.consumer_group, letter.reason, letter.attempts),
                         (str(row.event_id), "notifications", "retries_exhausted", 3))
        self.assertEqual(letter.original_topic, topics.name(topics.NOTIFICATION_EVENTS))
        self.assertEqual(letter.aggregate_id, "PF-DLQ")
        self.assertIn("ConnectionError", letter.error_message)

        # The operator fixes the cause and replays it explicitly.
        task.failures = 0
        task.calls.clear()
        dlq.replay(letter.pk, note="fixed")
        self.publish_all()
        self.run_worker("notifications", until=lambda: ProcessedEvent.objects.filter(event_id=row.event_id).exists())
        self.assertEqual(task.calls, [1])  # a fresh set of attempts
        letter.refresh_from_db()
        self.assertEqual((letter.status, letter.replay_count), ("replayed", 1))

    def _topic_size(self, logical_topic):
        try:
            return self._end_offsets(logical_topic)
        except KafkaException:
            return 0  # partition leadership still settling right after topic creation

    def _end_offsets(self, logical_topic):
        client = kafka.admin()
        topic = topics.name(logical_topic)
        metadata = client.list_topics(topic, timeout=5)
        tps = {TopicPartition(topic, p): OffsetSpec.latest() for p in metadata.topics[topic].partitions}
        return sum(f.result().offset for f in client.list_offsets(tps).values())

    def test_restart_redelivers_uncommitted_work_without_repeating_it(self):
        calls = []
        self.register("notifications", "it.ok", lambda e: calls.append(1))
        self.enqueue("it.ok")
        self.publish_all()

        # Worker A processes the event and commits it to PostgreSQL, then
        # "crashes" before acknowledging it to Kafka.
        crashing = Worker("notifications")
        crashing._commit = lambda message: None
        self.run_worker("notifications", until=lambda: calls, worker=crashing)
        self.assertEqual(self.committed_lag("notifications", topics.NOTIFICATION_EVENTS), 1)

        # Worker B (same group) is handed the message again: deduplicated.
        self.run_worker("notifications",
                        until=lambda: self.committed_lag("notifications", topics.NOTIFICATION_EVENTS) == 0)
        self.assertEqual(calls, [1])

    def test_crash_before_processing_redelivers(self):
        calls = []
        self.register("notifications", "it.ok", lambda e: calls.append(1))
        self.enqueue("it.ok")
        self.publish_all()
        dying = Worker("notifications")
        dying.handle = lambda message: None  # received, never processed or committed
        dying.run(max_messages=1, timeout=30)
        self.assertEqual(calls, [])
        self.run_worker("notifications", until=lambda: calls)
        self.assertEqual(calls, [1])

    def test_one_group_spreads_partitions_over_its_workers(self):
        first = kafka.consumer(registry.group_id("images"))
        second = kafka.consumer(registry.group_id("images"))
        topic = topics.name(topics.IMAGE_PROCESSING)
        for consumer in (first, second):
            consumer.subscribe([topic])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            first.poll(0.2)
            second.poll(0.2)
            a, b = first.assignment(), second.assignment()
            if a and b and len(a) + len(b) == 2:
                break
        self.assertEqual(sorted(tp.partition for tp in first.assignment() + second.assignment()), [0, 1])
        first.close()
        second.close()

    def test_publisher_recovers_after_kafka_is_unreachable(self):
        row = self.enqueue("it.ok")
        dead = kafka.producer("it-dead", **{"bootstrap.servers": "127.0.0.1:1", "message.timeout.ms": 1500,
                                            "delivery.timeout.ms": 1500})
        with override_settings(KAFKA_DELIVERY_TIMEOUT_MS=1500):
            OutboxPublisher(producer=dead).publish_once()
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts), (OutboxEvent.STATUS_PENDING, 1))
        self.assertTrue(row.last_error)
        OutboxEvent.objects.filter(pk=row.pk).update(available_at=timezone.now())  # skip the backoff
        self.publish_all()
        row.refresh_from_db()
        self.assertEqual(row.status, OutboxEvent.STATUS_PUBLISHED)

    def test_dlq_publish_failure_is_stored_and_delivered_later(self):
        self.register("notifications", "it.bad", Flaky(failures=99, error=ValueError("bad payload")))
        self.enqueue("it.bad")
        self.publish_all()
        # The worker's own producer can't reach Kafka when it dead-letters.
        broken = kafka.producer("it-dead", **{"bootstrap.servers": "127.0.0.1:1", "message.timeout.ms": 1000})
        worker = Worker("notifications", producer=broken)
        self.run_worker("notifications", worker=worker,
                        until=lambda: OutboxEvent.objects.filter(kind=OutboxEvent.KIND_FORWARD).exists())
        forward = OutboxEvent.objects.get(kind=OutboxEvent.KIND_FORWARD)
        self.assertEqual(forward.topic, topics.name(topics.DEAD_LETTER_EVENTS))
        self.assertEqual(self.committed_lag("notifications", topics.NOTIFICATION_EVENTS), 0)  # not lost
        self.publish_all()  # the publisher delivers the dead letter
        self.run_worker("dlq-archiver", until=lambda: DeadLetterEvent.objects.exists())
        self.assertEqual(DeadLetterEvent.objects.get().reason, "non_retryable")

    def test_verified_payment_sends_a_notification(self):
        from apps.orders.models import Order
        from apps.payments.models import Payment
        from apps.payments.services import apply_result
        from apps.payments.services.base import Verification

        user = User.objects.create_user("buyer", "buyer@example.com", "Buyer-pass-123!")
        address = {"country": "Pakistan", "city": "Lahore"}
        order = Order.objects.create(
            number="PF-INTEG01", user_id=user.id, status=Order.STATUS_AWAITING_PAYMENT, contact="0300",
            delivery_method="ship", shipping_address=address, billing_address=address,
            shipping_method="standard", payment_method=Order.PAYMENT_JAZZCASH,
            subtotal=Decimal("500.00"), total=Decimal("500.00"),
        )
        payment = Payment.objects.create(order=order, payment_method=Payment.METHOD_JAZZCASH, amount=order.total,
                                         expires_at=timezone.now() + timedelta(minutes=30))
        result = Verification(status=Payment.STATUS_PAID, amount=Decimal("500.00"), currency="PKR",
                              provider_transaction_id="T1", raw={})
        apply_result(payment.pk, result, source="inquiry")
        apply_result(payment.pk, result, source="ipn")  # duplicate IPN: no second event
        self.assertEqual(OutboxEvent.objects.filter(event_type="payment.succeeded").count(), 1)
        self.publish_all()
        self.run_worker("notifications", until=lambda: len(mail.outbox) >= 1)
        self.assertEqual(mail.outbox[0].to, ["buyer@example.com"])
        self.assertIn("PF-INTEG01", mail.outbox[0].subject)
