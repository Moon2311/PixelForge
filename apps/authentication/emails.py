"""Password emails.

The views only queue an event (``queue_*``); the ``notifications`` workers
build and send the email (``*_message``). The request no longer waits for
SMTP, and its timing no longer depends on whether the address matched an
account. The reset token is generated when the email is built, so it never
travels through Kafka or the outbox.
"""

from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.events import outbox, topics


def build_password_reset_link(user):
    query = urlencode(
        {
            "uid": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": default_token_generator.make_token(user),
        }
    )
    return f"{settings.PASSWORD_RESET_URL}?{query}"


def password_reset_message(user):
    minutes = settings.PASSWORD_RESET_TIMEOUT // 60
    body = (
        f"Hi {user.first_name or user.username},\n\n"
        "We received a request to reset your PixelForge password. "
        "Use the link below to choose a new one:\n\n"
        f"{build_password_reset_link(user)}\n\n"
        f"This link expires in {minutes} minutes and can only be used once. "
        "If you didn't request a reset, you can ignore this email.\n"
    )
    return "Reset your PixelForge password", body


def password_changed_message(user):
    body = (
        f"Hi {user.first_name or user.username},\n\n"
        "Your PixelForge password was just changed. If this wasn't you, "
        "reset your password immediately and contact support.\n"
    )
    return "Your PixelForge password was changed", body


def _queue(event_type, user):
    outbox.enqueue(
        event_type, topic=topics.NOTIFICATION_EVENTS, aggregate_type="user",
        aggregate_id=user.pk, payload={"user_id": user.pk},
    )


def queue_password_reset_email(user):
    _queue("notification.password_reset_requested", user)


def queue_password_changed_email(user):
    _queue("notification.password_changed", user)
