import copy

from django.db import connections
from django.test.runner import DiscoverRunner
from django.test.utils import override_settings

from apps.common.db import PRIMARY

# Reader aliases the tests can use: one replica, or a pool of three.
TEST_REPLICA_ALIASES = ("replica", "replica_1", "replica_2", "replica_3")


class TestRunner(DiscoverRunner):
    """Runs the suite without Redis, as before Redis was added: Django's cache
    is in-process memory and apps.common.cache is off (every read goes to
    PostgreSQL). Cache tests opt in with
    ``apps.common.testing.RedisCacheTestMixin``.

    Read-replica routing is off too (every read on the primary, as before).
    The ``TEST_REPLICA_ALIASES`` always exist as test mirrors of the
    primary's test database, so routing tests
    (``apps.common.testing.ReplicaRoutingTestMixin``) can turn routing on
    and see which connection served each query."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._no_redis = override_settings(
            CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
            CACHE_ENABLED=False,
            READ_REPLICA_ENABLED=False,
        )
        self._no_redis.enable()
        for alias in TEST_REPLICA_ALIASES:
            if alias not in connections.settings:
                mirror = copy.deepcopy(connections.settings[PRIMARY])
                mirror["TEST"] = {**mirror["TEST"], "MIRROR": PRIMARY}
                connections.settings[alias] = mirror

    def teardown_test_environment(self, **kwargs):
        self._no_redis.disable()
        super().teardown_test_environment(**kwargs)
