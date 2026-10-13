from django.db import models


class Notification(models.Model):
    """One message to one recipient, sent at most once per ``dedup_key``.

    ``dedup_key`` is derived from the event id, the kind and the recipient,
    so a redelivered or duplicated event finds its notification already
    sent. Before sending, a worker claims the row (``status=sending`` with a
    lease) so two workers handling the same event at once don't both send.
    The body is built at send time and never stored (it may hold a password
    reset link).
    """

    STATUS_PENDING = "pending"
    STATUS_SENDING = "sending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SENDING, "Sending"),
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Failed"),
    ]

    CHANNEL_EMAIL = "email"

    dedup_key = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=60, db_index=True)
    channel = models.CharField(max_length=20, default=CHANNEL_EMAIL)
    user_id = models.IntegerField(null=True, blank=True, db_index=True)
    recipient = models.CharField(max_length=254)
    subject = models.CharField(max_length=255, blank=True, default="")
    event_id = models.UUIDField(null=True, blank=True, db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    claimed_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "apps_notification"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} -> user {self.user_id} ({self.status})"
