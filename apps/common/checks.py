from django.conf import settings
from django.core.checks import Error, register


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
