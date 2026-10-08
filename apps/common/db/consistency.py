"""Read-your-writes on top of asynchronous replication, using commit LSNs.

After a request's write commits, the primary's WAL position (LSN) is stored
in Redis under the caller's identity::

    pixelforge:rw:user:7          -> "000000000301A2B8"
    pixelforge:rw:guest:<sha256>  -> ...
    pixelforge:rw:session:<sha256>

A later read by the same caller may use the replica only once the replica
has replayed that LSN (``replica.has_replayed``); until then it reads the
primary. ``RECENT_WRITE_TTL`` is only how long the marker is remembered, not
an assumption that the replica has caught up: the replica is taken out of
rotation when it lags more than ``REPLICA_MAX_LAG`` (< the TTL, see the
``common.W002`` check), so an expired marker can't point at a replica that
is still missing the write.

Catalog cache refills use a second, shared marker (``catalog_fence_key``):
every committed catalog change raises it *before* the cache keys are
deleted, refills read only from a replica that has replayed it, and a refill
is stored only if the marker hasn't changed since the refill started (an
atomic compare-and-set in apps.common.cache). A refill that read old data
can therefore never be stored after the invalidation.

Without Redis no marker can be read: every read that has an identity, and
every cache refill, uses the primary. Reads are never served from a replica
that might be missing the caller's write.
"""

import hashlib
import logging
import uuid

from django.conf import settings
from django.db import DatabaseError

from apps.common import cache
from apps.common.cache import client as cache_client
from apps.common.db import metrics, replica

logger = logging.getLogger("apps.db")

# KEYS[1]=marker, ARGV[1]=LSN (16 hex digits), ARGV[2]=suffix, ARGV[3]=TTL.
# Keeps the highest LSN (equal-length hex strings compare like numbers), so
# concurrent writers can't move the marker backwards.
_RAISE_SCRIPT = """
local current = redis.call("get", KEYS[1])
local lsn = ARGV[1]
if current and string.sub(current, 1, 16) > lsn then
    lsn = string.sub(current, 1, 16)
end
redis.call("set", KEYS[1], lsn .. ARGV[2], "EX", ARGV[3])
return lsn
"""


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def request_key(request):
    """The read-your-writes identity of a request, from its headers only (no
    database query): ``("user", id)`` for a valid Bearer token, else
    ``("guest", digest)`` for X-Guest-Session, else ``("session", digest)``
    for a Django session cookie (admin), else None."""
    from apps.authentication.tokens import user_id_from_access_token

    parts = request.headers.get("Authorization", "").split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        user_id = user_id_from_access_token(parts[1])
        if user_id is not None:
            return ("user", str(user_id))
    guest = request.headers.get("X-Guest-Session", "").strip()
    if guest:
        return ("guest", _digest(guest))
    session = request.COOKIES.get(settings.SESSION_COOKIE_NAME)
    if session:
        return ("session", _digest(session))
    return None


def user_key(user_id):
    return ("user", str(user_id))


def marker_key(identity):
    return cache.build_key("rw", *identity)


def catalog_fence_key():
    return cache.build_key("rw", "catalog")


def _lsn_of(raw):
    if raw is None:
        return 0
    if isinstance(raw, bytes):
        raw = raw.decode()
    return int(raw[:16], 16)


def _raise(key, lsn, suffix=""):
    return cache_client.run(
        "recent-write mark",
        lambda r: r.register_script(_RAISE_SCRIPT)(
            keys=[key], args=[f"{lsn:016X}", suffix, max(1, int(settings.RECENT_WRITE_TTL))]
        ),
    )


def _read_lsn(key):
    """The LSN stored at ``key``; 0 if none; None if Redis can't be read."""
    try:
        return _lsn_of(cache_client.run("recent-write read", lambda r: r.get(key)))
    except cache_client.CacheUnavailable:
        return None


def _committed_lsn():
    try:
        return replica.primary_lsn()
    except DatabaseError as exc:
        logger.error("Could not read the primary's WAL position (%s: %s)", type(exc).__name__, exc)
        return None


def mark_recent_writes(identities):
    """Record the primary's current LSN for each identity. Call after commit."""
    identities = [i for i in identities if i]
    if not identities or not replica.configured():
        return
    lsn = _committed_lsn()
    if lsn is None:
        return
    for identity in identities:
        try:
            _raise(marker_key(identity), lsn)
        except cache_client.CacheUnavailable:
            # Its later reads can't find a marker either (Redis is down), so
            # they use the primary; nothing stale can be served.
            return
        metrics.incr("db_recent_write_marks_total")


def recent_write_lsn(identity):
    """The LSN ``identity`` must see: 0 if it hasn't written recently, None
    if unknown (Redis unavailable)."""
    return _read_lsn(marker_key(identity))


def note_catalog_write():
    """After a catalog commit, before its cache keys are deleted: raise the
    fence so in-flight refills can't store what they read, and later refills
    wait for a replica that has this write."""
    if not replica.configured():
        return
    lsn = _committed_lsn()
    if lsn is None:
        return
    try:
        _raise(catalog_fence_key(), lsn, ":" + uuid.uuid4().hex)
    except cache_client.CacheUnavailable:
        pass  # the cache delete that follows fails too; TTLs bound staleness


def catalog_write_lsn():
    """The LSN a catalog cache refill must read at least: 0, or None if unknown."""
    if not replica.configured():
        return 0
    return _read_lsn(catalog_fence_key())
