"""Cache-aside on Redis with stampede, penetration and avalanche protection.

    get_or_set(
        build_key("product", product_id),
        lambda: product_store.get_product(product_id),
        policy=CachePolicy(ttl=300, jitter=30),
        not_found=ProductDoesNotExist,
    )

* **Cache-aside:** read Redis; on a miss load from PostgreSQL and store it.
* **Stampede:** on a miss only the request holding the key's mutex loads
  from PostgreSQL. It re-checks Redis after acquiring the lock (a previous
  holder may have just filled the key). The others wait with backoff and
  then read the fresh entry, or load it themselves after
  ``CACHE_LOCK_WAIT_TIMEOUT``.
* **Penetration:** when the loader raises one of ``not_found``, a short-lived
  "does not exist" marker is stored and the same exception is raised again
  on later reads without querying PostgreSQL. Other errors (e.g. the
  database being down) are never cached.
* **Avalanche:** every TTL is ``ttl + random(0, jitter)``, so entries
  written together don't all expire in the same second.
* **Fence (optional):** ``fence=<redis key>`` stores a rebuilt value only if
  that key still holds what it held before the loader ran (atomic
  compare-and-set). Writers change the fence before invalidating, so a
  rebuild that read old data (e.g. from a lagging read replica) is returned
  but never cached after the invalidation. See apps.common.db.consistency.

Redis values are JSON envelopes, so a miss (no key), a cached ``None`` and a
cached "not found" can't be confused::

    {"v": <value>}       cached value
    {"nf": "<message>"}  cached "does not exist"

Values are encoded with DRF's JSON encoder, the one the API's JSONRenderer
uses, so a cached response renders exactly like a fresh one (Decimal,
datetime and UUID included). Cache plain dicts/lists, not model instances:
a value that can't be encoded is returned but not cached.

If Redis is unavailable every function falls back to "no cache": reads
miss, writes are skipped and ``get_or_set`` calls the loader.
"""

import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from rest_framework.utils.encoders import JSONEncoder

from apps.common.cache import client, keys, locks, metrics

logger = logging.getLogger("apps.cache")

_MISS = object()


class _NotFoundMarker:
    def __repr__(self):
        return "CACHE_NOT_FOUND"


# Returned by ``get`` for a cached "does not exist" entry.
CACHE_NOT_FOUND = _NotFoundMarker()


@dataclass(frozen=True)
class CachePolicy:
    """TTLs (seconds) for one kind of data. None means the settings default:
    CACHE_DEFAULT_TTL, CACHE_TTL_JITTER, NEGATIVE_CACHE_TTL and
    NEGATIVE_CACHE_TTL_JITTER. ``negative_ttl=0`` disables negative caching."""

    ttl: Optional[int] = None
    jitter: Optional[int] = None
    negative_ttl: Optional[int] = None
    negative_jitter: Optional[int] = None

    def value_ttl(self):
        return jittered_ttl(
            settings.CACHE_DEFAULT_TTL if self.ttl is None else self.ttl,
            settings.CACHE_TTL_JITTER if self.jitter is None else self.jitter,
        )

    def not_found_ttl(self):
        base = settings.NEGATIVE_CACHE_TTL if self.negative_ttl is None else self.negative_ttl
        if base <= 0:
            return None
        return jittered_ttl(
            base,
            settings.NEGATIVE_CACHE_TTL_JITTER if self.negative_jitter is None else self.negative_jitter,
        )


DEFAULT_POLICY = CachePolicy()


