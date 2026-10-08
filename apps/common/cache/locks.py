"""Redis mutex used to rebuild one cache entry at a time (stampede protection).

Acquire is ``SET <lock key> <token> NX PX <ttl>``: atomic, held by at most
one request, and always expiring, so a worker that dies mid-rebuild can't
deadlock the key. The token is random per acquisition and release deletes
the lock only if it still holds that token (checked atomically in Lua). A
request whose lock already expired can therefore never delete the lock a
later request now holds.
"""

import uuid

from django.conf import settings

from apps.common.cache import client

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


def acquire_lock(key, ttl=None):
    """Return the owner token, or None if the lock is held or Redis is unavailable.

    ``ttl`` (seconds, may be fractional) defaults to ``CACHE_LOCK_TTL``.
    """
    ttl = settings.CACHE_LOCK_TTL if ttl is None else ttl
    token = uuid.uuid4().hex
    try:
        acquired = client.run(
            "lock acquire",
            lambda r: r.set(key, token, nx=True, px=max(1, int(ttl * 1000))),
        )
    except client.CacheUnavailable:
        return None
    return token if acquired else None


def release_lock(key, token):
    """Release the lock if ``token`` still owns it. Returns True if it was released."""
    try:
        released = client.run(
            "lock release",
            lambda r: r.register_script(_RELEASE_SCRIPT)(keys=[key], args=[token]),
        )
    except client.CacheUnavailable:
        return False  # the lock expires on its own
    return bool(released)
