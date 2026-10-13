from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.events import dlq, envelope
from apps.events.models import DeadLetterEvent, OutboxEvent

from .helpers import event_message


def archived(event_type="test.x", group="notifications"):
    event = event_message(event_type, aggregate_id="PF-1")
    failure = {"target_group": group, "task": "t", "attempt": 5, "reason": "retries_exhausted",
               "error_category": "transient", "error_message": "ConnectionError: x",
               "original_topic": "order-events", "original_partition": 1, "original_offset": 9,
               "first_failed_at": timezone.now().isoformat(), "last_failed_at": timezone.now().isoformat(),
               "replay_count": 0}
    message = envelope.failure_message(envelope.DEAD_LETTER, event, failure)
    return dlq.archive(_event(event, message))


def _event(event, message):
    e = envelope.Event.from_message(event)
    e.raw = message
    return e


@override_settings(EVENTS_MAX_DLQ_REPLAYS=2)
class ReplayTest(TestCase):
    def test_replay_queues_a_fresh_retry_through_the_outbox(self):
        row = archived()
        dlq.replay(row.pk, note="smtp fixed")
        row.refresh_from_db()
        self.assertEqual((row.status, row.replay_count, row.note), ("replayed", 1, "smtp fixed"))
        forward = OutboxEvent.objects.get(kind=OutboxEvent.KIND_FORWARD)
        self.assertEqual(forward.topic, "retry-events")
        failure = forward.payload["failure"]
        self.assertEqual((failure["attempt"], failure["replay_count"], failure["target_group"]),
                         (0, 1, "notifications"))
        self.assertNotIn("reason", failure)
        self.assertEqual(forward.payload["event"]["event_id"], str(row.event_id))

    def test_only_pending_rows_and_a_bounded_number_of_times(self):
        row = archived()
        dlq.replay(row.pk)
        with self.assertRaises(dlq.ReplayRefused):
            dlq.replay(row.pk)  # already replayed
        # Dead-lettered again after the replay: pending once more.
        archived_again = dlq.archive(_event(row.envelope["event"], {**row.envelope, "message_id": "m2"}))
        self.assertEqual((archived_again.pk, archived_again.status), (row.pk, "pending"))
        dlq.replay(row.pk)
        dlq.archive(_event(row.envelope["event"], {**row.envelope, "message_id": "m3"}))
        with self.assertRaisesRegex(dlq.ReplayRefused, "EVENTS_MAX_DLQ_REPLAYS"):
            dlq.replay(row.pk)

    def test_command_requires_explicit_bounded_selection(self):
        for _ in range(3):
            archived()
        with self.assertRaises(CommandError):
            call_command("kafka_dlq", "replay")
        with self.assertRaises(CommandError):
            call_command("kafka_dlq", "replay", "--all")  # needs --group
        with self.assertRaises(CommandError):
            call_command("kafka_dlq", "replay", "--all", "--group", "notifications", "--limit", "500")
        call_command("kafka_dlq", "replay", "--all", "--group", "notifications", "--limit", "2")
        self.assertEqual(DeadLetterEvent.objects.filter(status="replayed").count(), 2)

    def test_discard(self):
        row = archived()
        call_command("kafka_dlq", "discard", str(row.pk), "--note", "obsolete order")
        row.refresh_from_db()
        self.assertEqual(row.status, "discarded")
