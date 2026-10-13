"""Dead letters: archive (DLQ archiver group) and the explicit replay workflow.

Nothing replays automatically. An operator inspects the archive and replays
chosen events after fixing the cause (``manage.py kafka_dlq``):

    kafka_dlq list [--group notifications]
    kafka_dlq show <id>
    kafka_dlq replay <id> [<id> ...]       or  --group G --all --limit 20
    kafka_dlq discard <id> --note "..."

A replay sends the original event back to its group through the retry topic
(attempts start again, ``replay_count`` + 1), written to the outbox in the
same transaction that marks the archive row replayed. An event may be
replayed at most EVENTS_MAX_DLQ_REPLAYS times, and one command replays at
most ``MAX_REPLAY_BATCH`` events, so a poison message can't loop forever.
If it fails again it comes back to the archive as pending.
"""

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import envelope, metrics, topics
from .models import DeadLetterEvent, OutboxEvent

MAX_REPLAY_BATCH = 100


def archive(event):
    """Store or update the archive row for a dead-letter message."""
    message = event.raw
    failure = message["failure"]
    original = message["event"]
    fields = {
        "task": failure.get("task", "")[:100],
        "event_type": original.get("event_type", "")[:100],
        "aggregate_id": str(original.get("aggregate_id", ""))[:100],
        "reason": failure.get("reason", "")[:30],
        "error_category": failure.get("error_category", "")[:30],
        "error_message": failure.get("error_message", "")[:500],
        "attempts": int(failure.get("attempt", 0)),
        "original_topic": failure.get("original_topic", "")[:200],
        "original_partition": failure.get("original_partition"),
        "original_offset": failure.get("original_offset"),
        "first_failed_at": envelope.parse_time(failure.get("first_failed_at")),
        "last_failed_at": envelope.parse_time(failure.get("last_failed_at")),
        "envelope": message,
        "status": DeadLetterEvent.STATUS_PENDING,
    }
    with transaction.atomic():
        row, created = DeadLetterEvent.objects.select_for_update().get_or_create(
            consumer_group=failure.get("target_group", "")[:100],
            event_id=original["event_id"],
            defaults=fields,
        )
        if not created:
            # Dead-lettered again (e.g. after a replay): newest failure wins.
            for name, value in fields.items():
                setattr(row, name, value)
            row.save()
    metrics.event("warning", "dead_letter_archived", group=row.consumer_group, event_id=row.event_id,
                  event_type=row.event_type, reason=row.reason, created=created)
    return row


class ReplayRefused(Exception):
    pass


def replay(row_id, note=""):
    """Queue one archived dead letter for another round of attempts."""
    with transaction.atomic():
        row = DeadLetterEvent.objects.select_for_update().get(pk=row_id)
        if row.status != DeadLetterEvent.STATUS_PENDING:
            raise ReplayRefused(f"#{row.pk} is {row.status}, not pending")
        if row.replay_count >= settings.EVENTS_MAX_DLQ_REPLAYS:
            raise ReplayRefused(
                f"#{row.pk} was already replayed {row.replay_count} times "
                f"(EVENTS_MAX_DLQ_REPLAYS={settings.EVENTS_MAX_DLQ_REPLAYS}); discard it or fix it by hand"
            )
        original = row.envelope["event"]
        failure = {
            **row.envelope["failure"],
            "attempt": 0,  # a fresh set of attempts
            "next_retry_at": timezone.now().isoformat(),
            "replay_count": row.replay_count + 1,
            "replayed_at": timezone.now().isoformat(),
        }
        failure.pop("reason", None)
        failure.pop("dead_lettered_at", None)
        message = envelope.failure_message(envelope.RETRY, original, failure)
        OutboxEvent.objects.create(
            id=message["message_id"],
            event_id=original["event_id"],
            kind=OutboxEvent.KIND_FORWARD,
            event_type=row.event_type,
            topic=topics.name(topics.RETRY_EVENTS),
            key=row.aggregate_id,
            aggregate_type=original.get("aggregate_type", ""),
            aggregate_id=row.aggregate_id,
            payload=message,
            headers={"event_id": original["event_id"], "event_type": row.event_type,
                     "message_id": message["message_id"], "target_group": row.consumer_group},
        )
        row.status = DeadLetterEvent.STATUS_REPLAYED
        row.replay_count += 1
        row.replayed_at = timezone.now()
        row.note = note[:255]
        row.save(update_fields=["status", "replay_count", "replayed_at", "note", "updated_at"])
    metrics.event("warning", "dead_letter_replayed", group=row.consumer_group, event_id=row.event_id,
                  replay_count=row.replay_count)
    return row


def discard(row_id, note):
    with transaction.atomic():
        row = DeadLetterEvent.objects.select_for_update().get(pk=row_id)
        row.status = DeadLetterEvent.STATUS_DISCARDED
        row.note = note[:255]
        row.save(update_fields=["status", "note", "updated_at"])
    metrics.event("warning", "dead_letter_discarded", group=row.consumer_group, event_id=row.event_id)
    return row
