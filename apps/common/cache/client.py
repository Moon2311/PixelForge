"""The Redis connection used by apps.common.cache.

It is django-redis's client for ``CACHES["default"]``: the same connection
pool and settings as Django's cache, so there is one Redis configuration.

Redis is optional. After a Redis error, ``available()`` is False in this
worker for ``CACHE_FAILURE_COOLDOWN`` seconds, so an outage costs one
timeout and one log line per worker per cooldown instead of one per request.
"""

import logging
import threading
import time

from django.conf import settings
from django_redis import get_redis_connection
from redis.exceptions import RedisError

from apps.common.cache import metrics

logger = logging.getLogger("apps.cache")

_state_lock = threading.Lock()
_down_until = 0.0
_down = False


class CacheUnavailable(Exception):
    """Redis is disabled, cooling down after an error, or just failed."""


def get_client():
    return get_redis_connection("default")


def available():
    return settings.CACHE_ENABLED and time.monotonic() >= _down_until


def run(operation, command):
    """Run ``command(redis_client)``; raise CacheUnavailable instead of RedisError.

    Errors that aren't Redis errors (e.g. CACHES not pointing at django-redis)
    are configuration problems and propagate.
    """
    global _down
    if not available():
        raise CacheUnavailable(operation)
    try:
        result = command(get_client())
    except RedisError as exc:
        _report_failure(operation, exc)
        raise CacheUnavailable(operation) from exc
    if _down:
        with _state_lock:
            recovered, _down = _down, False
        if recovered:
            logger.info("Redis is available again; caching resumed")
    return result


def _report_failure(operation, exc):
    global _down, _down_until
    metrics.incr("cache_errors_total")
    cooldown = settings.CACHE_FAILURE_COOLDOWN
    with _state_lock:
        first = time.monotonic() >= _down_until
        _down_until = time.monotonic() + cooldown
        _down = True
    if first:
        # The exception names host:port at most, never the password in REDIS_URL.
        logger.warning(
            "Redis %s failed (%s: %s); serving from PostgreSQL without cache for %ss",
            operation, type(exc).__name__, exc, cooldown,
        )


def ping():
    """Health check: ``"ok"``, ``"disabled"`` or ``"error"``. Ignores the cooldown."""
    if not settings.CACHE_ENABLED:
        return "disabled"
    try:
        get_client().ping()
    except RedisError:
        return "error"
    return "ok"


def reset():
    """Forget past failures (tests)."""
    global _down, _down_until
    with _state_lock:
        _down_until = 0.0
        _down = False
