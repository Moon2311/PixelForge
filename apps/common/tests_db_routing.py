"""Primary/replica read routing, read-your-writes and the cache on top.

Routing tests run against the ``replica`` test mirror (a second connection
to the same test database) with the replication state simulated
(``apps.common.testing.FakeReplica``), and assert which connection served
each query. Tests that need read-your-writes markers use a real Redis
(skipped when REDIS_TEST_URL is unreachable).
"""

import contextlib
import threading
import time
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.db import connections, transaction
from django.http import HttpResponse
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TransactionTestCase,
    override_settings,
)
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.authentication.models import Role, UserProfile
from apps.authentication.tokens import issue_access_token
from apps.cart.models import Cart
from apps.catalog import cache as catalog_cache
from apps.catalog.models import Category, Product, RecentlyViewed
from apps.common import cache
from apps.common.db import PRIMARY, REPLICA, consistency, context, metrics, replica
from apps.common.db.middleware import ReadReplicaMiddleware
from apps.common.db.router import PrimaryReplicaRouter, ReplicaWriteError, is_write_sql
from apps.common.testing import (
    RedisCacheTestMixin,
    ReplicaRoutingTestMixin,
    redis_cache_settings,
)
from apps.orders.models import Order
from apps.orders.tests import _product
from apps.payments.models import Payment

router = PrimaryReplicaRouter()


def _user(username, role="buyer"):
    user = User.objects.create_user(username, f"{username}@example.com", "Some-pass-123!")
    UserProfile.objects.create(user=user, role=Role.objects.get_or_create(name=role)[0])
    return user


def _api(user=None):
    api = APIClient()
    if user is not None:
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(user)}")
    return api


def _touching(queries, table):
    return [q["sql"] for q in queries if f'"{table}"' in q["sql"]]


class _Queries:
    """Captured queries of several connections, as one sequence."""

    def __init__(self, contexts):
        self.contexts = contexts

    def __iter__(self):
        for capture in self.contexts:
            yield from capture.captured_queries

    def __len__(self):
        return sum(len(capture) for capture in self.contexts)


class QueryCaptureMixin:
    @contextlib.contextmanager
    def capture(self):
        """Yields (primary, replicas) query lists of this thread; ``replicas``
        covers every reader alias of the test."""
        aliases = getattr(self, "replica_aliases", (REPLICA,))
        with contextlib.ExitStack() as stack:
            primary = stack.enter_context(CaptureQueriesContext(connections[PRIMARY]))
            replicas = [stack.enter_context(CaptureQueriesContext(connections[a])) for a in aliases]
            yield primary, _Queries(replicas)

    def assertServedBy(self, alias, captured, table):
        primary, replica_queries = captured
        on_primary, on_replica = _touching(primary, table), _touching(replica_queries, table)
        if alias == REPLICA:
            self.assertTrue(on_replica, f"no {table} query on the replica")
            self.assertEqual(on_primary, [], f"{table} queries reached the primary")
        else:
            self.assertTrue(on_primary, f"no {table} query on the primary")
            self.assertEqual(on_replica, [], f"{table} queries reached the replica")


# ---------------------------------------------------------------------------
# Router decisions (no database needed)
# ---------------------------------------------------------------------------