def jittered_ttl(base, jitter):
    """``base + random(0, jitter)`` seconds; always at least 1."""
    return max(1, int(base)) + random.randint(0, max(0, int(jitter)))


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def _encode(envelope, key):
    try:
        return json.dumps(envelope, cls=JSONEncoder, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        metrics.incr("cache_errors_total")
        logger.error("Not caching %s: value is not JSON-serializable (%s)", key, exc)
        return None


def _decode(raw, key):
    """``(value, None)``, ``(CACHE_NOT_FOUND, message)``, or ``(_MISS, None)``
    for a missing or unreadable entry."""
    if raw is None:
        return _MISS, None
    try:
        envelope = json.loads(raw)
        if "v" in envelope:
            return envelope["v"], None
        if "nf" in envelope:
            return CACHE_NOT_FOUND, envelope["nf"]
    except (TypeError, ValueError):
        pass
    metrics.incr("cache_errors_total")
    logger.error("Ignoring unreadable cache entry %s", key)
    return _MISS, None


def _read(key):
    try:
        raw = client.run("get", lambda r: r.get(key))
    except client.CacheUnavailable:
        return _MISS, None
    return _decode(raw, key)


# KEYS[1]=entry, KEYS[2]=fence, ARGV[1]=payload, ARGV[2]=TTL, ARGV[3]=expected fence value.
_FENCED_SET_SCRIPT = """
if (redis.call("get", KEYS[2]) or "") ~= ARGV[3] then
    return 0
end
redis.call("set", KEYS[1], ARGV[1], "EX", ARGV[2])
return 1
"""


def _write(key, envelope, ttl, fence=None, expected=""):
    """Store ``envelope``. With a ``fence`` key, only if it still holds ``expected``."""
    payload = _encode(envelope, key)
    if payload is None:
        return False
    try:
        if fence is None:
            client.run("set", lambda r: r.set(key, payload, ex=ttl))
        else:
            stored = client.run(
                "fenced set",
                lambda r: r.register_script(_FENCED_SET_SCRIPT)(
                    keys=[key, fence], args=[payload, ttl, expected]
                ),
            )
            if not stored:
                metrics.incr("cache_fenced_skips_total")
                logger.debug("cache not stored %s: changed while it was loading", key)
                return False
    except client.CacheUnavailable:
        return False
    metrics.incr("cache_sets_total")
    logger.debug("cache set %s (ttl %ss)", key, ttl)
    return True


# ---------------------------------------------------------------------------
# Basic operations
# ---------------------------------------------------------------------------


def get(key, default=None):
    """The cached value, CACHE_NOT_FOUND for a negative entry, or ``default`` on a miss."""
    value, _ = _read(key)
    return default if value is _MISS else value


def set(key, value, ttl=None, jitter=None):  # noqa: A001 (mirrors the cache API)
    """Store ``value`` for ``ttl + random(0, jitter)`` seconds. Returns True if stored."""
    return _write(key, {"v": value}, CachePolicy(ttl=ttl, jitter=jitter).value_ttl())


def set_not_found(key, message="", ttl=None, jitter=None):
    """Store a "does not exist" marker (negative cache entry)."""
    ttl = CachePolicy(negative_ttl=ttl, negative_jitter=jitter).not_found_ttl()
    return ttl is not None and _write(key, {"nf": message}, ttl)


def exists(key):
    try:
        return bool(client.run("exists", lambda r: r.exists(key)))
    except client.CacheUnavailable:
        return False


def delete(*keys_to_delete):
    """Delete keys (values and negative entries alike). Returns how many existed."""
    keys_to_delete = [k for k in keys_to_delete if k]
    if not keys_to_delete:
        return 0
    deleted = 0
    try:
        for start in range(0, len(keys_to_delete), 500):
            chunk = keys_to_delete[start:start + 500]
            deleted += client.run("delete", lambda r, chunk=chunk: r.delete(*chunk))
    except client.CacheUnavailable:
        logger.warning(
            "Could not invalidate %s cache key(s); they expire within their TTL",
            len(keys_to_delete),
        )
    metrics.incr("cache_deletes_total", deleted)
    logger.info("cache invalidated %s key(s), e.g. %s", len(keys_to_delete), keys_to_delete[0])
    return deleted


def delete_prefix(*parts):
    """Delete every key under ``build_key(*parts)``. Uses SCAN (never KEYS),
    so Redis isn't blocked, but it walks the whole keyspace: for maintenance
    commands only, never per request."""
    pattern = keys.build_key(*parts) + ":*"
    try:
        found = client.run("scan", lambda r: list(r.scan_iter(match=pattern, count=1000)))
    except client.CacheUnavailable:
        return 0
    return delete(*(key.decode() for key in found)) if found else 0


def acquire_lock(key, ttl=None):
    return locks.acquire_lock(key, ttl)


def release_lock(key, token):
    return locks.release_lock(key, token)


# ---------------------------------------------------------------------------
# Cache-aside with stampede protection
# ---------------------------------------------------------------------------


def _lookup(key, not_found, count_miss=True):
    """The cached value, or _MISS. A negative entry raises ``not_found``."""
    value, message = _read(key)
    if value is CACHE_NOT_FOUND:
        if not not_found:
            return _MISS  # no exception to raise: treat as a miss
        metrics.incr("cache_negative_hits_total")
        logger.debug("cache negative hit %s", key)
        raise not_found[0](message)
    if value is _MISS:
        if count_miss:
            metrics.incr("cache_misses_total")
            logger.debug("cache miss %s", key)
        return _MISS
    metrics.incr("cache_hits_total")
    logger.debug("cache hit %s", key)
    return value


def _fence_value(fence):
    """The fence's current value ("" if unset), or None if Redis can't be read."""
    try:
        raw = client.run("fence read", lambda r: r.get(fence))
    except client.CacheUnavailable:
        return None
    return raw.decode() if isinstance(raw, bytes) else (raw or "")


def _rebuild(key, loader, policy, not_found, fence=None):
    metrics.incr("cache_rebuild_total")
    store = {}
    if fence is not None:
        # Read before the loader runs: a change during the load blocks the store.
        expected = _fence_value(fence)
        store = {"fence": fence, "expected": expected}
        if expected is None:
            return loader()  # Redis failed: nothing could be stored anyway
    try:
        value = loader()
    except not_found as exc:
        ttl = policy.not_found_ttl()
        if ttl is not None:
            _write(key, {"nf": str(exc)}, ttl, **store)
        raise
    _write(key, {"v": value}, policy.value_ttl(), **store)
    return value


def get_or_set(key, loader, policy=DEFAULT_POLICY, not_found=(), fence=None):
    """Return the cached value for ``key``, loading it with ``loader()`` on a miss.

    ``not_found``: exception class(es) meaning "does not exist". They are
    negative-cached and re-raised (as the first class, with the original
    message) on later reads. Any other exception propagates uncached.

    ``fence``: optional Redis key; the loaded value is stored only if the
    fence is unchanged since the load started (see the module docstring).
    """
    if not isinstance(not_found, tuple):
        not_found = (not_found,)
    if not client.available():
        return loader()

    value = _lookup(key, not_found)
    if value is not _MISS:
        return value

    lock = keys.lock_key(key)
    deadline = time.monotonic() + settings.CACHE_LOCK_WAIT_TIMEOUT
    delay = settings.CACHE_LOCK_RETRY_DELAY
    waited = False
    while True:
        token = locks.acquire_lock(lock)
        if token:
            metrics.incr("cache_lock_acquired_total")
            try:
                # Re-check: whoever held the lock before may have just stored it.
                value = _lookup(key, not_found, count_miss=False)
                if value is not _MISS:
                    return value
                return _rebuild(key, loader, policy, not_found, fence)
            finally:
                locks.release_lock(lock, token)

        if not client.available():
            return loader()  # Redis failed: no cache and no lock to wait for
        if not waited:
            waited = True
            metrics.incr("cache_lock_contention_total")
            logger.debug("cache lock busy %s; waiting for the rebuild", key)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            metrics.incr("cache_lock_timeout_total")
            logger.warning(
                "Waited %ss for the cache rebuild of %s; loading from PostgreSQL",
                settings.CACHE_LOCK_WAIT_TIMEOUT, key,
            )
            return _rebuild(key, loader, policy, not_found)
        # Backoff with +-20% jitter so waiters don't poll Redis in lockstep.
        time.sleep(min(delay * random.uniform(0.8, 1.2), remaining))
        delay = min(delay * 2, settings.CACHE_LOCK_RETRY_MAX_DELAY)

        value = _lookup(key, not_found, count_miss=False)
        if value is not _MISS:
            return value
