import uuid

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.utils import timezone


class OutboxEvent(models.Model):
    """A message waiting to be published to Kafka (the transactional outbox).

    Business code writes ``KIND_EVENT`` rows in the same transaction as the
    change they describe, so a committed order always has its event and a
    rolled-back one never does. Workers also write ``KIND_FORWARD`` rows (a
    retry or dead-letter message they could not publish directly), so a
    failed task is never lost while Kafka is unavailable.

    ``payload`` is the exact Kafka message value. The row's ``id`` is the
    message id; ``event_id`` is the business event id, shared by an event
    and its retry/dead-letter copies (consumers deduplicate on it).
    """

    KIND_EVENT = "event"
    KIND_FORWARD = "forward"
    KIND_CHOICES = [(KIND_EVENT, "Event"), (KIND_FORWARD, "Retry/DLQ forward")]

    STATUS_PENDING = "pending"
    STATUS_PUBLISHED = "published"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PUBLISHED, "Published"),
        # A permanent error (e.g. the broker rejected the message); needs
        # `manage.py outbox requeue` after the cause is fixed.
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_id = models.UUIDField(db_index=True)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_EVENT)
    event_type = models.CharField(max_length=100)
    topic = models.CharField(max_length=200)
    key = models.CharField(max_length=200, blank=True, default="")
    aggregate_type = models.CharField(max_length=50, blank=True, default="")
    aggregate_id = models.CharField(max_length=100, blank=True, default="")
    payload = models.JSONField(encoder=DjangoJSONEncoder)
    headers = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempts = models.PositiveIntegerField(default=0)
    # Earliest time of the next publish attempt (backoff after a failure).
    available_at = models.DateTimeField(default=timezone.now)
    # A publisher's claim on the row; expired claims are taken over.
    locked_until = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=100, blank=True, default="")
    last_error = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    kafka_partition = models.IntegerField(null=True, blank=True)
    kafka_offset = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "apps_outbox_event"
        ordering = ["created_at"]
        indexes = [
            # The publisher's work queue: pending rows by due time.
            models.Index(
                fields=["available_at", "created_at"],
                name="outbox_pending_due_idx",
                condition=Q(status="pending"),
            ),
            models.Index(fields=["status", "created_at"], name="outbox_status_created_idx"),
        ]
        constraints = [
            # One outbox row per business event (forwards repeat event_id).
            models.UniqueConstraint(
                fields=["event_id"], condition=Q(kind="event"), name="outbox_unique_event_id"
            ),
        ]

    def __str__(self):
        return f"{self.event_type} {self.event_id} -> {self.topic} ({self.status})"


class ProcessedEvent(models.Model):
    """An event a consumer group has fully processed (idempotency record).

    Written after the task succeeded and before the Kafka offset is
    committed. A redelivered event whose record exists is acknowledged
    without running the task again.
    """

    consumer_group = models.CharField(max_length=100)
    event_id = models.UUIDField()
    event_type = models.CharField(max_length=100)
    task = models.CharField(max_length=100)
    topic = models.CharField(max_length=200, blank=True, default="")
    partition = models.IntegerField(null=True, blank=True)
    offset = models.BigIntegerField(null=True, blank=True)
    attempt = models.PositiveIntegerField(default=1)
    duration_ms = models.PositiveIntegerField(default=0)
    processed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "apps_processed_event"
        constraints = [
            models.UniqueConstraint(fields=["consumer_group", "event_id"], name="processed_event_unique"),
        ]

    def __str__(self):
        return f"{self.consumer_group}: {self.event_type} {self.event_id}"


class DeadLetterEvent(models.Model):
    """A dead-lettered task, archived from the DLQ topic for operators.

    The Kafka DLQ topic stays the record of what was dead-lettered; this
    table makes it searchable and tracks the replay workflow
    (``manage.py kafka_dlq``). One row per (consumer group, event).
    """

    STATUS_PENDING = "pending"
    STATUS_REPLAYED = "replayed"
    STATUS_DISCARDED = "discarded"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_REPLAYED, "Replayed"),
        (STATUS_DISCARDED, "Discarded"),
    ]

    event_id = models.UUIDField()
    consumer_group = models.CharField(max_length=100)
    task = models.CharField(max_length=100, blank=True, default="")
    event_type = models.CharField(max_length=100)
    aggregate_id = models.CharField(max_length=100, blank=True, default="")
    reason = models.CharField(max_length=30)
    error_category = models.CharField(max_length=30, blank=True, default="")
    error_message = models.CharField(max_length=500, blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    original_topic = models.CharField(max_length=200, blank=True, default="")
    original_partition = models.IntegerField(null=True, blank=True)
    original_offset = models.BigIntegerField(null=True, blank=True)
    first_failed_at = models.DateTimeField(null=True, blank=True)
    last_failed_at = models.DateTimeField(null=True, blank=True)
    # The DLQ message (original event + failure metadata).
    envelope = models.JSONField(encoder=DjangoJSONEncoder)
    dlq_partition = models.IntegerField(null=True, blank=True)
    dlq_offset = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    replay_count = models.PositiveIntegerField(default=0)
    replayed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "apps_dead_letter_event"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["consumer_group", "event_id"], name="dead_letter_unique"),
        ]

    def __str__(self):
        return f"DLQ {self.consumer_group}: {self.event_type} {self.event_id} ({self.status})"


class AuditEvent(models.Model):
    """A non-critical audit record (logins, admin product changes, order and
    payment lifecycle), written by the audit consumer group."""

    event_id = models.UUIDField(unique=True)
    event_type = models.CharField(max_length=100, db_index=True)
    actor_user_id = models.IntegerField(null=True, blank=True, db_index=True)
    aggregate_type = models.CharField(max_length=50, blank=True, default="")
    aggregate_id = models.CharField(max_length=100, blank=True, default="")
    data = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    correlation_id = models.CharField(max_length=64, blank=True, default="")
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "apps_audit_event"
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["aggregate_type", "aggregate_id"])]

    def __str__(self):
        return f"{self.event_type} {self.aggregate_type}:{self.aggregate_id}"


class ConsumerHeartbeat(models.Model):
    """Liveness and counters of one worker or publisher process, for health
    checks (each process is separate, so in-process counters can't be read
    by the web instances)."""

    member = models.CharField(max_length=200, unique=True)  # "<role>@<host>:<pid>"
    role = models.CharField(max_length=100)  # consumer group id, or "outbox-publisher"
    hostname = models.CharField(max_length=100)
    pid = models.IntegerField()
    started_at = models.DateTimeField()
    last_seen = models.DateTimeField(db_index=True)
    assignment = models.JSONField(default=list, blank=True)
    stats = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "apps_consumer_heartbeat"
        ordering = ["role", "member"]

    def __str__(self):
        return self.member
