"""Shared token authentication — copied from search-service to avoid cross-service import.

Both services must share the same SHARED_AUTH_SECRET and SALT.
"""

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

SALT = "pixelforge.access"
TOKEN_MAX_AGE = 60 * 60 * 8  # 8 hours


class AuthUser:
    """Lightweight authenticated principal carried by a valid shared token."""

    def __init__(self, user_id, role, is_staff):
        self.pk = user_id
        self.id = user_id
        self.username = str(user_id)
        self.role = role
        self.is_staff = is_staff
        self.is_authenticated = True

    def __str__(self):
        return f"AuthUser({self.pk}, {self.role})"


def decode_access_token(token):
    """Return an AuthUser for a valid, non-expired token, else None."""
    try:
        payload = TimestampSigner(key=settings.SHARED_AUTH_SECRET, salt=SALT).unsign(
            token, max_age=TOKEN_MAX_AGE
        )
    except (BadSignature, SignatureExpired):
        return None

    parts = payload.split(":")
    if len(parts) != 3 or not parts[0].isdigit():
        return None
    return AuthUser(int(parts[0]), parts[1], parts[2] == "1")


class SharedTokenAuthentication:
    """DRF authentication backend for the shared cross-service access token."""

    keyword = "Bearer"
    www_authenticate_realm = "api"

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header:
            return None
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != self.keyword.lower():
            return None
        user = decode_access_token(parts[1])
        if user is None:
            return None
        return (user, parts[1])

    def authenticate_header(self, request):
        return f'{self.keyword} realm="{self.www_authenticate_realm}"'
