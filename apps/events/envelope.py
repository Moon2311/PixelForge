"""Message formats (JSON values; keys are plain strings).

Event (on the business topics)::

    {"message_type": "event", "event_id": "<uuid>", "event_type": "order.placed",
     "event_version": 1, "aggregate_type": "order", "aggregate_id": "PF-1A2B3C4D",
     "occurred_at": "<iso>", "correlation_id": "<request id>", "producer": "pixelforge",
     "payload": {...}}

Payloads carry identifiers and non-sensitive facts only (order number,
status, amount); consumers load anything else from PostgreSQL. Never
credentials, tokens, provider responses or card data.

Retry (``retry-events``) and dead letter (``dead-letter-events``)::

    {"message_type": "retry" | "dead_letter", "message_id": "<uuid>",
     "event": <the original event>, "failure": {...}}

``failure``: target_group, consumer_group, task, event_type, entity_id,
attempt (attempts made), max_attempts, original_topic/partition/offset,
error_category, error_type, error_message (sanitized), first_failed_at,
last_failed_at, next_retry_at (retry only), reason and dead_lettered_at
(dead letter only), correlation_id, replay_count.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from datetime import timezone as dt_timezone

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

EVENT = "event"
RETRY = "retry"
DEAD_LETTER = "dead_letter"
VERSION = 1
PRODUCER = "pixelforge"


class InvalidMessage(ValueError):
    """A message that isn't a well-formed envelope (never retried)."""


def new_event(event_type, aggregate_type, aggregate_id, payload, correlation_id=None, event_id=None):
    return {
        "message_type": EVENT,
        "event_id": str(event_id or uuid.uuid4()),
        "event_type": event_type,
        "event_version": VERSION,
        "aggregate_type": aggregate_type,
        "aggregate_id": str(aggregate_id),
        "occurred_at": timezone.now().isoformat(),
        "correlation_id": correlation_id or "",
        "producer": PRODUCER,
        # Round-trip through JSON now: Decimals and datetimes become strings
        # here, not differently in Kafka and in inline mode.
        "payload": json.loads(json.dumps(payload or {}, cls=DjangoJSONEncoder)),
    }


def dumps(message):
    return json.dumps(message, cls=DjangoJSONEncoder, separators=(",", ":")).encode()


def loads(value):
    if value is None:
        raise InvalidMessage("empty message")
    try:
        message = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise InvalidMessage(f"not JSON: {exc}") from None
    if not isinstance(message, dict):
        raise InvalidMessage("not a JSON object")
    kind = message.get("message_type", EVENT)
    if kind == EVENT:
        validate_event(message)
    elif kind in (RETRY, DEAD_LETTER):
        if not isinstance(message.get("event"), dict) or not isinstance(message.get("failure"), dict):
            raise InvalidMessage(f"{kind} message without event/failure")
        validate_event(message["event"])
    else:
        raise InvalidMessage(f"unknown message_type {kind!r}")
    return message


def validate_event(event):
    for name in ("event_id", "event_type"):
        if not isinstance(event.get(name), str) or not event[name]:
            raise InvalidMessage(f"event without {name}")
    try:
        uuid.UUID(event["event_id"])
    except ValueError:
        raise InvalidMessage("event_id is not a UUID") from None
    if not isinstance(event.get("payload", {}), dict):
        raise InvalidMessage("payload is not an object")


@dataclass
class Event:
    """What a task receives."""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict
    occurred_at: str = ""
    correlation_id: str = ""
    attempt: int = 1
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_message(cls, event, attempt=1):
        return cls(
            event_id=event["event_id"],
            event_type=event["event_type"],
            aggregate_type=event.get("aggregate_type", ""),
            aggregate_id=str(event.get("aggregate_id", "")),
            payload=event.get("payload") or {},
            occurred_at=event.get("occurred_at", ""),
            correlation_id=event.get("correlation_id", ""),
            attempt=attempt,
            raw=event,
        )


def failure_message(kind, event, failure):
    return {"message_type": kind, "message_id": str(uuid.uuid4()), "event": event, "failure": failure}


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if timezone.is_aware(parsed) else parsed.replace(tzinfo=dt_timezone.utc)
