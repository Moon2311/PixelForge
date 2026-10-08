"""Application cache on Redis (PostgreSQL stays the source of truth).

Use this package instead of Redis directly::

    from apps.common import cache

    cache.get_or_set(cache.build_key("product", 42), loader, policy=..., not_found=...)

Modules: ``manager`` (cache-aside, negative caching, TTL jitter),
``locks`` (rebuild mutex), ``keys`` (key naming), ``client`` (Redis
connection and failure handling), ``metrics`` (counters).
"""

from apps.common.cache.keys import build_key, lock_key
from apps.common.cache.manager import (
    CACHE_NOT_FOUND,
    DEFAULT_POLICY,
    CachePolicy,
    acquire_lock,
    delete,
    delete_prefix,
    exists,
    get,
    get_or_set,
    jittered_ttl,
    release_lock,
    set,
    set_not_found,
)

__all__ = [
    "CACHE_NOT_FOUND",
    "DEFAULT_POLICY",
    "CachePolicy",
    "acquire_lock",
    "build_key",
    "delete",
    "delete_prefix",
    "exists",
    "get",
    "get_or_set",
    "jittered_ttl",
    "lock_key",
    "release_lock",
    "set",
    "set_not_found",
]
