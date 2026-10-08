"""Test helpers: apps.common.cache against a real Redis, and read-replica
routing against the ``replica`` test mirror."""

import os
import unittest
from unittest import mock

from django.db import OperationalError
from django.test.utils import override_settings
from django_redis import get_redis_connection
from redis.exceptions import RedisError

from apps.common.cache import client, metrics
from apps.common.db import metrics as db_metrics
from apps.common.db import replica

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


class FakeReplica:
    """Stands in for the replica's replication state in tests.

    Queries routed to a replica really run, on its test mirror (the same
    test database), so tests see which connection served them.
    Only the replication facts are simulated: ``replay_lsn`` None means
    "caught up with the primary", an int means "replayed up to there";
    ``down`` makes every probe fail like an unreachable server.
    """

    def __init__(self):
        self.down = False
        self.in_recovery = True
        self.replay_lsn = None
        self.replay_age = 0.0

    def _replayed(self):
        return replica.primary_lsn() if self.replay_lsn is None else self.replay_lsn

    def probe(self):
        if self.down:
            raise OperationalError("could not connect to the read replica (test)")
        return {
            "in_recovery": self.in_recovery,
            "replay_lsn": self._replayed(),
            "replay_age": self.replay_age,
            "primary_lsn": replica.primary_lsn(),
        }

    def replay(self):
        if self.down:
            raise OperationalError("could not connect to the read replica (test)")
        return self._replayed()


class ReplicaRoutingTestMixin:
    """Turns read routing on against test mirrors (``replica_aliases``), with
    each replica's replication state simulated by ``self.fake_replicas[alias]``
    (``self.fake_replica`` is the first one).

    Use with TransactionTestCase: a mirror is a second connection, so it only
    sees committed data. ``set_replica(...)`` changes the simulated state and
    forgets the cached health checks.
    """

    replica_aliases = ("replica",)
    databases = {"default", "replica"}

    def setUp(self):
        super().setUp()
        self.fake_replicas = {alias: FakeReplica() for alias in self.replica_aliases}
        self.fake_replica = self.fake_replicas[self.replica_aliases[0]]
        for name, method in (("_probe", "probe"), ("_replay_lsn", "replay")):
            patcher = mock.patch(
                f"apps.common.db.replica.{name}",
                side_effect=lambda alias, method=method: getattr(self.fake_replicas[alias], method)(),
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        routing = override_settings(
            READ_REPLICA_ENABLED=True,
            READ_REPLICA_ALIASES=list(self.replica_aliases),
            RECENT_WRITE_TTL=30,
            REPLICA_MAX_LAG=10.0,
            REPLICA_HEALTH_INTERVAL=3600.0,  # re-checked only via set_replica() / fresh checks
            REPLICA_FAILURE_COOLDOWN=3600,
        )
        routing.enable()
        self.addCleanup(routing.disable)
        replica.reset()
        db_metrics.reset()
        self.addCleanup(replica.reset)

    def set_replica(self, alias=None, **state):
        fake = self.fake_replicas[alias] if alias else self.fake_replica
        for name, value in state.items():
            setattr(fake, name, value)
        replica.reset()
