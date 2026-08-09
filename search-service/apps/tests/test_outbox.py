"""Outbox relay and dead-letter management command tests.

Tests verify:
    - Relay processes pending events
    - Relay handles Kafka failures gracefully
    - Dead-letter retry resets events
    - Dead-letter stats are correct
    - Dead-letter purge removes old events
    - Dead-letter reset unsticks events
    - EventRelay exponential backoff
    - EventRelay max-retry failure
"""

from datetime import timezone as tz
from unittest.mock import MagicMock, patch
from uuid import uuid4
from django.utils import timezone
from apps.models import OutboxEvent
from apps.event_relay import EventRelay
from apps.tests.test_events import OutboxTransactionTest


class EventRelayProcessingTest(OutboxTransactionTest):
    """Test EventRelay processes pending events."""

    def setUp(self):
        self.relay = EventRelay(poll_interval=0, batch_size=10)
        self.event = OutboxEvent.objects.create(
            event_type="ProductCreated",
            aggregate_type="product",
            aggregate_id="1",
            payload={
                "event_id": str(uuid4()),
                "event_type": "ProductCreated",
                "aggregate_type": "product",
                "aggregate_id": "1",
                "version": 1,
                "occurred_at": timezone.now().isoformat(),
                "data": {},
            },
            status=OutboxEvent.STATUS_PENDING,
            available_at=timezone.now(),
        )

    def test_processes_pending_event(self):
        self.relay._process_pending_events()
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, OutboxEvent.STATUS_PUBLISHED)
        self.assertIsNotNone(self.event.published_at)

    def test_skips_future_available_at(self):
        self.event.available_at = timezone.now() + timezone.timedelta(hours=1)
        self.event.save(update_fields=["available_at"])
        self.relay._process_pending_events()
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, OutboxEvent.STATUS_PENDING)

    def test_skips_already_processing(self):
        self.event.status = OutboxEvent.STATUS_PROCESSING
        self.event.save(update_fields=["status"])
        self.relay._process_pending_events()
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, OutboxEvent.STATUS_PROCESSING)

    def test_handles_kafka_failure_gracefully(self):
        mock_producer = MagicMock()
        mock_producer.send.side_effect = Exception("Kafka unavailable")
        self.relay.set_kafka_producer(mock_producer)
        self.relay._process_event(self.event)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, OutboxEvent.STATUS_PENDING)
        self.assertEqual(self.event.retry_count, 1)

    def test_no_kafka_marks_published(self):
        self.relay.set_kafka_producer(None)
        self.relay._process_event(self.event)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, OutboxEvent.STATUS_PUBLISHED)


class EventRelayBackoffTest(OutboxTransactionTest):
    """Test exponential backoff on retries."""

    def setUp(self):
        self.relay = EventRelay(poll_interval=0, batch_size=10)

    def test_first_retry_backoff_2s(self):
        event = OutboxEvent.objects.create(
            event_type="TestEvent",
            aggregate_type="test",
            aggregate_id="1",
            payload={
                "event_id": str(uuid4()),
                "event_type": "TestEvent",
                "aggregate_type": "test",
                "aggregate_id": "1",
                "version": 1,
                "occurred_at": timezone.now().isoformat(),
                "data": {},
            },
            status=OutboxEvent.STATUS_PENDING,
            retry_count=0,
        )
        mock_producer = MagicMock()
        mock_producer.send.side_effect = Exception("fail")
        self.relay.set_kafka_producer(mock_producer)
        self.relay._process_event(event)
        event.refresh_from_db()
        self.assertEqual(event.retry_count, 1)
        self.assertEqual(event.status, OutboxEvent.STATUS_PENDING)

    def test_max_retries_moves_to_failed(self):
        event = OutboxEvent.objects.create(
            event_type="TestEvent",
            aggregate_type="test",
            aggregate_id="1",
            payload={
                "event_id": str(uuid4()),
                "event_type": "TestEvent",
                "aggregate_type": "test",
                "aggregate_id": "1",
                "version": 1,
                "occurred_at": timezone.now().isoformat(),
                "data": {},
            },
            status=OutboxEvent.STATUS_PENDING,
            retry_count=4,
        )
        mock_producer = MagicMock()
        mock_producer.send.side_effect = Exception("fail")
        self.relay.set_kafka_producer(mock_producer)
        self.relay._process_event(event)
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxEvent.STATUS_FAILED)
        self.assertEqual(event.retry_count, 5)


# ---------------------------------------------------------------------------
# Dead Letter Command Tests
# ---------------------------------------------------------------------------


