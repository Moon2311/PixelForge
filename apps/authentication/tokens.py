"""Bearer access tokens for the API.

Login returns a signed, expiring token (signed with ``SECRET_KEY``) that
carries only the user id. Every request resolves it back to the real
``User`` row, so role changes and deactivation take effect immediately.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

SALT = "pixelforge.access"


def _signer():
    return TimestampSigner(salt=SALT)


def issue_access_token(user):
    """Return a signed, expiring access token for ``user``."""
    return _signer().sign(str(user.pk))


def user_from_access_token(token):
    """Return the active User for a valid, unexpired token, else None."""
    try:
        user_id = _signer().unsign(token, max_age=settings.ACCESS_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not user_id.isdigit():
        return None
    User = get_user_model()
    return (
        User.objects.select_related("profile__role")
        .filter(pk=int(user_id), is_active=True)
        .first()
    )
