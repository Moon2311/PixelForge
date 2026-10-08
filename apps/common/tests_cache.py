"""apps.common.cache against a real Redis (skipped when REDIS_TEST_URL is unreachable)."""

import datetime
import threading
import time
import uuid
from decimal import Decimal
from unittest import mock

from django.db import DatabaseError
from django.test import SimpleTestCase, override_settings
from django_redis import get_redis_connection
from rest_framework.renderers import JSONRenderer

from apps.common import cache
from apps.common.cache import locks, metrics
from apps.common.testing import RedisCacheTestMixin, redis_cache_settings


class NotFound(Exception):
    pass


class Loader:
    """Counts calls; optionally slow or failing."""

    def __init__(self, value="fresh", delay=0.0, error=None):
        self.value, self.delay, self.error = value, delay, error
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.value


class KeyTests(SimpleTestCase):
    def test_namespaced_and_deterministic(self):
        self.assertEqual(cache.build_key("product", 42), "pixelforge:product:42")
        self.assertEqual(cache.build_key("product", 42), cache.build_key("product", "42"))
        self.assertEqual(cache.lock_key("pixelforge:product:42"), "pixelforge:lock:product:42")

    def test_free_text_parts_cannot_collide(self):
        self.assertNotEqual(cache.build_key("a:b", "c"), cache.build_key("a", "b:c"))
        hashed = cache.build_key("search", "rtx 4070")
        self.assertRegex(hashed, r"^pixelforge:search:#[0-9a-f]{64}$")
        self.assertNotEqual(hashed, cache.build_key("search", "rtx 4080"))

    def test_user_scope_isolates_keys(self):
        self.assertEqual(cache.build_key("cart", user_id=7), "pixelforge:user:7:cart")
        self.assertNotEqual(cache.build_key("cart", user_id=7), cache.build_key("cart", user_id=8))
        self.assertNotEqual(cache.build_key("cart", user_id=7), cache.build_key("cart"))

    def test_rejects_ambiguous_parts(self):
        for bad in (None, True, 1.5, object()):
            with self.assertRaises(TypeError):
                cache.build_key("product", bad)
        with self.assertRaises(ValueError):
            cache.build_key()


class JitterTests(SimpleTestCase):
    def test_ttl_is_base_plus_jitter(self):
        ttls = {cache.jittered_ttl(300, 30) for _ in range(200)}
        self.assertTrue(all(300 <= ttl <= 330 for ttl in ttls))
        self.assertGreater(len(ttls), 1)

    def test_ttl_is_always_positive(self):
        self.assertEqual(cache.jittered_ttl(0, 0), 1)
        self.assertEqual(cache.jittered_ttl(-50, -5), 1)

    @override_settings(CACHE_DEFAULT_TTL=300, CACHE_TTL_JITTER=30, NEGATIVE_CACHE_TTL=60, NEGATIVE_CACHE_TTL_JITTER=15)
    def test_policy_defaults_and_overrides(self):
        self.assertTrue(300 <= cache.DEFAULT_POLICY.value_ttl() <= 330)
        self.assertTrue(60 <= cache.DEFAULT_POLICY.not_found_ttl() <= 75)
        policy = cache.CachePolicy(ttl=600, jitter=0, negative_ttl=0)
        self.assertEqual(policy.value_ttl(), 600)
        self.assertIsNone(policy.not_found_ttl())


@override_settings(CACHE_ENABLED=False)
class DisabledCacheTests(SimpleTestCase):
    def test_disabled_cache_always_loads(self):
        loader = Loader()
        self.assertEqual(cache.get_or_set("k", loader), "fresh")
        self.assertEqual(cache.get_or_set("k", loader), "fresh")
        self.assertEqual(loader.calls, 2)


