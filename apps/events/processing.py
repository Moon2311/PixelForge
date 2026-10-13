"""Run one task for one event: deduplicate, enforce the time limit, record
success, classify failure. Used by the Kafka worker and by inline mode.

Idempotency, in order:

1. ``ProcessedEvent(consumer_group, event_id)`` exists -> the task already
   succeeded for this group: skip it (a redelivery or a duplicate publish).
2. The task runs. Tasks are idempotent themselves (unique notification
   keys, unique audit/archive rows), which covers a crash after the task's
   effect but before step 3.
3. ``ProcessedEvent`` is inserted (unique constraint; a concurrent
   duplicate's insert loses harmlessly).
4. The caller commits the Kafka offset. A crash between 3 and 4 means a
   redelivery, which step 1 skips.
"""

import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from django.db import DatabaseError, IntegrityError, transaction

from apps.common.proxy import bind_request_id, reset_request_id

from . import metrics, registry
from .models import ProcessedEvent
from .retry import RetryPolicy, TaskTimeout, sanitize_error

PROCESSED = "processed"
DUPLICATE = "duplicate"
IGNORED = "ignored"
FAILED = "failed"


@dataclass
class Outcome:
    status: str
    task: object = None
    error: BaseException = None
    retryable: bool = False
    category: str = ""
    duration_ms: float = 0.0

    @property
    def ok(self):
        return self.status != FAILED

    @property
    def policy(self):
        return self.task.policy() if self.task else RetryPolicy.from_settings()

    @property
    def error_message(self):
        return sanitize_error(self.error) if self.error else ""


@contextmanager
def time_limit(seconds):
    """Raise ``TaskTimeout`` in the task after ``seconds`` (SIGALRM; only in
    the main thread of a Unix process, i.e. the workers). Elsewhere, or for
    ``seconds`` <= 0, no limit is enforced."""
    usable = (
        seconds and seconds > 0
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )
    if not usable:
        yield
        return

    def expired(signum, frame):
        raise TaskTimeout(f"task exceeded its {seconds:g}s time limit")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def dedupe_id(group, event, message):
    if group.dedupe_on == "message_id" and message.get("message_id"):
        return message["message_id"]
    return event.event_id


def execute(group, event, *, message=None, topic="", partition=None, offset=None, enforce_timeout=True):
    task = registry.find(group.name, event.event_type)
    if task is None:
        metrics.incr("events_ignored_total")
        return Outcome(IGNORED)

    consumer_group = registry.group_id(group.name)
    key = dedupe_id(group, event, message or event.raw)
    try:
        if ProcessedEvent.objects.filter(consumer_group=consumer_group, event_id=key).exists():
            metrics.incr("events_duplicates_total")
            metrics.event("info", "event_duplicate", group=group.name, task=task.name,
                          event_id=event.event_id, event_type=event.event_type)
            return Outcome(DUPLICATE, task)
    except DatabaseError as exc:
        return _failed(task, exc, 0.0, group, event)

    policy = task.policy()
    token = bind_request_id(event.correlation_id or None)
    started = time.monotonic()
    try:
        with time_limit(policy.timeout if enforce_timeout else None):
            task(event)
    except Exception as exc:
        return _failed(task, exc, (time.monotonic() - started) * 1000, group, event)
    finally:
        reset_request_id(token)
    duration = (time.monotonic() - started) * 1000

    try:
        with transaction.atomic():
            ProcessedEvent.objects.create(
                consumer_group=consumer_group, event_id=key, event_type=event.event_type, task=task.name,
                topic=topic, partition=partition, offset=offset, attempt=event.attempt,
                duration_ms=int(duration),
            )
    except IntegrityError:
        pass  # a concurrent duplicate recorded it first
    except DatabaseError as exc:
        # The work is done; without the record a redelivery relies on the
        # task's own idempotency.
        metrics.event("warning", "processed_record_failed", group=group.name, event_id=event.event_id,
                      error=sanitize_error(exc))
    metrics.incr("events_processed_total")
    metrics.observe(task.name, duration)
    metrics.event("info", "event_processed", group=group.name, task=task.name, event_id=event.event_id,
                  event_type=event.event_type, attempt=event.attempt, duration_ms=round(duration, 1),
                  correlation_id=event.correlation_id)
    return Outcome(PROCESSED, task, duration_ms=duration)


def _failed(task, exc, duration, group, event):
    policy = task.policy()
    outcome = Outcome(FAILED, task, exc, policy.is_retryable(exc), policy.category(exc), duration)
    metrics.incr("events_failed_total")
    if isinstance(exc, TaskTimeout):
        metrics.incr("events_timeouts_total")
    metrics.observe(task.name, duration, failed=True)
    metrics.event("warning", "event_failed", group=group.name, task=task.name, event_id=event.event_id,
                  event_type=event.event_type, attempt=event.attempt, retryable=outcome.retryable,
                  category=outcome.category, error=outcome.error_message,
                  correlation_id=event.correlation_id)
    return outcome
