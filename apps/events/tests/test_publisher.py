"""Outbox publisher with a fake producer: acknowledgement, transient and
permanent failures, leases."""

from datetime import timedelta

from confluent_kafka import KafkaError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.events import outbox, topics
from apps.events.models import OutboxEvent
from apps.events.publisher import OutboxPublisher

from .helpers import FakeProducer

KAFKA = {"EVENTS_BACKEND": "kafka", "KAFKA_BOOTSTRAP_SERVERS": "localhost:1", "OUTBOX_RETRY_BASE_DELAY": 1.0}


@override_settings(**KAFKA)
class PublisherTest(TestCase):
    def enqueue(self, n=1, aggregate_id="PF-1"):
        return [
            outbox.enqueue("order.placed", topic=topics.ORDER_EVENTS, aggregate_type="order",
                           aggregate_id=aggregate_id, payload={"i": i})
            for i in range(n)
        ]

    def test_marks_rows_published_after_acknowledgement(self):
        rows = self.enqueue(3)
        producer = FakeProducer()
        result = OutboxPublisher(producer=producer).publish_once()
        self.assertEqual(result, {"claimed": 3, "published": 3, "failed": 0})
        self.assertEqual([m["value"]["payload"]["i"] for m in producer.sent], [0, 1, 2])  # creation order
        self.assertEqual(producer.sent[0]["key"], "PF-1")
        self.assertEqual(producer.sent[0]["headers"]["event_type"], b"order.placed")
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.status, OutboxEvent.STATUS_PUBLISHED)
            self.assertIsNotNone(row.published_at)
            self.assertIsNotNone(row.kafka_offset)

    def test_transient_failure_keeps_rows_pending_with_backoff(self):
        (row,) = self.enqueue()
        result = OutboxPublisher(producer=FakeProducer(fail=KafkaError._TRANSPORT)).publish_once()
        self.assertEqual(result["failed"], 1)
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts), (OutboxEvent.STATUS_PENDING, 1))
        self.assertGreater(row.available_at, timezone.now())
        self.assertIsNone(row.locked_until)
        # Not due yet: the next batch skips it.
        self.assertEqual(OutboxPublisher(producer=FakeProducer()).publish_once()["claimed"], 0)
        # Once due, a healthy broker takes it.
        OutboxEvent.objects.filter(pk=row.pk).update(available_at=timezone.now())
        self.assertEqual(OutboxPublisher(producer=FakeProducer()).publish_once()["published"], 1)

    def test_unconfirmed_delivery_is_a_failure(self):
        (row,) = self.enqueue()
        OutboxPublisher(producer=FakeProducer(silent=True)).publish_once()
        row.refresh_from_db()
        self.assertEqual(row.status, OutboxEvent.STATUS_PENDING)
        self.assertIn("not confirmed", row.last_error)

    def test_permanent_failure_marks_failed(self):
        (row,) = self.enqueue()
        OutboxPublisher(producer=FakeProducer(fail=KafkaError.MSG_SIZE_TOO_LARGE)).publish_once()
        row.refresh_from_db()
        self.assertEqual(row.status, OutboxEvent.STATUS_FAILED)

    def test_claimed_rows_are_skipped_until_the_lease_expires(self):
        (row,) = self.enqueue()
        OutboxEvent.objects.filter(pk=row.pk).update(locked_until=timezone.now() + timedelta(seconds=60),
                                                      locked_by="other")
        self.assertEqual(OutboxPublisher(producer=FakeProducer()).publish_once()["claimed"], 0)
        # The other publisher died: its lease runs out and the row is recovered.
        OutboxEvent.objects.filter(pk=row.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(OutboxPublisher(producer=FakeProducer()).publish_once()["published"], 1)

    def test_backoff_grows_with_attempts(self):
        from apps.events.publisher import backoff

        self.assertLess(max(backoff(1) for _ in range(20)), min(backoff(4) for _ in range(20)))
