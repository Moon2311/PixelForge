"""Cached catalog endpoints against a real Redis (skipped when REDIS_TEST_URL is unreachable)."""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.models import Role, UserProfile
from apps.authentication.tokens import issue_access_token
from apps.cart.models import Cart, CartItem
from apps.catalog import cache as catalog_cache
from apps.catalog.models import Brand, Category, Inventory, Product, VariantPrice
from apps.catalog.stats import refresh_product_stats
from apps.common import cache
from apps.common.testing import RedisCacheTestMixin, redis_cache_settings
from apps.orders.tests import ADDRESS, _product


class ProductDetailCacheTest(RedisCacheTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.product = _product("P1", price="100.00", stock=5)
        self.url = f"/api/products/{self.product.pk}/"

    def get(self, url=None):
        return self.client.get(url or self.url)

    def test_second_request_is_served_from_redis(self):
        first = self.get()
        self.assertEqual(first.status_code, 200)
        with self.assertNumQueries(0):
            second = self.get()
        self.assertEqual(second.json(), first.json())
        self.assertEqual(second.json()["data"]["stock_quantity"], 5)
        self.assertTrue(cache.exists(catalog_cache.product_key(self.product.pk)))

    def test_unknown_product_is_negative_cached(self):
        first = self.get("/api/products/999999/")
        self.assertEqual(first.status_code, 404)
        with self.assertNumQueries(0):
            second = self.get("/api/products/999999/")
        self.assertEqual(second.status_code, 404)
        self.assertEqual(second.json()["message"], first.json()["message"])
        self.assertIs(cache.get(catalog_cache.product_key(999999)), cache.CACHE_NOT_FOUND)

    def test_creating_the_product_clears_its_negative_entry(self):
        url = "/api/products/999998/"
        self.assertEqual(self.get(url).status_code, 404)
        with self.captureOnCommitCallbacks(execute=True):
            Product.objects.create(pk=999998, name="New", sku="NEW", slug="new", status="active")
        self.assertEqual(self.get(url).status_code, 200)

    def test_product_update_invalidates(self):
        self.get()
        with self.captureOnCommitCallbacks(execute=True):
            self.product.name = "Renamed phone"
            self.product.save()
        self.assertFalse(cache.exists(catalog_cache.product_key(self.product.pk)))
        self.assertEqual(self.get().json()["data"]["name"], "Renamed phone")

    def test_price_and_stock_changes_invalidate(self):
        self.get()
        with self.captureOnCommitCallbacks(execute=True):
            price = VariantPrice.objects.get(variant__product=self.product)
            price.regular_price = Decimal("120.00")
            price.save()
        self.assertEqual(self.get().json()["data"]["price"], 120.0)
        with self.captureOnCommitCallbacks(execute=True):
            inventory = Inventory.objects.get(variant__product=self.product)
            inventory.stock_quantity = 0
            inventory.save()
        self.assertEqual(self.get().json()["data"]["stock_quantity"], 0)

    def test_soft_delete_invalidates(self):
        self.get()
        with self.captureOnCommitCallbacks(execute=True):
            self.product.soft_delete()
        self.assertEqual(self.get().status_code, 404)

    def test_brand_rename_invalidates_its_products(self):
        self.get()
        with self.captureOnCommitCallbacks(execute=True):
            brand = self.product.brand
            brand.name = "Apple Inc"
            brand.save()
        self.assertEqual(self.get().json()["data"]["brand_name"], "Apple Inc")

    def test_no_invalidation_when_transaction_rolls_back(self):
        self.get()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.product.name = "Never committed"
            self.product.save()
        self.assertTrue(callbacks)  # deferred until commit, so a rollback drops them
        self.assertTrue(cache.exists(catalog_cache.product_key(self.product.pk)))

    def test_full_stats_rebuild_clears_product_documents(self):
        self.get()
        with self.captureOnCommitCallbacks(execute=True):
            refresh_product_stats()
        self.assertFalse(cache.exists(catalog_cache.product_key(self.product.pk)))

    def test_same_document_for_every_user(self):
        """Product documents are public: the key has no user part, and an
        authenticated request gets exactly what an anonymous one got."""
        anonymous = self.get().json()
        user = User.objects.create_user("buyer", "buyer@example.com", "Buyer-pass-123!")
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(user)}")
        self.assertEqual(api.get(self.url).json(), anonymous)
        self.assertNotIn(":user:", catalog_cache.product_key(self.product.pk))

    def test_redis_down_serves_from_postgresql(self):
        with redis_cache_settings(location="redis://127.0.0.1:1/0"), self.assertLogs("apps.cache", "WARNING"):
            response = self.get()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["name"], "Phone P1")
            self.assertEqual(self.get("/api/products/999999/").status_code, 404)

    def test_health_reports_redis(self):
        self.assertEqual(self.client.get("/api/health/").json()["data"]["checks"]["redis"]["status"], "ok")
        with redis_cache_settings(location="redis://127.0.0.1:1/0"):
            body = self.client.get("/api/health/")
        self.assertEqual(body.status_code, 200)
        self.assertEqual(body.json()["data"]["status"], "degraded")
        self.assertEqual(body.json()["data"]["checks"]["redis"]["status"], "error")


