"""Kafka consumer worker for one consumer group (``manage.py kafka_worker <group>``).

Per message, in partition order:

1. Parse the envelope. A malformed message goes to the DLQ (never retried).
2. Retry messages (``retry-events``) addressed to another group are skipped.
   One that isn't due yet is *not* waited for: the partition is paused and
   rewound to that message, and resumed when it is due. Other partitions
   keep flowing; nothing sleeps inside the consumer.
3. Run the task (``processing.execute``: dedup, time limit, classification).
4. Success (or duplicate / no task for this event type): commit the offset.
5. Failure: publish a retry message (``attempt + 1``, ``next_retry_at`` from
   the policy) or, once the attempts are used up or the error can't be
   retried, a dead letter. The offset is committed only after Kafka
   acknowledged that message. If Kafka can't take it, the message is
   written to the outbox (PostgreSQL) for the publisher instead. If neither
   works, nothing is committed: the partition is rewound and paused with a
   backoff, and the same message is processed again later. A failed task is
   never acknowledged without a durable retry/dead-letter record.

The DLQ archiver group (``retries=False``) has no retry topic: a failed
archive is redelivered after a backoff the same way.

Offsets are committed synchronously, one message at a time, after the work
is durable (at-least-once). A worker that crashes before committing has its
messages redelivered to the next owner of the partition; ``ProcessedEvent``
makes those redeliveries no-ops.

Retried messages leave their partition, so a later event for the same key
can be processed before the retried one: tasks load current state from
PostgreSQL and never depend on strict event order.
"""

import signal
import threading
import time
import uuid

from confluent_kafka import KafkaError, KafkaException, TopicPartition
from django.conf import settings
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.utils import timezone

from . import envelope, heartbeat, kafka, metrics, processing, registry, topics
from .models import OutboxEvent
from .retry import CATEGORY_INVALID

RETRIES_EXHAUSTED = "retries_exhausted"
NON_RETRYABLE = "non_retryable"
INVALID_MESSAGE = "invalid_message"


