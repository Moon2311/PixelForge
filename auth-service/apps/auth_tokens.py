from django.core.signing import TimestampSigner

TOKEN_MAX_AGE = 60 * 60 * 8  # 8 hours
SALT = "pixelforge.access"


def issue_access_token(user):
    """Return a signed, expiring token for the given user.

    The token embeds user id, role name and staff flag so that other
    services can authorize the request without a shared user store.
    """
    role = getattr(getattr(user, "profile", None), "role", None)
    role_name = role.name if role else ""
    payload = f"{user.pk}:{role_name}:{'1' if user.is_staff else '0'}"
    return TimestampSigner(key=settings_secret(), salt=SALT).sign(payload)


def settings_secret():
    from django.conf import settings

    return settings.SHARED_AUTH_SECRET
