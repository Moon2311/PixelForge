import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

logger = logging.getLogger(__name__)


def build_password_reset_link(user):
    query = urlencode(
        {
            "uid": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": default_token_generator.make_token(user),
        }
    )
    return f"{settings.PASSWORD_RESET_URL}?{query}"


def send_password_reset_email(user):
    minutes = settings.PASSWORD_RESET_TIMEOUT // 60
    body = (
        f"Hi {user.first_name or user.username},\n\n"
        "We received a request to reset your PixelForge password. "
        "Use the link below to choose a new one:\n\n"
        f"{build_password_reset_link(user)}\n\n"
        f"This link expires in {minutes} minutes and can only be used once. "
        "If you didn't request a reset, you can ignore this email.\n"
    )
    try:
        send_mail("Reset your PixelForge password", body, None, [user.email])
    except Exception:
        # Never surface delivery failures to the caller; that would reveal
        # whether the account exists.
        logger.exception("Failed to send password reset email to user %s", user.pk)


def send_password_changed_email(user):
    body = (
        f"Hi {user.first_name or user.username},\n\n"
        "Your PixelForge password was just changed. If this wasn't you, "
        "reset your password immediately and contact support.\n"
    )
    try:
        send_mail("Your PixelForge password was changed", body, None, [user.email])
    except Exception:
        logger.exception("Failed to send password changed email to user %s", user.pk)
