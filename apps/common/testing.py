"""Test helpers for code that uses apps.common.cache against a real Redis."""

import os
import unittest

from django.test.utils import override_settings
from django_redis import get_redis_connection
from redis.exceptions import RedisError

from apps.common.cache import client, metrics

# A dedicated database; tests only delete keys under TEST_KEY_PREFIX.
REDIS_TEST_URL = os.environ.get("REDIS_TEST_URL", "redis://127.0.0.1:6379/15")
TEST_KEY_PREFIX = "pixelforge-test"


def redis_cache_settings(location=REDIS_TEST_URL, **overrides):
    return override_settings(
        CACHES={
            "default": {
                "BACKEND": "django_redis.cache.RedisCache",
                "LOCATION": location,
                "OPTIONS": {
                    "CLIENT_CLASS": "django_redis.client.DefaultClient",
                    "SOCKET_CONNECT_TIMEOUT": 0.25,
                    "SOCKET_TIMEOUT": 0.25,
                    "IGNORE_EXCEPTIONS": True,
                },
            }
        },
        CACHE_ENABLED=True,
        CACHE_KEY_PREFIX=TEST_KEY_PREFIX,
        **overrides,
    )


def flush_test_keys():
    redis = get_redis_connection("default")
    found = list(redis.scan_iter(match=f"{TEST_KEY_PREFIX}:*", count=1000))
    if found:
        redis.delete(*found)


class RedisCacheTestMixin:
    """Runs the test class with caching on, against REDIS_TEST_URL.

    The class is skipped when that Redis isn't reachable, e.g.
    ``docker compose up redis`` or a local ``redis-server`` provides it.
    """

    @classmethod
    def setUpClass(cls):
        cls._redis_settings = redis_cache_settings()
        cls._redis_settings.enable()
        try:
            get_redis_connection("default").ping()
        except RedisError:
            cls._redis_settings.disable()
            raise unittest.SkipTest("Redis not reachable; set REDIS_TEST_URL to run cache tests") from None
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._redis_settings.disable()

    def setUp(self):
        super().setUp()
        flush_test_keys()
        client.reset()
        metrics.reset()
        self.addCleanup(flush_test_keys)
        self.addCleanup(client.reset)
