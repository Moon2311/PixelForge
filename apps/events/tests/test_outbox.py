"""Transactional outbox: rows commit and roll back with the business change;
inline mode processes them after commit."""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.events import inline, outbox, topics
from apps.events.models import AuditEvent, OutboxEvent, ProcessedEvent
from apps.events.retry import NonRetryableError

from .helpers import Flaky, TaskRegistrationMixin


class EnqueueTest(TestCase):
    def test_row_holds_the_envelope(self):
        row = outbox.enqueue("order.placed", topic=topics.ORDER_EVENTS, aggregate_type="order",
                             aggregate_id="PF-1", payload={"total": Decimal("10.50")})
        self.assertEqual(row.status, OutboxEvent.STATUS_PENDING)
        self.assertEqual(row.topic, "order-events")
        self.assertEqual(row.key, "PF-1")
        self.assertEqual(row.payload["event_type"], "order.placed")
        self.assertEqual(row.payload["payload"], {"total": "10.50"})
        self.assertEqual(str(row.event_id), row.payload["event_id"])

    def test_event_id_is_unique_for_events_but_not_forwards(self):
        row = outbox.enqueue("order.placed", topic=topics.ORDER_EVENTS, aggregate_type="order",
                             aggregate_id="PF-1", payload={})
        OutboxEvent.objects.create(event_id=row.event_id, kind=OutboxEvent.KIND_FORWARD, event_type="x",
                                   topic="retry-events", payload={})
        with self.assertRaises(IntegrityError), transaction.atomic():
            OutboxEvent.objects.create(event_id=row.event_id, kind=OutboxEvent.KIND_EVENT, event_type="x",
                                       topic="order-events", payload={})


class OutboxTransactionTest(TransactionTestCase):
    """Real commits and rollbacks (no test transaction around them)."""

    def test_rollback_leaves_no_publishable_event(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            outbox.enqueue("order.placed", topic=topics.ORDER_EVENTS, aggregate_type="order",
                           aggregate_id="PF-1", payload={})
            raise RuntimeError("checkout failed after the event was written")
        self.assertFalse(OutboxEvent.objects.exists())

    @override_settings(EVENTS_BACKEND="kafka", KAFKA_BOOTSTRAP_SERVERS="localhost:1")
    def test_commit_makes_it_publishable(self):
        with transaction.atomic():
            outbox.enqueue("order.placed", topic=topics.ORDER_EVENTS, aggregate_type="order",
                           aggregate_id="PF-1", payload={})
            # Not visible to the publisher before the commit.
            self.assertEqual(OutboxEvent.objects.count(), 1)
        row = OutboxEvent.objects.get()
        self.assertEqual(row.status, OutboxEvent.STATUS_PENDING)  # kafka mode: waits for the publisher


class InlineDispatchTest(TaskRegistrationMixin, TestCase):
    def enqueue(self, event_type="test.inline"):
        return outbox.enqueue(event_type, topic=topics.NOTIFICATION_EVENTS, aggregate_type="test",
                              aggregate_id="1", payload={"n": 1})

    def test_runs_tasks_after_commit_and_marks_published(self):
        calls = []
        self.register("notifications", "test.inline", lambda event: calls.append(event.payload))
        with self.captureOnCommitCallbacks(execute=True):
            row = self.enqueue()
            self.assertEqual(calls, [])  # nothing before the commit
        self.assertEqual(calls, [{"n": 1}])
        row.refresh_from_db()
        self.assertEqual(row.status, OutboxEvent.STATUS_PUBLISHED)
        self.assertTrue(ProcessedEvent.objects.filter(event_id=row.event_id, event_type="test.inline").exists())

    def test_failure_keeps_the_row_pending_with_backoff(self):
        task = Flaky(failures=1)
        self.register("notifications", "test.inline", task)
        with self.captureOnCommitCallbacks(execute=True):
            row = self.enqueue()
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts), (OutboxEvent.STATUS_PENDING, 1))
        self.assertGreater(row.available_at, timezone.now() - timedelta(seconds=1))
        self.assertIn("ConnectionError", row.last_error)
        # The publisher (inline mode) retries it later: now it succeeds.
        OutboxEvent.objects.filter(pk=row.pk).update(available_at=timezone.now())
        inline.dispatch(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, OutboxEvent.STATUS_PUBLISHED)
        self.assertEqual(len(task.calls), 2)

    def test_non_retryable_failure_marks_failed(self):
        self.register("notifications", "test.inline", Flaky(failures=9, error=NonRetryableError("bad data")))
        with self.captureOnCommitCallbacks(execute=True):
            row = self.enqueue()
        row.refresh_from_db()
        self.assertEqual(row.status, OutboxEvent.STATUS_FAILED)

    def test_dispatch_twice_runs_the_task_once(self):
        calls = []
        self.register("notifications", "test.inline", lambda event: calls.append(1))
        row = self.enqueue()
        inline.dispatch(row.pk)
        OutboxEvent.objects.filter(pk=row.pk).update(status=OutboxEvent.STATUS_PENDING)  # e.g. requeued
        inline.dispatch(row.pk)
        self.assertEqual(calls, [1])


class AuditTest(TestCase):
    def test_inline_audit_is_recorded_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            outbox.audit("audit.product_updated", aggregate_type="product", aggregate_id=7, actor_user_id=3,
                         data={"field": "price"})
        record = AuditEvent.objects.get()
        self.assertEqual((record.event_type, record.aggregate_id, record.actor_user_id),
                         ("audit.product_updated", "7", 3))
        self.assertEqual(record.data, {"field": "price"})

    def test_audit_is_idempotent(self):
        from apps.events.envelope import Event, new_event

        event = Event.from_message(new_event("audit.x", "user", 1, {}, event_id=uuid.uuid4()))
        outbox.record_audit(event)
        outbox.record_audit(event)
        self.assertEqual(AuditEvent.objects.count(), 1)