@override_settings(READ_REPLICA_ENABLED=True, READ_REPLICA_ALIASES=[REPLICA])
class RouterTest(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(replica, "usable", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    @contextlib.contextmanager
    def request(self, **state):
        with context.request_scope(context.RequestState(**state)):
            yield

    def test_writes_always_use_primary(self):
        with self.request():  # a GET that would read the replica
            for model in (Product, Category, RecentlyViewed, Cart, Order, Payment, User, Session):
                self.assertEqual(router.db_for_write(model), PRIMARY)
                self.assertEqual(router.db_for_read(Product), REPLICA)

    def test_catalog_reads_use_replica_in_safe_requests(self):
        with self.request():
            self.assertEqual(router.db_for_read(Product), REPLICA)
            self.assertEqual(router.db_for_read(RecentlyViewed), REPLICA)

    def test_strongly_consistent_apps_always_read_primary(self):
        with self.request():
            for model in (Cart, Order, Payment, User, Session, UserProfile):
                self.assertEqual(router.db_for_read(model), PRIMARY, model)

    def test_unsafe_requests_read_primary(self):
        with self.request(pinned=True):
            self.assertEqual(router.db_for_read(Product), PRIMARY)

    def test_reads_outside_requests_use_primary(self):
        self.assertEqual(router.db_for_read(Product), PRIMARY)  # shell, management commands

    def test_use_primary_block(self):
        with self.request(), context.use_primary():
            self.assertEqual(router.db_for_read(Product), PRIMARY)

    def test_unknown_required_lsn_reads_primary(self):
        with self.request(), context.require_lsn(None):
            self.assertEqual(router.db_for_read(Product), PRIMARY)

    def test_replica_disabled_reads_primary(self):
        with self.settings(READ_REPLICA_ENABLED=False), self.request():
            self.assertEqual(router.db_for_read(Product), PRIMARY)

    def test_migrations_only_on_primary(self):
        for app_label in ("catalog", "cart", "orders", "payments", "authentication", "auth"):
            self.assertTrue(router.allow_migrate(PRIMARY, app_label))
            self.assertFalse(router.allow_migrate(REPLICA, app_label))

    def test_relations_between_primary_and_replica_objects(self):
        a, b = Product(), Category()
        a._state.db, b._state.db = PRIMARY, REPLICA
        self.assertTrue(router.allow_relation(a, b))
        b._state.db = "other"
        self.assertIsNone(router.allow_relation(a, b))

    def test_write_statement_detection(self):
        for sql in ('INSERT INTO "x"', ' update "x" SET', 'DELETE FROM "x"', "TRUNCATE x"):
            self.assertTrue(is_write_sql(sql), sql)
        for sql in ('SELECT 1', 'SELECT * FROM "x" FOR UPDATE', "SAVEPOINT s1", None):
            self.assertFalse(is_write_sql(sql), sql)


# ---------------------------------------------------------------------------
# Routing against the replica mirror
# ---------------------------------------------------------------------------


class ReplicaRoutingTest(QueryCaptureMixin, ReplicaRoutingTestMixin, TransactionTestCase):
    """Cache off (the default in tests): no Redis needed."""

    def setUp(self):
        super().setUp()
        self.product = _product("P1")
        self.admin = _user("admin", role="admin")

    def test_normal_reads_use_replica(self):
        with self.capture() as captured:
            response = self.client.get("/api/search/products/?q=Phone")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 1)
        self.assertServedBy(REPLICA, captured, "apps_product")
        self.assertGreater(metrics.snapshot()["db_replica_reads_total"], 0)

    def test_writes_never_reach_the_replica(self):
        category = self.product.category
        with self.capture() as (primary, replica_queries):
            response = _api(self.admin).patch(
                f"/api/catalog/categories/{category.pk}/", {"name": "Smartphones"}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(replica_queries), [])  # validation reads included
        self.assertTrue(any(q["sql"].startswith("UPDATE") for q in primary))

    def test_replica_connection_refuses_writes(self):
        with self.assertRaises(ReplicaWriteError):
            Category.objects.using(REPLICA).create(name="Nope", slug="nope")
        with self.assertRaises(ReplicaWriteError):
            Category.objects.using(REPLICA).filter(pk=self.product.category_id).update(name="Nope")
        self.assertFalse(Category.objects.filter(name="Nope").exists())

    def test_reads_inside_transactions_stay_on_primary(self):
        with self.capture() as captured, context.request_scope(context.RequestState()), transaction.atomic():
            list(Product.objects.all())
        self.assertServedBy(PRIMARY, captured, "apps_product")

    def test_replica_unavailable_falls_back_to_primary(self):
        self.set_replica(down=True)
        with self.capture() as captured, self.assertLogs("apps.db", "WARNING"):
            response = self.client.get("/api/search/products/?q=Phone")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 1)
        self.assertServedBy(PRIMARY, captured, "apps_product")
        self.assertEqual(metrics.snapshot()["db_replica_errors_total"], 1)

    def test_replica_failing_mid_request_is_retried_on_primary(self):
        """Healthy at the last check, then the query itself fails."""
        replica.check()
        self.assertEqual(replica.state().status, replica.OK)

        def broken(execute, sql, params, many, ctx):
            self.fake_replica.down = True
            raise connections[REPLICA].Database.OperationalError("server closed the connection (test)")

        client = Client(raise_request_exception=False)
        with connections[REPLICA].execute_wrapper(broken), self.assertLogs("apps.db", "WARNING"):
            response = client.get(f"/api/catalog/products/{self.product.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["id"], self.product.pk)
        self.assertEqual(metrics.snapshot()["db_replica_retries_total"], 1)
        self.assertEqual(replica.state().status, replica.UNAVAILABLE)

    def test_application_error_on_healthy_replica_is_not_retried(self):
        with mock.patch("apps.search.views.ProductSearch.search", side_effect=ValueError("bug")):
            response = Client(raise_request_exception=False).get("/api/search/products/?q=Phone")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(metrics.snapshot()["db_replica_retries_total"], 0)

    def test_lagging_replica_is_not_used(self):
        self.set_replica(replay_lsn=1, replay_age=60.0)  # far behind REPLICA_MAX_LAG (10 s)
        with self.capture() as captured, self.assertLogs("apps.db", "WARNING"):
            self.client.get("/api/search/products/?q=Phone")
        self.assertServedBy(PRIMARY, captured, "apps_product")
        self.assertEqual(replica.state().status, replica.LAGGING)

    def test_slightly_behind_replica_is_used(self):
        self.set_replica(replay_lsn=1, replay_age=0.5)
        with self.capture() as captured:
            self.client.get("/api/search/products/?q=Phone")
        self.assertServedBy(REPLICA, captured, "apps_product")

    def test_a_server_that_is_not_a_standby_is_not_used(self):
        self.set_replica(in_recovery=False)
        with self.capture() as captured, self.assertLogs("apps.db", "ERROR"):
            self.client.get("/api/search/products/?q=Phone")
        self.assertServedBy(PRIMARY, captured, "apps_product")

    def test_redis_unavailable_still_serves_every_request(self):
        """Markers can't be read: callers with an identity read the primary;
        anonymous reads still use the replica. Nothing fails."""
        buyer = _user("buyer")
        with redis_cache_settings(location="redis://127.0.0.1:1/0"):
            with self.capture() as captured:
                response = _api(buyer).get("/api/products/recently-viewed/")
            self.assertEqual(response.status_code, 200)
            self.assertServedBy(PRIMARY, captured, "apps_recentlyviewed")
            with self.capture() as captured:
                response = self.client.get("/api/search/products/?q=Phone")
            self.assertEqual(response.status_code, 200)
            self.assertServedBy(REPLICA, captured, "apps_product")
            response = _api(buyer).post("/api/products/recently-viewed/", {"ids": str(self.product.pk)},
                                        format="json")
            self.assertEqual(response.status_code, 200)

    def test_health_distinguishes_replica_states(self):
        def database_health():
            return self.client.get("/api/health/database/").json()["data"]

        data = database_health()
        self.assertEqual((data["status"], data["primary"]["status"]), ("healthy", "ok"))
        self.assertEqual((data["replica"]["status"], data["reads"]), ("ok", "replica"))
        self.assertEqual(data["replica"]["lag_bytes"], 0)

        self.set_replica(replay_lsn=1, replay_age=60.0)
        data = database_health()
        self.assertEqual((data["status"], data["replica"]["status"], data["reads"]), ("degraded", "lagging", "primary"))
        self.assertGreater(data["replica"]["lag_bytes"], 0)
        self.assertEqual(data["replica"]["lag_seconds"], 60.0)

        self.set_replica(down=True)
        with self.assertLogs("apps.db", "WARNING"):
            data = database_health()
        self.assertEqual((data["status"], data["replica"]["status"]), ("degraded", "unavailable"))
        self.assertNotIn("password", data["replica"]["error"].lower())

        body = self.client.get("/api/health/").json()["data"]
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["checks"]["database"]["replica"]["status"], "unavailable")

        with self.settings(READ_REPLICA_ENABLED=False):
            self.assertEqual(database_health()["replica"]["status"], "disabled")


