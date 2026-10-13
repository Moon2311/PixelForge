"""Outbox publisher (``manage.py outbox_publisher``).

Loop: claim due pending rows -> produce them -> wait for the broker's
acknowledgement -> mark each acknowledged row published; record a failure
(with backoff) for the rest.

* Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` and a lease
  (``locked_until``), committed before any Kafka call, so no transaction is
  held open while talking to Kafka and several publishers never take the
  same row. A publisher that dies leaves its claims to expire; another (or
  the restarted one) takes them over, so nothing pending is lost.
* A row is marked published only from its delivery callback (``acks=all``).
  If the publisher dies after the broker acknowledged but before the update,
  the row is published again: consumers deduplicate on the event id.
* Transient errors (broker down, timeouts) retry forever with capped
  exponential backoff; permanent ones (message too large, not authorised)
  mark the row failed for an operator (``manage.py outbox requeue``).
* Ordering: rows are produced in creation order, and the idempotent producer
  keeps them in order per partition. A row that fails and is retried can be
  overtaken by a later row with the same key; run one publisher where strict
  per-key order matters.

In inline mode the same loop processes rows in-process (``inline.process``)
instead of producing them, which retries rows whose inline tasks failed.
"""

import os
import random
import signal
import socket
import threading
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, close_old_connections, transaction
from django.db.models import F, Q
from django.utils import timezone

from . import envelope, heartbeat, inline, kafka, metrics
from .models import OutboxEvent

ROLE = "outbox-publisher"


def backoff(attempts):
    delay = settings.OUTBOX_RETRY_BASE_DELAY * 2 ** max(0, attempts - 1)
    return min(settings.OUTBOX_RETRY_MAX_DELAY, delay) * random.uniform(0.8, 1.2)


class OutboxPublisher:
    def __init__(self, producer=None, batch_size=None, lease_seconds=None, name=None):
        self.batch_size = batch_size or settings.OUTBOX_BATCH_SIZE
        self.lease = lease_seconds or settings.OUTBOX_LEASE_SECONDS
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self._producer = producer
        self._stop = threading.Event()
        self.heartbeat = heartbeat.Heartbeat(ROLE)

    @property
    def producer(self):
        if self._producer is None:
            self._producer = kafka.producer("outbox-publisher")
        return self._producer

    def claim(self):
        now = timezone.now()
        with transaction.atomic():
            rows = list(
                OutboxEvent.objects.select_for_update(skip_locked=True)
                .filter(status=OutboxEvent.STATUS_PENDING, available_at__lte=now)
                .filter(Q(locked_until__isnull=True) | Q(locked_until__lt=now))
                .order_by("created_at")[: self.batch_size]
            )
            if rows:
                OutboxEvent.objects.filter(pk__in=[r.pk for r in rows]).update(
                    locked_until=now + timedelta(seconds=self.lease), locked_by=self.name[:100]
                )
        return rows

    def publish_once(self):
        """One batch. Returns ``{"claimed", "published", "failed"}``."""
        rows = self.claim()
        if not rows:
            return {"claimed": 0, "published": 0, "failed": 0}
        if not kafka.enabled():
            for row in rows:
                inline.process(row)
            return {"claimed": len(rows), "published": len(rows), "failed": 0}

        results = {}

        def callback(row_id):
            def delivered(err, msg):
                results[row_id] = (err, msg.partition() if err is None else None,
                                   msg.offset() if err is None else None)
            return delivered

        for row in rows:
            try:
                self.producer.produce(
                    row.topic,
                    key=row.key or None,
                    value=_value(row),
                    headers=kafka.headers_list({**row.headers, "message_id": str(row.pk)}),
                    on_delivery=callback(row.pk),
                )
            except BufferError:
                self.producer.poll(1.0)
                results[row.pk] = (kafka.KafkaError(kafka.KafkaError._QUEUE_FULL, "producer queue full"), None, None)
            except kafka.KafkaException as exc:
                results[row.pk] = (exc.args[0], None, None)
        self.producer.flush(settings.KAFKA_DELIVERY_TIMEOUT_MS / 1000 + 5)

        published = failed = 0
        for row in rows:
            err, partition, offset = results.get(
                row.pk, (kafka.KafkaError(kafka.KafkaError._MSG_TIMED_OUT, "delivery not confirmed"), None, None)
            )
            if err is None:
                self._mark_published(row, partition, offset)
                published += 1
            else:
                self._mark_failed(row, err)
                failed += 1
        return {"claimed": len(rows), "published": published, "failed": failed}

    def _mark_published(self, row, partition, offset):
        OutboxEvent.objects.filter(pk=row.pk, status=OutboxEvent.STATUS_PENDING).update(
            status=OutboxEvent.STATUS_PUBLISHED, published_at=timezone.now(), kafka_partition=partition,
            kafka_offset=offset, attempts=F("attempts") + 1, locked_until=None, locked_by="", last_error="",
        )
        metrics.incr("outbox_published_total")
        metrics.event("info", "outbox_published", event_id=row.event_id, event_type=row.event_type,
                      topic=row.topic, partition=partition, offset=offset)

    def _mark_failed(self, row, err):
        permanent = kafka.is_permanent(err)
        attempts = row.attempts + 1
        OutboxEvent.objects.filter(pk=row.pk, status=OutboxEvent.STATUS_PENDING).update(
            status=OutboxEvent.STATUS_FAILED if permanent else OutboxEvent.STATUS_PENDING,
            attempts=attempts, available_at=timezone.now() + timedelta(seconds=backoff(attempts)),
            locked_until=None, locked_by="", last_error=str(err)[:500],
        )
        metrics.incr("outbox_permanent_failures_total" if permanent else "outbox_publish_failures_total")
        metrics.event("error" if permanent else "warning", "outbox_publish_failed", event_id=row.event_id,
                      event_type=row.event_type, topic=row.topic, attempts=attempts,
                      permanent=permanent, error=str(err)[:200])

    def run(self, once=False):
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.stop())
        metrics.event("info", "outbox_publisher_started", publisher=self.name, backend=settings.EVENTS_BACKEND)
        try:
            while not self._stop.is_set():
                close_old_connections()
                try:
                    result = self.publish_once()
                except DatabaseError as exc:
                    metrics.event("warning", "outbox_publisher_db_error", error=str(exc)[:200])
                    result = {"claimed": 0}
                    self._stop.wait(settings.OUTBOX_POLL_INTERVAL * 5)
                self.heartbeat.beat_if_due(stats=metrics.snapshot())
                if once:
                    break
                if not result["claimed"]:
                    self._stop.wait(settings.OUTBOX_POLL_INTERVAL)
        finally:
            if self._producer is not None:
                self._producer.flush(10)
            self.heartbeat.stop()
            metrics.event("info", "outbox_publisher_stopped", publisher=self.name)

    def stop(self):
        self._stop.set()


def _value(row):
    return envelope.dumps(row.payload)