class Worker:
    def __init__(self, group_name, consumer=None, producer=None):
        self.group = registry.GROUPS[group_name]
        self.group_id = registry.group_id(group_name)
        self.retry_topic = topics.name(topics.RETRY_EVENTS)
        self.dlq_topic = topics.name(topics.DEAD_LETTER_EVENTS)
        self.topics = [topics.name(t) for t in self.group.topics]
        if self.group.retries:
            self.topics.append(self.retry_topic)
        self.consumer = consumer or kafka.consumer(self.group_id)
        self.producer = producer or kafka.producer(f"{group_name}-worker")
        self.heartbeat = heartbeat.Heartbeat(self.group_id)
        self._paused = {}    # (topic, partition) -> monotonic time to resume
        self._blocked = {}   # (topic, partition) -> consecutive failures to record a failure
        self._assignment = []
        self._stop = threading.Event()
        self.handled = 0

    # -- loop --------------------------------------------------------------

    def run(self, max_messages=None, timeout=None, until=None):
        """Consume until stopped (SIGTERM/SIGINT), or until ``max_messages``
        were handled, ``timeout`` seconds passed or ``until()`` is true
        (tests, one-off runs)."""
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.stop())
        deadline = time.monotonic() + timeout if timeout else None
        self.consumer.subscribe(self.topics, on_assign=self._on_assign, on_revoke=self._on_revoke,
                                on_lost=self._on_revoke)
        metrics.event("info", "worker_started", group=self.group_id, topics=",".join(self.topics))
        try:
            while not self._stop.is_set():
                if (deadline and time.monotonic() >= deadline) or (until and until()):
                    break
                self._resume_due()
                message = self.consumer.poll(self._poll_timeout())
                self.producer.poll(0)
                self.heartbeat.beat_if_due(self._assignment, metrics.snapshot())
                if message is None:
                    continue
                if message.error():
                    self._on_error(message.error())
                    continue
                self.handle(message)
                self.handled += 1
                if max_messages and self.handled >= max_messages:
                    break
        finally:
            self.close()

    def stop(self):
        self._stop.set()

    def close(self):
        try:
            self.consumer.close()  # leaves the group now, so the partitions move at once
        except (KafkaException, RuntimeError):
            pass
        self.producer.flush(10)
        self.heartbeat.stop()
        metrics.event("info", "worker_stopped", group=self.group_id, handled=self.handled)

    def _poll_timeout(self):
        if not self._paused:
            return 1.0
        return max(0.05, min(1.0, min(self._paused.values()) - time.monotonic()))

    def _on_assign(self, consumer, partitions):
        self._assignment = sorted(f"{p.topic}[{p.partition}]" for p in partitions)
        metrics.event("info", "partitions_assigned", group=self.group_id, partitions=",".join(self._assignment))

    def _on_revoke(self, consumer, partitions):
        for p in partitions:
            self._paused.pop((p.topic, p.partition), None)
            self._blocked.pop((p.topic, p.partition), None)
        revoked = {f"{p.topic}[{p.partition}]" for p in partitions}
        self._assignment = [a for a in self._assignment if a not in revoked]
        metrics.event("info", "partitions_revoked", group=self.group_id, partitions=",".join(sorted(revoked)))

    def _on_error(self, error):
        if error.code() == KafkaError._PARTITION_EOF:
            return
        metrics.event("error" if error.fatal() else "warning", "consumer_error", group=self.group_id,
                      code=error.name(), error=error.str())
        if error.fatal():
            raise KafkaException(error)

    # -- one message -------------------------------------------------------

    def handle(self, message):
        metrics.incr("events_consumed_total")
        _recycle_connections()  # like a request: drop broken/expired DB connections
        try:
            data = envelope.loads(message.value())
        except envelope.InvalidMessage as exc:
            return self._invalid(message, exc)

        kind = data.get("message_type", envelope.EVENT)
        prior = None
        if kind == envelope.RETRY:
            prior = data["failure"]
            if prior.get("target_group") != self.group.name:
                return self._commit(message)  # another group's retry
            due = envelope.parse_time(prior.get("next_retry_at"))
            if due and due > timezone.now():
                return self._defer(message, (due - timezone.now()).total_seconds())
            event_data, attempt = data["event"], int(prior.get("attempt", 0)) + 1
        elif kind == envelope.DEAD_LETTER:
            if self.group.message_type != envelope.DEAD_LETTER:
                return self._commit(message)
            event_data, attempt = data["event"], 1
        else:
            if self.group.message_type != envelope.EVENT:
                return self._commit(message)
            event_data, attempt = data, 1

        event = envelope.Event.from_message(event_data, attempt=attempt)
        event.raw = data
        outcome = processing.execute(self.group, event, message=data, topic=message.topic(),
                                     partition=message.partition(), offset=message.offset())
        _recycle_connections()
        if outcome.ok:
            return self._commit(message)
        if not self.group.retries:
            return self._block(message, outcome.error_message)
        kind, topic, failure = self._failure(event, prior, outcome, message)
        if self._forward(kind, topic, event_data, failure, message.key()):
            return self._commit(message)
        return self._block(message, "could not record the failure in Kafka or PostgreSQL")

    def _failure(self, event, prior, outcome, message):
        policy = outcome.policy
        now = timezone.now()
        prior = prior or {}
        failure = {
            "target_group": self.group.name,
            "consumer_group": self.group_id,
            "task": outcome.task.name if outcome.task else "",
            "event_type": event.event_type,
            "entity_id": event.aggregate_id,
            "attempt": event.attempt,
            "max_attempts": policy.max_attempts,
            "original_topic": prior.get("original_topic") or message.topic(),
            "original_partition": prior.get("original_partition", message.partition()),
            "original_offset": prior.get("original_offset", message.offset()),
            "error_category": outcome.category,
            "error_type": type(outcome.error).__name__ if outcome.error else "",
            "error_message": outcome.error_message,
            "first_failed_at": prior.get("first_failed_at") or now.isoformat(),
            "last_failed_at": now.isoformat(),
            "correlation_id": event.correlation_id,
            "replay_count": int(prior.get("replay_count", 0)),
        }
        if outcome.retryable and event.attempt < policy.max_attempts:
            failure["next_retry_at"] = policy.next_retry_at(now, event.attempt).isoformat()
            return envelope.RETRY, self.retry_topic, failure
        failure["reason"] = RETRIES_EXHAUSTED if outcome.retryable else NON_RETRYABLE
        failure["dead_lettered_at"] = now.isoformat()
        return envelope.DEAD_LETTER, self.dlq_topic, failure

    def _invalid(self, message, exc):
        """A message that can't be parsed: dead-letter it with a preview."""
        raw = message.value() or b""
        event_data = {
            "message_type": envelope.EVENT,
            "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                       f"kafka:{message.topic()}/{message.partition()}/{message.offset()}")),
            "event_type": "invalid",
            "aggregate_type": "",
            "aggregate_id": "",
            "payload": {"raw_preview": raw[:500].decode("utf-8", "replace")},
        }
        now = timezone.now().isoformat()
        failure = {
            "target_group": self.group.name, "consumer_group": self.group_id, "task": "",
            "event_type": "invalid", "entity_id": "", "attempt": 1, "max_attempts": 1,
            "original_topic": message.topic(), "original_partition": message.partition(),
            "original_offset": message.offset(), "error_category": CATEGORY_INVALID,
            "error_type": type(exc).__name__, "error_message": str(exc)[:300], "first_failed_at": now,
            "last_failed_at": now, "correlation_id": "", "replay_count": 0, "reason": INVALID_MESSAGE,
            "dead_lettered_at": now,
        }
        metrics.event("error", "invalid_message", group=self.group_id, topic=message.topic(),
                      partition=message.partition(), offset=message.offset(), error=str(exc)[:200])
        if not self.group.retries:
            # A malformed dead letter stays in the DLQ topic (Kafka retention);
            # there is nowhere further to send it.
            return self._commit(message)
        if self._forward(envelope.DEAD_LETTER, self.dlq_topic, event_data, failure, message.key()):
            return self._commit(message)
        return self._block(message, "could not dead-letter an invalid message")

    def _forward(self, kind, topic, event_data, failure, key):
        """Publish a retry/dead-letter message; fall back to the outbox."""
        message = envelope.failure_message(kind, event_data, failure)
        counter = "events_retried_total" if kind == envelope.RETRY else "events_dead_lettered_total"
        log_fields = {"group": self.group_id, "event_id": event_data["event_id"],
                      "event_type": event_data["event_type"], "attempt": failure["attempt"],
                      "category": failure["error_category"], "error": failure["error_message"],
                      "correlation_id": failure["correlation_id"]}
        headers = {"event_id": event_data["event_id"], "event_type": event_data["event_type"],
                   "message_id": message["message_id"], "target_group": self.group.name}
        error, _, _ = kafka.produce_sync(self.producer, topic, envelope.dumps(message), key=key,
                                         headers=headers)
        if error is None:
            metrics.incr(counter)
            if kind == envelope.RETRY:
                metrics.event("warning", "event_retry_scheduled", next_retry_at=failure["next_retry_at"],
                              **log_fields)
            else:
                metrics.event("error", "event_dead_lettered", reason=failure["reason"], **log_fields)
            return True

        metrics.event("warning", "forward_publish_failed", topic=topic, kafka_error=str(error)[:200], **log_fields)
        try:
            with transaction.atomic():
                OutboxEvent.objects.create(
                    id=message["message_id"],
                    event_id=event_data["event_id"],
                    kind=OutboxEvent.KIND_FORWARD,
                    event_type=event_data["event_type"],
                    topic=topic,
                    key=(key or b"").decode("utf-8", "replace") if isinstance(key, bytes) else (key or ""),
                    aggregate_type=event_data.get("aggregate_type", ""),
                    aggregate_id=str(event_data.get("aggregate_id", ""))[:100],
                    payload=message,
                    headers=headers,
                )
        except DatabaseError as exc:
            metrics.incr("events_forward_blocked_total")
            metrics.event("error", "forward_fallback_failed", db_error=str(exc)[:200], **log_fields)
            _recycle_connections()
            return False
        metrics.incr(counter)
        metrics.incr("events_forward_fallbacks_total")
        metrics.event("warning", "forward_stored_in_outbox", topic=topic, **log_fields)
        return True

    # -- offsets and partition flow ----------------------------------------

    def _commit(self, message):
        tp = (message.topic(), message.partition())
        try:
            self.consumer.commit(message=message, asynchronous=False)
        except KafkaException as exc:
            # E.g. a rebalance took the partition: the next owner gets the
            # message again and deduplicates it.
            metrics.event("warning", "commit_failed", group=self.group_id, topic=tp[0], partition=tp[1],
                          offset=message.offset(), error=str(exc)[:200])
        self._blocked.pop(tp, None)

    def _rewind_and_pause(self, message, seconds):
        tp = TopicPartition(message.topic(), message.partition(), message.offset())
        self.consumer.pause([tp])
        self.consumer.seek(tp)
        self._paused[(message.topic(), message.partition())] = time.monotonic() + max(0.0, seconds)

    def _defer(self, message, seconds):
        metrics.incr("events_retry_deferred_total")
        self._rewind_and_pause(message, seconds)

    def _block(self, message, reason):
        tp = (message.topic(), message.partition())
        failures = self._blocked.get(tp, 0) + 1
        self._blocked[tp] = failures
        delay = min(settings.EVENTS_FORWARD_BACKOFF_MAX, settings.EVENTS_FORWARD_BACKOFF * 2 ** (failures - 1))
        metrics.event("error", "partition_blocked", group=self.group_id, topic=tp[0], partition=tp[1],
                      offset=message.offset(), retry_in=round(delay, 1), reason=reason)
        self._rewind_and_pause(message, delay)

    def _resume_due(self):
        now = time.monotonic()
        due = [tp for tp, at in self._paused.items() if at <= now]
        if due:
            self.consumer.resume([TopicPartition(t, p) for t, p in due])
            for tp in due:
                del self._paused[tp]


def _recycle_connections():
    """Close broken or expired database connections between messages (as
    Django does between requests), so a worker reconnects after a database
    restart or failover. Skipped inside a transaction (tests)."""
    if not connection.in_atomic_block:
        close_old_connections()