class CacheAsideTests(RedisCacheTestMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.redis = get_redis_connection("default")
        self.key = cache.build_key("thing", 1)

    def test_miss_loads_and_populates(self):
        loader = Loader({"id": 1})
        self.assertEqual(cache.get_or_set(self.key, loader, policy=cache.CachePolicy(ttl=300, jitter=30)), {"id": 1})
        self.assertEqual(loader.calls, 1)
        self.assertEqual(cache.get(self.key), {"id": 1})
        self.assertTrue(300 <= self.redis.ttl(self.key) <= 330)
        self.assertEqual(metrics.snapshot()["cache_misses_total"], 1)
        self.assertEqual(metrics.snapshot()["cache_rebuild_total"], 1)

    def test_hit_skips_loader(self):
        cache.set(self.key, {"id": 1, "name": "cached"})
        loader = Loader()
        self.assertEqual(cache.get_or_set(self.key, loader), {"id": 1, "name": "cached"})
        self.assertEqual(loader.calls, 0)
        self.assertEqual(metrics.snapshot()["cache_hits_total"], 1)

    def test_cached_none_is_a_hit(self):
        loader = Loader(value=None)
        self.assertIsNone(cache.get_or_set(self.key, loader))
        self.assertIsNone(cache.get_or_set(self.key, loader))
        self.assertEqual(loader.calls, 1)

    def test_entry_expires(self):
        loader = Loader()
        policy = cache.CachePolicy(ttl=1, jitter=0)
        cache.get_or_set(self.key, loader, policy=policy)
        cache.get_or_set(self.key, loader, policy=policy)
        self.assertEqual(loader.calls, 1)
        time.sleep(1.1)
        cache.get_or_set(self.key, loader, policy=policy)
        self.assertEqual(loader.calls, 2)

    def test_basic_operations(self):
        self.assertIsNone(cache.get(self.key))
        self.assertEqual(cache.get(self.key, "default"), "default")
        self.assertFalse(cache.exists(self.key))
        self.assertTrue(cache.set(self.key, [1, 2]))
        self.assertTrue(cache.exists(self.key))
        self.assertEqual(cache.delete(self.key), 1)
        self.assertFalse(cache.exists(self.key))

    def test_delete_prefix(self):
        for i in range(3):
            cache.set(cache.build_key("thing", i), i)
        cache.set(cache.build_key("other", 1), 1)
        self.assertEqual(cache.delete_prefix("thing"), 3)
        self.assertTrue(cache.exists(cache.build_key("other", 1)))

    def test_cached_value_renders_like_a_fresh_one(self):
        value = {
            "price": Decimal("19.90"),
            "at": datetime.datetime(2026, 10, 7, 12, 30, 15, 123456, tzinfo=datetime.timezone.utc),
            "ref": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        }
        fresh = cache.get_or_set(self.key, lambda: value)
        cached = cache.get_or_set(self.key, Loader())
        render = JSONRenderer().render
        self.assertEqual(render(cached), render(fresh))

    def test_unserializable_value_is_returned_but_not_cached(self):
        instance = object()  # e.g. a model instance
        with self.assertLogs("apps.cache", "ERROR"):
            self.assertIs(cache.get_or_set(self.key, lambda: instance), instance)
        self.assertFalse(cache.exists(self.key))

    def test_corrupt_entry_is_a_miss(self):
        self.redis.set(self.key, b"not json")
        with self.assertLogs("apps.cache", "ERROR"):
            self.assertEqual(cache.get_or_set(self.key, Loader()), "fresh")
        self.assertEqual(cache.get(self.key), "fresh")


class NegativeCacheTests(RedisCacheTestMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.key = cache.build_key("thing", 999999)

    def test_not_found_is_cached_and_reraised(self):
        loader = Loader(error=NotFound("Thing 999999 not found"))
        for _ in range(3):
            with self.assertRaisesMessage(NotFound, "Thing 999999 not found"):
                cache.get_or_set(self.key, loader, not_found=NotFound)
        self.assertEqual(loader.calls, 1)
        self.assertIs(cache.get(self.key), cache.CACHE_NOT_FOUND)
        self.assertEqual(metrics.snapshot()["cache_negative_hits_total"], 2)

    @override_settings(NEGATIVE_CACHE_TTL=60, NEGATIVE_CACHE_TTL_JITTER=15)
    def test_negative_ttl_is_short_and_separate(self):
        policy = cache.CachePolicy(ttl=3600, jitter=0)
        with self.assertRaises(NotFound):
            cache.get_or_set(self.key, Loader(error=NotFound()), policy=policy, not_found=NotFound)
        self.assertTrue(60 <= get_redis_connection("default").ttl(self.key) <= 75)

    def test_negative_entry_expires(self):
        loader = Loader(error=NotFound())
        policy = cache.CachePolicy(negative_ttl=1, negative_jitter=0)
        for _ in range(2):
            with self.assertRaises(NotFound):
                cache.get_or_set(self.key, loader, policy=policy, not_found=NotFound)
        self.assertEqual(loader.calls, 1)
        time.sleep(1.1)
        loader.error = None
        self.assertEqual(cache.get_or_set(self.key, loader, policy=policy, not_found=NotFound), "fresh")
        self.assertEqual(loader.calls, 2)

    def test_negative_caching_can_be_disabled(self):
        policy = cache.CachePolicy(negative_ttl=0)
        with self.assertRaises(NotFound):
            cache.get_or_set(self.key, Loader(error=NotFound()), policy=policy, not_found=NotFound)
        self.assertFalse(cache.exists(self.key))

    def test_database_error_is_not_cached(self):
        loader = Loader(error=DatabaseError("connection lost"))
        with self.assertRaises(DatabaseError):
            cache.get_or_set(self.key, loader, not_found=NotFound)
        self.assertFalse(cache.exists(self.key))
        self.assertFalse(cache.exists(cache.lock_key(self.key)))  # lock released
        loader.error = None
        self.assertEqual(cache.get_or_set(self.key, loader, not_found=NotFound), "fresh")


class LockTests(RedisCacheTestMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.redis = get_redis_connection("default")
        self.lock = cache.lock_key(cache.build_key("thing", 1))

    def test_acquire_sets_owner_token_with_expiry(self):
        token = cache.acquire_lock(self.lock, ttl=5)
        self.assertTrue(token)
        self.assertEqual(self.redis.get(self.lock).decode(), token)
        self.assertTrue(0 < self.redis.pttl(self.lock) <= 5000)

    def test_second_request_cannot_acquire_held_lock(self):
        self.assertTrue(cache.acquire_lock(self.lock))
        self.assertIsNone(cache.acquire_lock(self.lock))

    def test_only_owner_can_release(self):
        token = cache.acquire_lock(self.lock)
        self.assertFalse(cache.release_lock(self.lock, "someone-else"))
        self.assertTrue(self.redis.exists(self.lock))
        self.assertTrue(cache.release_lock(self.lock, token))
        self.assertFalse(self.redis.exists(self.lock))
        self.assertFalse(cache.release_lock(self.lock, token))

    def test_expired_owner_cannot_release_next_owners_lock(self):
        old = cache.acquire_lock(self.lock, ttl=0.1)
        time.sleep(0.2)
        new = cache.acquire_lock(self.lock, ttl=5)
        self.assertTrue(new)
        self.assertFalse(cache.release_lock(self.lock, old))
        self.assertEqual(self.redis.get(self.lock).decode(), new)

    def test_lock_expires_on_its_own(self):
        self.assertTrue(cache.acquire_lock(self.lock, ttl=0.2))
        time.sleep(0.3)
        self.assertTrue(cache.acquire_lock(self.lock))


class StampedeTests(RedisCacheTestMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.key = cache.build_key("thing", "hot")

    def run_concurrently(self, count, target):
        barrier = threading.Barrier(count)
        results, errors = [], []

        def worker():
            barrier.wait()
            try:
                results.append(target())
            except Exception as exc:  # noqa: BLE001 (collected for the assertion)
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results, errors

    def test_100_concurrent_misses_query_the_database_once(self):
        loader = Loader({"id": "hot"}, delay=0.2)
        results, errors = self.run_concurrently(100, lambda: cache.get_or_set(self.key, loader))
        self.assertEqual(errors, [])
        self.assertEqual(loader.calls, 1)
        self.assertEqual(results, [{"id": "hot"}] * 100)
        counts = metrics.snapshot()
        self.assertEqual(counts["cache_rebuild_total"], 1)
        self.assertGreater(counts["cache_lock_contention_total"], 0)
        self.assertEqual(counts["cache_lock_timeout_total"], 0)

    def test_concurrent_not_found_queries_the_database_once(self):
        loader = Loader(error=NotFound("gone"), delay=0.2)
        results, errors = self.run_concurrently(
            50, lambda: cache.get_or_set(self.key, loader, not_found=NotFound)
        )
        self.assertEqual(loader.calls, 1)
        self.assertEqual(len(errors), 50)
        self.assertTrue(all(isinstance(e, NotFound) for e in errors))

    def test_waiter_receives_value_stored_by_lock_holder(self):
        lock = cache.lock_key(self.key)
        token = cache.acquire_lock(lock)
        loader = Loader("waiter loaded")
        result = []
        waiter = threading.Thread(target=lambda: result.append(cache.get_or_set(self.key, loader)))
        waiter.start()
        time.sleep(0.15)  # waiter is backing off
        cache.set(self.key, "holder loaded")
        cache.release_lock(lock, token)
        waiter.join()
        self.assertEqual(result, ["holder loaded"])
        self.assertEqual(loader.calls, 0)

    def test_value_stored_while_waiting_for_lock_is_not_reloaded(self):
        """The re-check after acquiring the lock: the previous holder filled the key."""
        real_acquire = locks.acquire_lock

        def acquire_after_previous_holder(key, ttl=None):
            cache.set(self.key, "stored by previous holder")
            return real_acquire(key, ttl)

        loader = Loader()
        with mock.patch.object(locks, "acquire_lock", side_effect=acquire_after_previous_holder):
            self.assertEqual(cache.get_or_set(self.key, loader), "stored by previous holder")
        self.assertEqual(loader.calls, 0)

    @override_settings(CACHE_LOCK_WAIT_TIMEOUT=0.3)
    def test_wait_timeout_falls_back_to_database(self):
        cache.acquire_lock(cache.lock_key(self.key), ttl=10)  # a holder that never finishes
        loader = Loader()
        started = time.monotonic()
        with self.assertLogs("apps.cache", "WARNING"):
            self.assertEqual(cache.get_or_set(self.key, loader), "fresh")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(loader.calls, 1)
        self.assertEqual(metrics.snapshot()["cache_lock_timeout_total"], 1)

    def test_waiters_back_off_instead_of_busy_looping(self):
        cache.acquire_lock(cache.lock_key(self.key), ttl=10)
        with override_settings(CACHE_LOCK_WAIT_TIMEOUT=0.5), mock.patch("apps.common.cache.manager.time.sleep",
                                                                        wraps=time.sleep) as sleep:
            with self.assertLogs("apps.cache", "WARNING"):
                cache.get_or_set(self.key, Loader())
        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertLessEqual(len(delays), 6)  # ~50, 100, 200, 200 ms, not a tight loop
        self.assertGreater(delays[1], delays[0])


class RedisUnavailableTests(RedisCacheTestMixin, SimpleTestCase):
    def test_falls_back_to_loader_and_logs_once(self):
        loader = Loader({"id": 1})
        with redis_cache_settings(location="redis://127.0.0.1:1/0"):
            with self.assertLogs("apps.cache", "WARNING") as logs:
                for _ in range(5):
                    self.assertEqual(cache.get_or_set("pixelforge-test:thing:1", loader), {"id": 1})
            self.assertEqual(loader.calls, 5)
            self.assertEqual(len([r for r in logs.records if "failed" in r.getMessage()]), 1)
            self.assertEqual(metrics.snapshot()["cache_errors_total"], 1)  # then skipped during cooldown
            self.assertFalse(cache.set("pixelforge-test:thing:1", 1))
            self.assertIsNone(cache.get("pixelforge-test:thing:1"))
            self.assertFalse(cache.exists("pixelforge-test:thing:1"))
            self.assertIsNone(cache.acquire_lock("pixelforge-test:lock:thing:1"))

    def test_not_found_still_raised_without_redis(self):
        with redis_cache_settings(location="redis://127.0.0.1:1/0"), self.assertLogs("apps.cache", "WARNING"):
            with self.assertRaises(NotFound):
                cache.get_or_set("pixelforge-test:thing:2", Loader(error=NotFound()), not_found=NotFound)

    @override_settings(CACHE_FAILURE_COOLDOWN=0)
    def test_recovers_when_redis_returns(self):
        with redis_cache_settings(location="redis://127.0.0.1:1/0"), self.assertLogs("apps.cache", "WARNING"):
            cache.get_or_set("pixelforge-test:thing:3", Loader())
        loader = Loader()
        with self.assertLogs("apps.cache", "INFO") as logs:
            cache.get_or_set("pixelforge-test:thing:3", loader)
        self.assertTrue(any("available again" in r.getMessage() for r in logs.records))
        cache.get_or_set("pixelforge-test:thing:3", loader)
        self.assertEqual(loader.calls, 1)