# ---------------------------------------------------------------------------
# Read-your-writes (needs Redis)
# ---------------------------------------------------------------------------


class ReadYourWritesTest(QueryCaptureMixin, ReplicaRoutingTestMixin, RedisCacheTestMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.product = _product("P1")
        self.buyer = _user("buyer")
        self.other = _user("other")
        self.admin = _user("admin", role="admin")

    def replica_behind(self):
        """Replication stops here: later commits aren't on the replica."""
        self.set_replica(replay_lsn=replica.primary_lsn())

    def marker(self, user):
        return consistency.recent_write_lsn(consistency.user_key(user.pk))

    def view_history(self, user):
        return _api(user).post("/api/products/recently-viewed/", {"ids": str(self.product.pk)}, format="json")

    def history(self, user):
        with self.capture() as captured:
            response = _api(user).get("/api/products/recently-viewed/")
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]["results"], captured

    def test_read_after_write_uses_primary(self):
        self.replica_behind()
        self.assertEqual(self.view_history(self.buyer).status_code, 200)
        results, captured = self.history(self.buyer)
        self.assertEqual([p["id"] for p in results], [self.product.pk])
        self.assertServedBy(PRIMARY, captured, "apps_recentlyviewed")
        self.assertGreater(metrics.snapshot()["db_read_your_writes_primary_total"], 0)

    def test_marker_is_the_commit_lsn(self):
        before = replica.primary_lsn()
        self.view_history(self.buyer)
        self.assertGreaterEqual(self.marker(self.buyer), before)
        self.assertEqual(metrics.snapshot()["db_recent_write_marks_total"], 1)

    def test_users_are_isolated(self):
        self.replica_behind()
        self.view_history(self.buyer)
        self.assertEqual(self.marker(self.other), 0)
        _, captured = self.history(self.other)
        self.assertServedBy(REPLICA, captured, "apps_recentlyviewed")
        _, captured = self.history(self.buyer)
        self.assertServedBy(PRIMARY, captured, "apps_recentlyviewed")

    def test_caught_up_replica_serves_the_writer_before_the_marker_expires(self):
        """The decision is the replica's replay position, not a timer."""
        self.replica_behind()
        self.view_history(self.buyer)
        self.set_replica(replay_lsn=None)  # replication caught up
        self.assertGreater(self.marker(self.buyer), 0)  # still remembered
        _, captured = self.history(self.buyer)
        self.assertServedBy(REPLICA, captured, "apps_recentlyviewed")

    def test_marker_expires(self):
        self.replica_behind()
        with self.settings(RECENT_WRITE_TTL=1):
            self.view_history(self.buyer)
        self.assertGreater(self.marker(self.buyer), 0)
        _, captured = self.history(self.buyer)
        self.assertServedBy(PRIMARY, captured, "apps_recentlyviewed")
        time.sleep(1.1)
        self.assertEqual(self.marker(self.buyer), 0)
        # Gone: the caller reads the replica again (subject to REPLICA_MAX_LAG).
        _, captured = self.history(self.buyer)
        self.assertServedBy(REPLICA, captured, "apps_recentlyviewed")

    def test_reads_do_not_create_markers(self):
        self.history(self.buyer)
        self.client.get("/api/search/products/?q=Phone")
        self.assertEqual(self.marker(self.buyer), 0)
        self.assertEqual(metrics.snapshot()["db_recent_write_marks_total"], 0)

    # -- marker only after commit ------------------------------------------

    def run_view(self, view, **headers):
        request = RequestFactory().post("/test/", **headers)
        return ReadReplicaMiddleware(view)(request)

    def auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(user)}"}

    def test_committed_transaction_creates_marker(self):
        def view(request):
            with transaction.atomic():
                Category.objects.create(name="Committed", slug="committed")
                self.assertEqual(self.marker(self.buyer), 0)  # not before the commit
            return HttpResponse("ok")

        self.run_view(view, **self.auth(self.buyer))
        self.assertGreater(self.marker(self.buyer), 0)

    def test_rollback_creates_no_marker(self):
        def view(request):
            with transaction.atomic():
                Category.objects.create(name="Rolled back", slug="rolled-back")
                transaction.set_rollback(True)
            return HttpResponse("ok")

        self.run_view(view, **self.auth(self.buyer))
        self.assertFalse(Category.objects.filter(slug="rolled-back").exists())
        self.assertEqual(self.marker(self.buyer), 0)

    def test_rolled_back_savepoint_inside_committed_transaction(self):
        def view(request):
            with transaction.atomic(), contextlib.suppress(RuntimeError), transaction.atomic():
                Category.objects.create(name="Inner", slug="inner")
                raise RuntimeError
            return HttpResponse("ok")

        self.run_view(view, **self.auth(self.buyer))
        self.assertEqual(self.marker(self.buyer), 0)  # nothing committed

    def test_autocommit_write_creates_marker(self):
        def view(request):
            Category.objects.create(name="Autocommit", slug="autocommit")
            return HttpResponse("ok")

        self.run_view(view, **self.auth(self.buyer))
        self.assertGreater(self.marker(self.buyer), 0)

    # -- anonymous / guest callers -------------------------------------------

    def test_guest_writes_are_keyed_by_guest_session(self):
        self.replica_behind()
        headers = {"HTTP_X_GUEST_SESSION": "guest-abc"}
        response = self.client.post(
            "/api/cart/guest/items/",
            {"product_id": self.product.pk, "product_name": "Phone P1", "unit_price": "100.00", "quantity": 1},
            content_type="application/json", **headers,
        )
        self.assertEqual(response.status_code, 201)
        identity = consistency.request_key(RequestFactory().get("/", **headers))
        self.assertEqual(identity[0], "guest")
        self.assertNotIn("guest-abc", consistency.marker_key(identity))  # stored hashed
        self.assertGreater(consistency.recent_write_lsn(identity), 0)
        with self.capture() as captured:
            self.client.get(f"/api/catalog/products/{self.product.pk}/", **headers)
        self.assertServedBy(PRIMARY, captured, "apps_product")

    def test_anonymous_callers_have_no_marker_and_read_the_replica(self):
        self.replica_behind()
        self.view_history(self.buyer)
        self.assertIsNone(consistency.request_key(RequestFactory().get("/")))
        with self.capture() as captured:
            self.client.get("/api/search/products/?q=Phone")
        self.assertServedBy(REPLICA, captured, "apps_product")

    def test_expired_or_invalid_token_has_no_identity(self):
        request = RequestFactory().get("/", HTTP_AUTHORIZATION="Bearer not-a-token")
        self.assertIsNone(consistency.request_key(request))

    # -- Phase 12: write, immediate read, read after expiry -------------------

    def test_write_then_immediate_read_then_read_after_expiry(self):
        category = self.product.category
        url = f"/api/catalog/categories/{category.pk}/"
        admin = _api(self.admin)
        self.replica_behind()

        with self.settings(RECENT_WRITE_TTL=1):
            # Request A: UPDATE -> primary only.
            with self.capture() as (primary, replica_queries):
                response = admin.patch(url, {"name": "Smartphones"}, format="json")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(list(replica_queries), [])
            self.assertTrue(any(q["sql"].startswith("UPDATE") for q in primary))

            # Request B: immediate GET by the same user -> primary, new value.
            with self.capture() as captured:
                response = admin.get(url)
            self.assertEqual(response.json()["data"]["name"], "Smartphones")
            self.assertServedBy(PRIMARY, captured, "apps_category")

            # Meanwhile another caller is served by the (behind) replica.
            with self.capture() as captured:
                self.client.get(url)
            self.assertServedBy(REPLICA, captured, "apps_category")

            # Replication catches up and the marker expires.
            self.set_replica(replay_lsn=None)
            time.sleep(1.1)

            # Request C -> replica, which now has the write.
            with self.capture() as captured:
                response = admin.get(url)
            self.assertEqual(response.json()["data"]["name"], "Smartphones")
            self.assertServedBy(REPLICA, captured, "apps_category")

    # -- concurrency -----------------------------------------------------------

    def test_concurrent_requests_route_by_their_own_identity(self):
        """Writer and non-writer requests interleaved on threads: each request
        is routed by its own caller, never by another request's state."""
        self.replica_behind()
        self.view_history(self.buyer)
        # A request that finds another one probing the replica reads the
        # primary instead of waiting; probe once up front so that doesn't blur
        # what this test checks.
        replica.check()
        tokens = {"buyer": issue_access_token(self.buyer), "other": issue_access_token(self.other)}
        barrier = threading.Barrier(8)
        results, errors = [], []

        def worker(name):
            try:
                api = APIClient()
                api.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens[name]}")
                barrier.wait()
                with self.capture() as captured:
                    response = api.get("/api/products/recently-viewed/")
                served = REPLICA if _touching(captured[1], "apps_recentlyviewed") else PRIMARY
                results.append((name, served, response.json()["data"]["count"]))
            except Exception as exc:  # noqa: BLE001  (re-raised as a test failure below)
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(n,)) for n in ["buyer", "other"] * 4]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        for name, served, count in results:
            if name == "buyer":
                self.assertEqual((served, count), (PRIMARY, 1))
            else:
                self.assertEqual((served, count), (REPLICA, 0))


