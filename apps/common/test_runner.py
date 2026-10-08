from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class TestRunner(DiscoverRunner):
    """Runs the suite without Redis, as before Redis was added: Django's cache
    is in-process memory and apps.common.cache is off (every read goes to
    PostgreSQL). Cache tests opt in with
    ``apps.common.testing.RedisCacheTestMixin``."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._no_redis = override_settings(
            CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
            CACHE_ENABLED=False,
        )
        self._no_redis.enable()

    def teardown_test_environment(self, **kwargs):
        self._no_redis.disable()
        super().teardown_test_environment(**kwargs)
