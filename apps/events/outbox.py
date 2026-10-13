"""Writing events.

``enqueue()``: reliable events (the transactional outbox). Call it inside
the transaction that makes the business change; the outbox row commits or
rolls back with it::

    with transaction.atomic():
        order = Order.objects.create(...)
        outbox.enqueue("order.placed", topic=topics.ORDER_EVENTS,
                       aggregate_type="order", aggregate_id=order.number,
                       payload={"order_number": order.number, ...})

The database commit and the Kafka publish are *not* atomic with each other:
the row is published later by ``outbox_publisher``, at least once.

``audit()``: best-effort audit events. No outbox row: after the commit the
event is handed to a background Kafka producer and may be lost if Kafka is
unavailable or the process dies first (counted as ``audit_dropped_total``).
Only for records nobody acts on (logins, admin edits).
"""

import logging

from django.conf import settings
from django.db import DatabaseError, transaction

from apps.common.proxy import current_request_id

from . import envelope, kafka, metrics, topics
from .models import AuditEvent, OutboxEvent

logger = logging.getLogger("apps.events")


def enqueue(event_type, *, topic, aggregate_type, aggregate_id, payload, key=None, correlation_id=None):
    message = envelope.new_event(
        event_type, aggregate_type, aggregate_id, payload,
        correlation_id=correlation_id or current_request_id(),
    )
    row = OutboxEvent.objects.create(
        event_id=message["event_id"],
        kind=OutboxEvent.KIND_EVENT,
        event_type=event_type,
        topic=topics.name(topic),
        key=str(key if key is not None else aggregate_id),
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        payload=message,
        headers={"event_type": event_type, "event_id": message["event_id"],
                 "correlation_id": message["correlation_id"]},
    )
    metrics.incr("outbox_enqueued_total")
    if settings.EVENTS_BACKEND == "inline" and settings.EVENTS_INLINE_DISPATCH:
        from . import inline

        transaction.on_commit(lambda: inline.dispatch(row.pk), robust=True)
    return row


def audit(event_type, *, aggregate_type, aggregate_id, data=None, actor_user_id=None):
    """Record a best-effort audit event after the current transaction commits."""
    message = envelope.new_event(
        event_type, aggregate_type, aggregate_id,
        {**(data or {}), "actor_user_id": actor_user_id},
        correlation_id=current_request_id(),
    )
    transaction.on_commit(lambda: _send_audit(message), robust=True)


def _send_audit(message):
    if not kafka.enabled():
        # No Kafka: store it directly (still best effort).
        try:
            record_audit(envelope.Event.from_message(message))
        except DatabaseError:
            metrics.incr("audit_dropped_total")
            logger.warning("Audit event %s not recorded", message["event_type"])
        return

    def delivered(err, msg):
        if err is None:
            metrics.incr("audit_published_total")
        else:
            metrics.incr("audit_dropped_total")
            metrics.event("warning", "audit_dropped", event_type=message["event_type"], error=str(err))

    try:
        producer = kafka.web_producer.get()
        producer.produce(
            topics.name(topics.AUDIT_EVENTS),
            key=str(message["aggregate_id"]),
            value=envelope.dumps(message),
            headers=kafka.headers_list({"event_type": message["event_type"], "event_id": message["event_id"]}),
            on_delivery=delivered,
        )
        producer.poll(0)  # serve delivery callbacks of earlier sends
    except Exception as exc:  # never fail the request for an audit record
        metrics.incr("audit_dropped_total")
        metrics.event("warning", "audit_dropped", event_type=message["event_type"], error=type(exc).__name__)


def record_audit(event):
    """Store an audit record (the audit consumer group; inline fallback).
    Idempotent: one row per event id."""
    payload = dict(event.payload)
    actor = payload.pop("actor_user_id", None)
    if actor is None:
        actor = payload.get("user_id")
    AuditEvent.objects.get_or_create(
        event_id=event.event_id,
        defaults={
            "event_type": event.event_type,
            "actor_user_id": actor if isinstance(actor, int) else None,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "data": payload,
            "correlation_id": (event.correlation_id or "")[:64],
            "occurred_at": envelope.parse_time(event.occurred_at) or _now(),
        },
    )


def _now():
    from django.utils import timezone

    return timezone.now()