# ---------------------------------------------------------------------------
# Redis cache on top of primary/replica (needs Redis)
# ---------------------------------------------------------------------------


class CacheWithReplicaTest(QueryCaptureMixin, ReplicaRoutingTestMixin, RedisCacheTestMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.product = _product("P1")
        self.url = f"/api/products/{self.product.pk}/"

    def rename(self, name):
        """A committed catalog write (autocommit: invalidation runs at once)."""
        self.product.name = name
        self.product.save()

    def test_cache_hit_queries_no_database(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        with self.capture() as (primary, replica_queries):
            response = self.client.get(self.url)
        self.assertEqual(response.json()["data"]["name"], "Phone P1")
        self.assertEqual((list(primary), list(replica_queries)), ([], []))

    def test_miss_refills_from_caught_up_replica(self):
        with self.capture() as captured:
            self.client.get(self.url)
        self.assertServedBy(REPLICA, captured, "apps_product")
        self.assertTrue(cache.exists(catalog_cache.product_key(self.product.pk)))

    def test_write_invalidates_and_refill_waits_for_the_replica(self):
        self.client.get(self.url)
        self.set_replica(replay_lsn=replica.primary_lsn())  # the rename won't be replicated
        self.rename("Renamed phone")
        self.assertFalse(cache.exists(catalog_cache.product_key(self.product.pk)))
        self.assertGreater(consistency.catalog_write_lsn(), 0)

        # Anonymous reader, no marker of its own: the refill must not read the
        # replica that is missing the rename, and must not cache old data.
        with self.capture() as captured:
            response = self.client.get(self.url)
        self.assertEqual(response.json()["data"]["name"], "Renamed phone")
        self.assertServedBy(PRIMARY, captured, "apps_product")
        self.assertEqual(cache.get(catalog_cache.product_key(self.product.pk))["name"], "Renamed phone")

        self.set_replica(replay_lsn=None)  # caught up: refills use the replica again
        cache.delete(catalog_cache.product_key(self.product.pk))
        with self.capture() as captured:
            self.client.get(self.url)
        self.assertServedBy(REPLICA, captured, "apps_product")

    def test_refill_that_raced_a_write_is_not_stored(self):
        """A write commits while a refill is loading: the refill's value is
        returned to its caller but never stored over the invalidation."""
        key = catalog_cache.product_key(self.product.pk)
        original = catalog_cache.product_store.get_product

        def slow_load(product_id):
            value = original(product_id)  # old data, read before the write
            self.rename("Renamed during refill")  # commit + fence + invalidate
            return value

        with mock.patch.object(catalog_cache.product_store, "get_product", side_effect=slow_load):
            self.assertEqual(catalog_cache.get_product(self.product.pk)["name"], "Phone P1")
        self.assertFalse(cache.exists(key))
        self.assertEqual(cache.metrics.snapshot()["cache_fenced_skips_total"], 1)
        self.assertEqual(self.client.get(self.url).json()["data"]["name"], "Renamed during refill")

    def test_rolled_back_write_neither_invalidates_nor_raises_the_fence(self):
        self.client.get(self.url)
        fence_before = consistency.catalog_write_lsn()
        with transaction.atomic():
            self.rename("Never committed")
            transaction.set_rollback(True)
        self.assertTrue(cache.exists(catalog_cache.product_key(self.product.pk)))
        self.assertEqual(consistency.catalog_write_lsn(), fence_before)

    def test_redis_down_refills_read_the_primary(self):
        with redis_cache_settings(location="redis://127.0.0.1:1/0"), \
                self.assertLogs("apps.cache", "WARNING"), self.capture() as captured:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertServedBy(PRIMARY, captured, "apps_product")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigurationTest(SimpleTestCase):
    def test_replica_alias_mirrors_primary_in_tests(self):
        self.assertEqual(connections.settings[REPLICA]["TEST"]["MIRROR"], PRIMARY)

    def test_router_is_installed_and_middleware_runs_before_sessions(self):
        from django.conf import settings

        self.assertIn("apps.common.db.router.PrimaryReplicaRouter", settings.DATABASE_ROUTERS)
        middleware = settings.MIDDLEWARE
        self.assertLess(
            middleware.index("apps.common.db.middleware.ReadReplicaMiddleware"),
            middleware.index("django.contrib.sessions.middleware.SessionMiddleware"),
        )

    def test_ttl_shorter_than_lag_window_is_reported(self):
        from apps.common.checks import read_replica_check

        with self.settings(READ_REPLICA_ENABLED=True, READ_REPLICA_ALIASES=[REPLICA], CACHE_ENABLED=True,
                           RECENT_WRITE_TTL=5,
                           REPLICA_MAX_LAG=10.0, REPLICA_HEALTH_INTERVAL=5.0):
            self.assertEqual([w.id for w in read_replica_check(None)], ["common.W002"])
        with self.settings(READ_REPLICA_ENABLED=True, READ_REPLICA_ALIASES=[REPLICA], CACHE_ENABLED=True,
                           RECENT_WRITE_TTL=30):
            self.assertEqual(read_replica_check(None), [])

    def test_lsn_parsing(self):
        self.assertEqual(replica.parse_lsn("16/B374D848"), 0x16B374D848)
        self.assertEqual(replica.format_lsn(0x16B374D848), "16/B374D848")
        self.assertIsNone(replica.parse_lsn(None))


# ---------------------------------------------------------------------------
# Reader pool: several replicas behind one logical reader endpoint
# ---------------------------------------------------------------------------

POOL = ("replica_1", "replica_2", "replica_3")


class PoolMixin(QueryCaptureMixin, ReplicaRoutingTestMixin):
    replica_aliases = POOL
    databases = {"default", *POOL}

    def setUp(self):
        super().setUp()
        self.product = _product("P1")

    def routed(self, requests=60, url="/api/search/products/?q=Phone"):
        """Per-target read routing counts after ``requests`` anonymous GETs."""
        metrics.reset()
        for _ in range(requests):
            self.assertEqual(self.client.get(url).status_code, 200)
        return metrics.snapshot()["db_reader_routing_total"]

    def pick(self, times=1):
        """Read targets chosen for ``times`` separate GET requests (no HTTP)."""
        picks = []
        for _ in range(times):
            with context.request_scope(context.RequestState()):
                picks.append(router.db_for_read(Product))
        return picks


class ReaderPoolTest(PoolMixin, TransactionTestCase):
    def test_reads_spread_over_every_healthy_replica(self):
        counts = self.routed()
        self.assertEqual(set(counts), set(POOL))  # never the primary
        for alias in POOL:
            self.assertGreater(counts[alias], 0, counts)

    def test_writes_never_use_the_pool(self):
        admin = _user("admin", role="admin")
        captures = [CaptureQueriesContext(connections[alias]) for alias in POOL]
        with contextlib.ExitStack() as stack:
            for capture in captures:
                stack.enter_context(capture)
            response = _api(admin).patch(
                f"/api/catalog/categories/{self.product.category_id}/", {"name": "Smartphones"}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([len(c) for c in captures], [0, 0, 0])

    def test_every_replica_refuses_writes(self):
        for alias in POOL:
            with self.assertRaises(ReplicaWriteError):
                Category.objects.using(alias).create(name=f"Nope {alias}", slug=f"nope-{alias}")

    def test_unavailable_replica_is_removed_and_reads_continue(self):
        self.set_replica("replica_2", down=True)
        with self.assertLogs("apps.db", "WARNING") as logs:
            counts = self.routed()
        self.assertNotIn("replica_2", counts)
        self.assertGreater(counts["replica_1"], 0)
        self.assertGreater(counts["replica_3"], 0)
        self.assertTrue(any("event=replica_removed replica=replica_2" in line for line in logs.output))
        self.assertEqual(replica.state("replica_2").status, replica.UNAVAILABLE)

    def test_lagging_replica_is_removed(self):
        self.set_replica("replica_3", replay_lsn=1, replay_age=60.0)
        with self.assertLogs("apps.db", "WARNING"):
            counts = self.routed()
        self.assertNotIn("replica_3", counts)
        self.assertEqual(set(counts), {"replica_1", "replica_2"})

    def test_recovered_replica_rejoins_the_pool(self):
        self.set_replica("replica_1", down=True)
        with self.assertLogs("apps.db", "WARNING"):
            self.assertNotIn("replica_1", self.routed(20))
        # Recovered; the next check (after the cooldown) finds it healthy.
        self.fake_replicas["replica_1"].down = False
        with self.settings(REPLICA_FAILURE_COOLDOWN=0), self.assertLogs("apps.db", "INFO") as logs:
            counts = self.routed()
        self.assertGreater(counts.get("replica_1", 0), 0)
        self.assertTrue(any("event=replica_rejoined replica=replica_1" in line for line in logs.output))

    def test_every_replica_down_reads_the_primary(self):
        for alias in POOL:
            self.set_replica(alias, down=True)
        with self.assertLogs("apps.db", "WARNING"), self.capture() as captured:
            response = self.client.get("/api/search/products/?q=Phone")
        self.assertEqual(response.json()["data"]["total"], 1)
        self.assertServedBy(PRIMARY, captured, "apps_product")

    def test_lower_lag_gets_more_reads(self):
        self.set_replica("replica_1", replay_lsn=1, replay_age=0.0)
        self.set_replica("replica_2", replay_lsn=1, replay_age=9.0)  # under REPLICA_MAX_LAG
        self.set_replica("replica_3", replay_lsn=1, replay_age=0.0)
        picks = self.pick(600)
        self.assertEqual(picks.count(PRIMARY), 0)
        # Weights 1 : 0.1 : 1, so replica_2 gets roughly 1/21 of the reads.
        self.assertGreater(picks.count("replica_2"), 0)
        self.assertLess(picks.count("replica_2"), picks.count("replica_1") / 3)
        self.assertLess(picks.count("replica_2"), picks.count("replica_3") / 3)

    def test_a_request_reads_from_one_replica(self):
        with context.request_scope(context.RequestState()):
            targets = {router.db_for_read(Product) for _ in range(30)}
        self.assertEqual(len(targets), 1)
        self.assertIn(targets.pop(), POOL)

    def test_replay_position_is_cached_per_connection(self):
        """Behind a reader endpoint a new connection can reach another
        server: a cached replay position must not outlive its connection."""
        alias = "replica_1"
        connections[alias].ensure_connection()
        replica._remember_replay(alias, 1000)
        self.assertEqual(replica._cached_replay(alias), 1000)
        connections[alias].close()
        connections[alias].ensure_connection()
        self.assertIsNone(replica._cached_replay(alias))

    def test_health_lists_every_replica_without_credentials(self):
        self.set_replica("replica_3", down=True)
        with self.assertLogs("apps.db", "WARNING"):
            response = self.client.get("/api/health/database/")
        body = response.json()["data"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["writer_role"], "primary")
        self.assertEqual(body["writer"]["role"], "primary")
        self.assertIsNotNone(body["writer"]["wal_lsn"])
        pool = body["replica"]
        self.assertEqual([m["alias"] for m in pool["members"]], list(POOL))
        self.assertEqual([m["status"] for m in pool["members"]], ["ok", "ok", "unavailable"])
        self.assertEqual(pool["healthy_count"], 2)
        self.assertEqual(body["routing_metrics"]["db_replica_healthy"], body["healthy_replica_count"])
        self.assertIn("replica_1", body["routing_metrics"]["db_replica_lag"])
        for key in ("cluster_status", "replica_count", "desired_replica_count", "replication_lag",
                    "failover_state"):
            self.assertIn(key, body)
        self.assertEqual(body["failover_state"], {"enabled": False})
        text = response.content.decode().lower()
        db = connections.settings[PRIMARY]
        for secret in ("password", "postgres://", db["PASSWORD"] or "\0"):
            self.assertNotIn(secret.lower(), text)


class ReaderPoolReadYourWritesTest(PoolMixin, RedisCacheTestMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.buyer = _user("buyer")

    def write(self):
        response = _api(self.buyer).post(
            "/api/products/recently-viewed/", {"ids": str(self.product.pk)}, format="json"
        )
        self.assertEqual(response.status_code, 200)

    def read_targets(self, times=20):
        targets = set()
        for _ in range(times):
            metrics.reset()
            response = _api(self.buyer).get("/api/products/recently-viewed/")
            self.assertEqual(response.json()["data"]["count"], 1)  # always sees its write
            targets |= set(metrics.snapshot()["db_reader_routing_total"])
        return targets

    def test_writer_reads_a_replica_that_has_its_write(self):
        before = replica.primary_lsn()
        for alias in ("replica_1", "replica_2"):
            self.set_replica(alias, replay_lsn=before)  # stalled before the write
        self.write()
        self.assertEqual(self.read_targets(), {"replica_3"})

    def test_writer_reads_the_primary_when_no_replica_has_its_write(self):
        before = replica.primary_lsn()
        for alias in POOL:
            self.set_replica(alias, replay_lsn=before)
        self.write()
        self.assertEqual(self.read_targets(5), {"primary"})
        self.assertGreater(metrics.snapshot()["db_read_your_writes_primary_total"], 0)

    def test_other_callers_keep_using_the_whole_pool(self):
        before = replica.primary_lsn()
        for alias in ("replica_1", "replica_2"):
            self.set_replica(alias, replay_lsn=before)
        self.write()
        self.assertEqual(set(self.routed()), set(POOL))


# ---------------------------------------------------------------------------
# Endpoint configuration (settings are read once per process: subprocesses)
# ---------------------------------------------------------------------------


def _settings_in_subprocess(**env):
    import json
    import os
    import subprocess
    import sys

    script = (
        "import json, django, os;"
        "os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings';"
        "django.setup();"
        "from django.conf import settings as s;"
        "print(json.dumps({'aliases': s.READ_REPLICA_ALIASES, 'enabled': s.READ_REPLICA_ENABLED,"
        " 'failover': s.FAILOVER_ENABLED, 'desired': s.DB_DESIRED_REPLICA_COUNT,"
        " 'dbs': {a: {k: c.get(k) for k in ('HOST', 'PORT', 'NAME', 'USER')} | "
        "{'read_only': 'default_transaction_read_only=on' in c['OPTIONS'].get('options', ''),"
        " 'mirror': c.get('TEST', {}).get('MIRROR')} for a, c in s.DATABASES.items()}}))"
    )
    keys = ("DB_WRITER_ENDPOINT", "DB_READER_ENDPOINT", "READ_REPLICA_COUNT", "DB_REPLICA_HOST",
            "DATABASE_REPLICA_URL", "READ_REPLICA_ENABLED", "FAILOVER_ENABLED", "DB_DESIRED_REPLICA_COUNT")
    clean = {k: v for k, v in os.environ.items() if k not in keys and not k.startswith("DB_REPLICA_")}
    result = subprocess.run([sys.executable, "-c", script], env={**clean, **env}, capture_output=True, check=False,
                            text=True, timeout=60)
    if result.returncode:
        return result.stderr
    return json.loads(result.stdout)


class EndpointConfigurationTest(SimpleTestCase):
    def test_no_replica_configuration_is_single_database(self):
        config = _settings_in_subprocess()
        self.assertEqual(config["aliases"], [])
        self.assertEqual(list(config["dbs"]), ["default"])

    def test_writer_and_reader_endpoints(self):
        config = _settings_in_subprocess(
            DB_PRIMARY_HOST="ignored", DB_PRIMARY_USER="app", DB_PRIMARY_PASSWORD="x",
            DB_WRITER_ENDPOINT="haproxy:5000", DB_READER_ENDPOINT="haproxy:5001",
            FAILOVER_ENABLED="true", DB_DESIRED_REPLICA_COUNT="3",
        )
        self.assertEqual(config["aliases"], ["replica"])
        self.assertEqual(config["dbs"]["default"]["HOST"], "haproxy")
        self.assertEqual(str(config["dbs"]["default"]["PORT"]), "5000")
        self.assertEqual(config["dbs"]["replica"]["HOST"], "haproxy")
        self.assertEqual(str(config["dbs"]["replica"]["PORT"]), "5001")
        self.assertEqual(config["dbs"]["replica"]["USER"], "app")  # credentials from the writer
        self.assertTrue(config["dbs"]["replica"]["read_only"])
        self.assertFalse(config["dbs"]["default"]["read_only"])
        self.assertEqual((config["failover"], config["desired"]), (True, 3))

    def test_replica_pool_of_configurable_size(self):
        config = _settings_in_subprocess(
            READ_REPLICA_COUNT="3", DB_REPLICA_1_HOST="r1", DB_REPLICA_2_HOST="r2",
            DATABASE_REPLICA_3_URL="postgres://u:p@r3:6432/pixelforge",
        )
        self.assertEqual(config["aliases"], ["replica_1", "replica_2", "replica_3"])
        self.assertEqual([config["dbs"][a]["HOST"] for a in config["aliases"]], ["r1", "r2", "r3"])
        self.assertEqual(str(config["dbs"]["replica_3"]["PORT"]), "6432")
        for alias in config["aliases"]:
            self.assertTrue(config["dbs"][alias]["read_only"])
            self.assertEqual(config["dbs"][alias]["mirror"], "default")
        self.assertEqual(config["desired"], 3)

    def test_missing_pool_member_is_a_configuration_error(self):
        error = _settings_in_subprocess(READ_REPLICA_COUNT="2", DB_REPLICA_1_HOST="r1")
        self.assertIsInstance(error, str)
        self.assertIn("DB_REPLICA_2_HOST", error)

    def test_legacy_single_replica_settings_still_work(self):
        config = _settings_in_subprocess(DB_REPLICA_HOST="replica-host")
        self.assertEqual(config["aliases"], ["replica"])
        self.assertEqual(config["dbs"]["replica"]["HOST"], "replica-host")

    def test_routing_disabled_keeps_reads_on_the_writer(self):
        config = _settings_in_subprocess(DB_READER_ENDPOINT="haproxy:5001", READ_REPLICA_ENABLED="false")
        self.assertFalse(config["enabled"])


# ---------------------------------------------------------------------------
# HA manager's view (Patroni REST API)
# ---------------------------------------------------------------------------


@override_settings(FAILOVER_ENABLED=True, DB_HA_STATUS_URL="http://ha:8008")
class FailoverStateTest(SimpleTestCase):
    MEMBERS = [
        {"name": "pg2", "role": "leader", "state": "running", "timeline": 2, "host": "pg2"},
        {"name": "pg3", "role": "replica", "state": "streaming", "timeline": 2, "lag": 0},
    ]
    HISTORY = [[1, 50331808, "no recovery target specified", "2026-10-08T12:00:00+00:00", "pg2"]]

    def state(self, cluster, history=()):
        from apps.common.db import cluster as cluster_module

        responses = {"/cluster": cluster, "/history": list(history)}
        with mock.patch.object(cluster_module, "_get_json", side_effect=lambda path: responses[path]):
            return cluster_module.failover_state()

    def test_stable_cluster_after_a_promotion(self):
        state = self.state({"members": self.MEMBERS}, self.HISTORY)
        self.assertEqual((state["state"], state["leader"], state["timeline"]), ("stable", "pg2", 2))
        self.assertEqual(state["promotions"], 1)
        self.assertEqual(state["last_promotion"]["new_leader"], "pg2")
        self.assertNotIn("host", state["members"][0])

    def test_no_leader_means_failover_in_progress(self):
        members = [{**m, "role": "replica", "state": "running"} for m in self.MEMBERS]
        self.assertEqual(self.state({"members": members})["state"], "no_leader")

    def test_maintenance_mode(self):
        self.assertEqual(self.state({"members": self.MEMBERS, "pause": True})["state"], "paused")

    def test_unreachable_ha_api_is_reported_not_raised(self):
        from apps.common.db import cluster as cluster_module

        with mock.patch.object(cluster_module, "_get_json", side_effect=OSError("refused")):
            state = cluster_module.failover_state()
        self.assertEqual(state["state"], "unknown")
