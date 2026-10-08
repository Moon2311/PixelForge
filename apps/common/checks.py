from django.conf import settings
from django.core.checks import Error, Warning, register

from apps.common.db import replica


@register()
def cache_backend_check(app_configs, **kwargs):
    """apps.common.cache needs django-redis: fail at startup, not on the first request."""
    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    if settings.CACHE_ENABLED and backend != "django_redis.cache.RedisCache":
        return [
            Error(
                "CACHE_ENABLED is True but CACHES['default'] is not django-redis.",
                hint="Use django_redis.cache.RedisCache, or set CACHE_ENABLED=False.",
                id="common.E001",
            )
        ]
    return []


@register()
def read_replica_check(app_configs, **kwargs):
    if not replica.configured():
        return []
    warnings = []
    window = settings.REPLICA_MAX_LAG + settings.REPLICA_HEALTH_INTERVAL
    if settings.RECENT_WRITE_TTL <= window:
        warnings.append(Warning(
            f"RECENT_WRITE_TTL ({settings.RECENT_WRITE_TTL}s) should exceed REPLICA_MAX_LAG + "
            f"REPLICA_HEALTH_INTERVAL ({window:g}s).",
            hint="Otherwise a write can be forgotten while a lagging replica still serves reads.",
            id="common.W002",
        ))
    if not settings.CACHE_ENABLED:
        warnings.append(Warning(
            "A read replica is configured but CACHE_ENABLED is False.",
            hint="Read-your-writes markers live in Redis: without it every request with an "
                 "identity (Bearer token, guest session, admin session) reads the primary.",
            id="common.W003",
        ))
    return warnings