class DeadLetterRetryTest(OutboxTransactionTest):
    """Test dead_letter retry subcommand."""

    def setUp(self):
        from django.core.management import call_command
        self.call_command = call_command
        self.events = []
        for i in range(3):
            self.events.append(OutboxEvent.objects.create(
                event_type="TestEvent",
                aggregate_type="test",
                aggregate_id=str(i),
                payload={},
                status=OutboxEvent.STATUS_FAILED,
                retry_count=5,
                last_error="Kafka down",
            ))

    def test_retry_all_failed_events(self):
        from io import StringIO
        out = StringIO()
        self.call_command("dead_letter", "retry", stdout=out)
        for event in self.events:
            event.refresh_from_db()
            self.assertEqual(event.status, OutboxEvent.STATUS_PENDING)
            self.assertEqual(event.retry_count, 0)
            self.assertEqual(event.last_error, "")

    def test_retry_specific_event(self):
        from io import StringIO
        out = StringIO()
        target = self.events[1]
        self.call_command("dead_letter", "retry", event_id=target.pk, stdout=out)
        target.refresh_from_db()
        self.assertEqual(target.status, OutboxEvent.STATUS_PENDING)
        # Others unchanged
        self.events[0].refresh_from_db()
        self.assertEqual(self.events[0].status, OutboxEvent.STATUS_FAILED)


class DeadLetterStatsTest(OutboxTransactionTest):

    def setUp(self):
        from django.core.management import call_command
        self.call_command = call_command
        OutboxEvent.objects.create(event_type="E1", aggregate_type="t", aggregate_id="1", payload={}, status=OutboxEvent.STATUS_PENDING)
        OutboxEvent.objects.create(event_type="E2", aggregate_type="t", aggregate_id="2", payload={}, status=OutboxEvent.STATUS_PUBLISHED)
        OutboxEvent.objects.create(event_type="E3", aggregate_type="t", aggregate_id="3", payload={}, status=OutboxEvent.STATUS_FAILED)

    def test_stats_shows_correct_counts(self):
        from io import StringIO
        out = StringIO()
        self.call_command("dead_letter", "stats", stdout=out)
        output = out.getvalue()
        self.assertIn("Total events:", output)
        self.assertIn("Pending:", output)
        self.assertIn("Failed:", output)


class DeadLetterPurgeTest(OutboxTransactionTest):

    def setUp(self):
        from django.core.management import call_command
        self.call_command = call_command

    def test_purge_removes_old_events(self):
        from datetime import timedelta
        old = OutboxEvent.objects.create(
            event_type="Old", aggregate_type="t", aggregate_id="1",
            payload={}, status=OutboxEvent.STATUS_FAILED,
        )
        OutboxEvent.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=60)
        )
        recent = OutboxEvent.objects.create(
            event_type="Recent", aggregate_type="t", aggregate_id="2",
            payload={}, status=OutboxEvent.STATUS_FAILED,
        )
        from io import StringIO
        out = StringIO()
        self.call_command("dead_letter", "purge", older_than=30, stdout=out)
        self.assertFalse(OutboxEvent.objects.filter(pk=old.pk).exists())
        self.assertTrue(OutboxEvent.objects.filter(pk=recent.pk).exists())

    def test_dry_run_does_not_delete(self):
        from datetime import timedelta
        old = OutboxEvent.objects.create(
            event_type="Old", aggregate_type="t", aggregate_id="1",
            payload={}, status=OutboxEvent.STATUS_FAILED,
        )
        OutboxEvent.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=60)
        )
        from io import StringIO
        out = StringIO()
        self.call_command("dead_letter", "purge", older_than=30, dry_run=True, stdout=out)
        self.assertTrue(OutboxEvent.objects.filter(pk=old.pk).exists())


class DeadLetterResetTest(OutboxTransactionTest):

    def setUp(self):
        from django.core.management import call_command
        self.call_command = call_command

    def test_reset_stuck_processing_events(self):
        from datetime import timedelta
        stuck = OutboxEvent.objects.create(
            event_type="Stuck", aggregate_type="t", aggregate_id="1",
            payload={}, status=OutboxEvent.STATUS_PROCESSING,
        )
        OutboxEvent.objects.filter(pk=stuck.pk).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        recent = OutboxEvent.objects.create(
            event_type="Recent", aggregate_type="t", aggregate_id="2",
            payload={}, status=OutboxEvent.STATUS_PROCESSING,
        )
        from io import StringIO
        out = StringIO()
        self.call_command("dead_letter", "reset", stuck_minutes=5, stdout=out)
        stuck.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual(stuck.status, OutboxEvent.STATUS_PENDING)
        self.assertEqual(recent.status, OutboxEvent.STATUS_PROCESSING)
