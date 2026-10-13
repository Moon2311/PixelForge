"""Inline mode (EVENTS_BACKEND=inline, no Kafka): outbox rows are processed
in-process right after their transaction commits, by the same tasks the
workers run. This keeps the single-backend development setup (and the test
suite) working without a broker.

A row whose tasks all succeed is marked published. A failed task leaves
the row pending with a backoff (``outbox_publisher`` retries it in inline
mode too) or, when the failure can't be retried or the attempts are used
up, marks it failed. Inline mode has no retry topic or DLQ.
"""

from datetime import timedelta

from django.db import DatabaseError, transaction
from django.utils import timezone

from . import envelope, metrics, processing, registry, topics
from .models import OutboxEvent
from .retry import RetryPolicy


def dispatch(row_id):
    """Process one outbox row now (after its transaction committed)."""
    with transaction.atomic():
        row = (
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(pk=row_id, status=OutboxEvent.STATUS_PENDING)
            .first()
        )
        if row is None:
            return None  # already processed, or another process has it
        row.locked_until = timezone.now() + timedelta(seconds=60)
        row.save(update_fields=["locked_until"])
    return process(row)


def process(row):
    if row.kind != OutboxEvent.KIND_EVENT:
        _finish(row, failed=True, error="retry/dead-letter forwards need EVENTS_BACKEND=kafka")
        return row
    logical = topics.logical_name(row.topic)
    event = envelope.Event.from_message(row.payload, attempt=row.attempts + 1)
    failures = []
    for group in registry.groups_for_topic(logical):
        if not group.retries:
            continue
        try:
            outcome = processing.execute(group, event, message=row.payload, topic=row.topic,
                                         enforce_timeout=False)
        except Exception as exc:  # a broken task must not break the request that committed
            outcome = processing.Outcome(processing.FAILED, error=exc, retryable=True)
        if not outcome.ok:
            failures.append(outcome)
    if not failures:
        metrics.incr("inline_dispatched_total")
        _finish(row)
        return row
    metrics.incr("inline_failures_total")
    policy = RetryPolicy.from_settings()
    give_up = any(not f.retryable for f in failures) or row.attempts + 1 >= policy.max_attempts
    _finish(row, failed=give_up, error=failures[0].error_message, delay=policy.delay(row.attempts + 1))
    return row


def _finish(row, failed=False, error="", delay=0.0):
    now = timezone.now()
    row.attempts += 1
    row.locked_until = None
    row.last_error = error[:500]
    if not error:
        row.status, row.published_at = OutboxEvent.STATUS_PUBLISHED, now
    elif failed:
        row.status = OutboxEvent.STATUS_FAILED
    else:
        row.available_at = now + timedelta(seconds=delay)
    try:
        row.save(update_fields=["attempts", "locked_until", "last_error", "status", "published_at", "available_at"])
    except DatabaseError:
        metrics.event("warning", "inline_outbox_update_failed", event_id=row.event_id)