class CatalogListCacheTest(RedisCacheTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.product = _product("P1")

    def test_categories_cached_and_invalidated(self):
        first = self.client.get("/api/categories/").json()
        with self.assertNumQueries(0):
            self.assertEqual(self.client.get("/api/categories/").json(), first)
        with self.captureOnCommitCallbacks(execute=True):
            Category.objects.create(name="Tablets", slug="tablets")
        names = [c["name"] for c in self.client.get("/api/categories/").json()["data"]["results"]]
        self.assertIn("Tablets", names)

    def test_brand_product_counts_follow_new_products(self):
        def apple_count():
            brands = self.client.get("/api/brands/").json()["data"]["results"]
            return next(b["product_count"] for b in brands if b["name"] == "Apple")

        self.assertEqual(apple_count(), 1)
        with self.captureOnCommitCallbacks(execute=True):
            _product("P2")
        self.assertEqual(apple_count(), 2)
        with self.captureOnCommitCallbacks(execute=True):
            Brand.objects.create(name="Samsung", slug="samsung")
        names = [b["name"] for b in self.client.get("/api/brands/").json()["data"]["results"]]
        self.assertIn("Samsung", names)


class CheckoutIgnoresCacheTest(RedisCacheTestMixin, TestCase):
    """Inventory is authoritative in PostgreSQL: checkout never trusts the cache."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("buyer", "buyer@example.com", "Buyer-pass-123!")
        UserProfile.objects.create(user=self.user, role=Role.objects.get_or_create(name="buyer")[0])
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.user)}")
        self.phone = _product("P1", price="100.00", stock=5)
        cart = Cart.objects.create(user_id=self.user.id)
        CartItem.objects.create(cart=cart, product_id=self.phone.id, quantity=2)

    def place(self):
        return self.api.post("/api/orders/", {
            "contact": "buyer@example.com",
            "delivery_method": "ship",
            "shipping_address": ADDRESS,
            "shipping_method": "standard",
            "billing_same_as_shipping": True,
            "payment_method": "cod",
            "items": [{"product_id": self.phone.id, "quantity": 2, "unit_price": "100.00"}],
            "total": "200.00",
        }, format="json")

    def test_stale_cached_stock_cannot_oversell(self):
        url = f"/api/products/{self.phone.pk}/"
        self.assertEqual(self.client.get(url).json()["data"]["stock_quantity"], 5)
        # Sell out behind the cache's back (update() sends no signals).
        Inventory.objects.filter(variant__product=self.phone).update(stock_quantity=1)
        self.assertEqual(self.client.get(url).json()["data"]["stock_quantity"], 5)  # stale display
        self.assertEqual(self.place().status_code, 409)
        self.assertEqual(Inventory.objects.get(variant__product=self.phone).stock_quantity, 1)

    def test_order_refreshes_the_cached_stock(self):
        url = f"/api/products/{self.phone.pk}/"
        self.client.get(url)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.place().status_code, 201)
        self.assertEqual(self.client.get(url).json()["data"]["stock_quantity"], 3)
