"""Idempotent email delivery for background tasks.

``deliver()`` sends one email at most once per ``dedup_key``:

    created/pending/failed --claim--> sending --send ok--> sent
                                         └──send error──> failed (task retried)

A claim is a conditional UPDATE, so of two workers processing the same
event at the same time only one sends; the other gets ``TaskInProgress``
and retries later (by then the notification is sent and it does nothing).

Limit: if a worker dies after the SMTP server accepted the message but
before the row is marked sent, the claim expires and the email is sent
again. SMTP has no idempotency key, so this narrow window can't be closed;
every other duplicate (redelivery, duplicate publish, replays) is.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.authentication.models import UserProfile
from apps.events.retry import TaskInProgress, sanitize_error

from .models import Notification

logger = logging.getLogger("apps.notifications")


def claim_seconds():
    # Longer than any task may run, so a live sender never loses its claim.
    return settings.EVENTS_TASK_TIMEOUT + 30


def deliver(*, dedup_key, kind, recipient, build, user_id=None, event_id=None):
    """Send the email ``build()`` returns as ``(subject, body)``, once.

    Returns the ``Notification``. Raises ``TaskInProgress`` while another
    worker holds the claim, and re-raises send errors (the task's retry
    policy decides what happens next).
    """
    notification = _get_or_create(dedup_key, kind, recipient, user_id, event_id)
    if notification.status == Notification.STATUS_SENT:
        return notification

    now = timezone.now()
    claimed = (
        Notification.objects.filter(pk=notification.pk)
        .filter(
            Q(status__in=[Notification.STATUS_PENDING, Notification.STATUS_FAILED])
            | Q(status=Notification.STATUS_SENDING, claimed_until__lt=now)
        )
        .update(status=Notification.STATUS_SENDING, claimed_until=now + timedelta(seconds=claim_seconds()),
                attempts=F("attempts") + 1)
    )
    if not claimed:
        notification.refresh_from_db()
        if notification.status == Notification.STATUS_SENT:
            return notification
        raise TaskInProgress(f"notification {notification.pk} is being sent by another worker")

    subject, body = build()
    try:
        send_mail(subject, body, None, [recipient], fail_silently=False)
    except Exception as exc:
        Notification.objects.filter(pk=notification.pk).update(
            status=Notification.STATUS_FAILED, claimed_until=None, last_error=sanitize_error(exc),
            subject=subject[:255],
        )
        raise
    Notification.objects.filter(pk=notification.pk).update(
        status=Notification.STATUS_SENT, sent_at=timezone.now(), claimed_until=None, last_error="",
        subject=subject[:255],
    )
    logger.info("Sent %s notification %s", kind, notification.pk)
    notification.refresh_from_db()
    return notification


def _get_or_create(dedup_key, kind, recipient, user_id, event_id):
    try:
        with transaction.atomic():
            notification, _ = Notification.objects.get_or_create(
                dedup_key=dedup_key[:200],
                defaults={"kind": kind, "recipient": recipient, "user_id": user_id, "event_id": event_id},
            )
    except IntegrityError:
        notification = Notification.objects.get(dedup_key=dedup_key[:200])
    return notification


def user_email(user_id):
    return (
        User.objects.filter(pk=user_id, is_active=True).exclude(email="")
        .values_list("email", flat=True).first()
    )


def staff_recipients():
    """Operational alerts: active users with ALERT_ROLES, plus ALERT_EMAILS."""
    users = (
        UserProfile.objects.filter(role__name__in=settings.ALERT_ROLES, user__is_active=True)
        .exclude(user__email="")
        .values_list("user_id", "user__email")
    )
    recipients = {email.lower(): user_id for user_id, email in users}
    for email in settings.ALERT_EMAILS:
        recipients.setdefault(email.strip().lower(), None)
    return sorted(recipients.items())
