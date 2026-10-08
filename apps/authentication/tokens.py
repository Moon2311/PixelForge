"""Bearer access tokens and refresh tokens for the API.

Login returns a short-lived access token and a longer-lived refresh token,
both signed with ``SECRET_KEY`` (under different salts, so one can't be
used as the other). The access token carries only the user id; every
request resolves it back to the real ``User`` row, so role changes and
deactivation take effect immediately. ``POST /api/auth/refresh/`` trades a
refresh token for a new pair. The refresh token also carries a fingerprint
of the password hash, so changing the password invalidates it.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.utils.crypto import constant_time_compare, salted_hmac

SALT = "pixelforge.access"
REFRESH_SALT = "pixelforge.refresh"


def _signer(salt=SALT):
    return TimestampSigner(salt=salt)


def _active_user(user_id):
    if not user_id.isdigit():
        return None
    User = get_user_model()
    return (
        User.objects.select_related("profile__role")
        .filter(pk=int(user_id), is_active=True)
        .first()
    )


def issue_access_token(user):
    """Return a signed, expiring access token for ``user``."""
    return _signer().sign(str(user.pk))


def user_id_from_access_token(token):
    """The user id in a valid, unexpired token, else None. No database
    query: the user may since have been deactivated (see user_from_access_token)."""
    try:
        user_id = _signer().unsign(token, max_age=settings.ACCESS_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return int(user_id) if user_id.isdigit() else None


def user_from_access_token(token):
    """Return the active User for a valid, unexpired token, else None."""
    user_id = user_id_from_access_token(token)
    return None if user_id is None else _active_user(str(user_id))


def _password_fingerprint(user):
    return salted_hmac(REFRESH_SALT, user.password).hexdigest()[:16]


def issue_refresh_token(user):
    """Return a signed, expiring refresh token for ``user``."""
    value = f"{user.pk}:{_password_fingerprint(user)}"
    return _signer(REFRESH_SALT).sign(value)


def user_from_refresh_token(token):
    """Return the active User for a valid, unexpired refresh token, else None.

    Tokens issued before the user's last password change are rejected.
    """
    try:
        value = _signer(REFRESH_SALT).unsign(
            token, max_age=settings.REFRESH_TOKEN_MAX_AGE
        )
    except (BadSignature, SignatureExpired):
        return None
    user_id, _, fingerprint = value.partition(":")
    user = _active_user(user_id)
    if user is None or not constant_time_compare(
        fingerprint, _password_fingerprint(user)
    ):
        return None
    return user


def issue_token_pair(user):
    return {
        "access_token": issue_access_token(user),
        "refresh_token": issue_refresh_token(user),
    }
