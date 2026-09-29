"""Authentication domain helpers shared with other modules."""


def get_user_role(user):
    """Return the role name for ``user`` (e.g. "admin"), or None.

    Accepts a Django ``User`` (role comes from ``user.profile.role``) or any
    principal that already exposes a ``role`` string.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    role = getattr(user, "role", None)
    if isinstance(role, str):
        return role
    profile = getattr(user, "profile", None)
    role = getattr(profile, "role", None) if profile is not None else None
    return role.name if role is not None else None
