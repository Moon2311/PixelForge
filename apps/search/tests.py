"""Search and legacy product API tests (PostgreSQL-backed).

Tests verify:
    - ProductSearch keyword matching, filters, sorting, pagination and facets
    - Autocomplete, category and brand search
    - Legacy /api/products/ create, read, update, list, search and delete
    - Legacy /api/categories/, /api/brands/, /api/banners/ endpoints
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.models import Role, UserProfile
from apps.authentication.tokens import issue_access_token
from apps.catalog.models import (
    Attribute,
    AttributeValue,
    VariantAttributeValue,
    Banner,
    Brand,
    Category,
    Inventory,
    InventoryLog,
    LowStockAlert,
    Product,
    ProductReview,
    ProductSpecification,
    ProductVariant,
    VariantPrice,
)
from apps.search.services import (
    BrandSearch,
    CategorySearch,
    ProductAutocomplete,
    ProductSearch,
)


def _admin_token():
    user = User.objects.create_user("admin", "admin@example.com", "Admin-pass-123!")
    UserProfile.objects.create(user=user, role=Role.objects.get_or_create(name="admin")[0])
    return issue_access_token(user)


def _make_product(name, sku, brand, category, price, stock=5, status="active", **extra):
    product = Product.objects.create(
        name=name, sku=sku, slug=sku.lower(), brand=brand, category=category,
        status=status, **extra,
    )
    variant = ProductVariant.objects.create(product=product, sku=f"{sku}-V")
    VariantPrice.objects.create(variant=variant, regular_price=Decimal(price))
    Inventory.objects.create(variant=variant, stock_quantity=stock, low_stock_threshold=2)
    return product


class CatalogFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.apple = Brand.objects.create(name="Apple", slug="apple")
        cls.sony = Brand.objects.create(name="Sony", slug="sony")
        cls.phones = Category.objects.create(name="Smartphones", slug="smartphones")
        cls.audio = Category.objects.create(name="Audio", slug="audio")

        cls.iphone = _make_product(
            "iPhone 15 Pro", "IP15P", cls.apple, cls.phones, "999.00",
            tags=["5g", "titanium"], is_featured=True,
        )
        cls.iphone_se = _make_product("iPhone SE", "IPSE", cls.apple, cls.phones, "429.00", stock=0)
        cls.headphones = _make_product("WH-1000XM5 Headphones", "WH5", cls.sony, cls.audio, "349.00")
        cls.draft = _make_product("Prototype Phone", "PROTO", cls.apple, cls.phones, "10.00", status="draft")

        ProductSpecification.objects.create(product=cls.headphones, name="Driver", value="30mm dynamic")
        ProductReview.objects.create(product=cls.iphone, user_id=1, rating=5, status="approved")
        ProductReview.objects.create(product=cls.iphone, user_id=2, rating=3, status="approved")
        ProductReview.objects.create(product=cls.headphones, user_id=1, rating=1, status="pending")


# ---------------------------------------------------------------------------
# ProductSearch
# ---------------------------------------------------------------------------


class ProductSearchTest(CatalogFixtureMixin, TestCase):
    def ids(self, **kwargs):
        return [r["id"] for r in ProductSearch().search(**kwargs)["results"]]

    def test_match_all_returns_only_active(self):
        self.assertCountEqual(
            self.ids(facets=False),
            [self.iphone.pk, self.iphone_se.pk, self.headphones.pk],
        )

    def test_keyword_matches_name_case_insensitive(self):
        self.assertCountEqual(self.ids(query="IPHONE"), [self.iphone.pk, self.iphone_se.pk])

    def test_every_word_must_match(self):
        self.assertEqual(self.ids(query="iphone pro"), [self.iphone.pk])

    def test_keyword_matches_brand_spec_and_tags(self):
        self.assertCountEqual(self.ids(query="sony"), [self.headphones.pk])
        self.assertEqual(self.ids(query="dynamic"), [self.headphones.pk])
        self.assertEqual(self.ids(query="titanium"), [self.iphone.pk])

    def test_sku_partial_and_category_match(self):
        self.assertEqual(self.ids(query="ip15"), [self.iphone.pk])
        self.assertEqual(self.ids(query="wh5"), [self.headphones.pk])
        self.assertCountEqual(self.ids(query="smartphones"), [self.iphone.pk, self.iphone_se.pk])

    def test_attribute_filter(self):
        color = Attribute.objects.create(name="Color", slug="color")
        black = AttributeValue.objects.create(attribute=color, value="Black")
        variant = self.headphones.variants.get()
        VariantAttributeValue.objects.create(variant=variant, attribute_value=black)
        self.assertEqual(self.ids(filters={"attributes": {"color": "Black"}}), [self.headphones.pk])
        self.assertEqual(self.ids(filters={"attributes": {"color": ["White"]}}), [])

    def test_relevance_ranks_exact_name_first(self):
        self.assertEqual(self.ids(query="iPhone SE")[0], self.iphone_se.pk)

    def test_filters(self):
        self.assertCountEqual(
            self.ids(filters={"brand_slug": "apple"}), [self.iphone.pk, self.iphone_se.pk]
        )
        self.assertEqual(self.ids(filters={"category_id": self.audio.pk}), [self.headphones.pk])
        self.assertEqual(self.ids(filters={"min_price": 500}), [self.iphone.pk])
        self.assertCountEqual(
            self.ids(filters={"max_price": 430}), [self.iphone_se.pk, self.headphones.pk]
        )
        self.assertNotIn(self.iphone_se.pk, self.ids(filters={"in_stock": True}))
        self.assertEqual(self.ids(filters={"in_stock": False}), [self.iphone_se.pk])
        self.assertEqual(self.ids(filters={"is_featured": True}), [self.iphone.pk])
        self.assertEqual(self.ids(filters={"min_rating": 4}), [self.iphone.pk])
        self.assertEqual(self.ids(filters={"tags": ["5g"]}), [self.iphone.pk])
        self.assertEqual(self.ids(filters={"status": "draft"}), [self.draft.pk])

    def test_sort_by_price(self):
        self.assertEqual(
            self.ids(sort="price"), [self.headphones.pk, self.iphone_se.pk, self.iphone.pk]
        )
        self.assertEqual(
            self.ids(sort="-price"), [self.iphone.pk, self.iphone_se.pk, self.headphones.pk]
        )

    def test_pagination(self):
        result = ProductSearch().search(sort="price", page=2, page_size=2)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["total_pages"], 2)
        self.assertEqual([r["id"] for r in result["results"]], [self.iphone.pk])

    def test_result_document_shape(self):
        doc = ProductSearch().search(query="15 pro")["results"][0]
        self.assertEqual(doc["product_id"], self.iphone.pk)
        self.assertEqual(doc["brand"]["slug"], "apple")
        self.assertEqual(doc["price"]["regular"], 999.0)
        self.assertEqual(doc["rating"], 4.0)
        self.assertEqual(doc["review_count"], 2)
        self.assertTrue(doc["availability"]["in_stock"])
        self.assertEqual(len(doc["variants"]), 1)

    def test_facets(self):
        facets = ProductSearch().search()["facets"]
        brands = {b["slug"]: b["count"] for b in facets["brands"]}
        self.assertEqual(brands, {"apple": 2, "sony": 1})
        price = {p["label"]: p["count"] for p in facets["price_ranges"]}
        self.assertEqual(price["200.0-500.0"], 2)
        self.assertEqual(price["500.0-*"], 1)
        self.assertEqual(facets["availability"], {"in_stock": 2, "out_of_stock": 1})
        ratings = {r["min_rating"]: r["count"] for r in facets["ratings"]}
        self.assertEqual(ratings[4], 1)


class AutocompleteAndTaxonomySearchTest(CatalogFixtureMixin, TestCase):
    def test_autocomplete(self):
        names = [s["name"] for s in ProductAutocomplete().suggest("phone")]
        self.assertCountEqual(names, ["iPhone 15 Pro", "iPhone SE", "WH-1000XM5 Headphones"])

        _make_product("Phone Stand", "STAND", self.apple, self.phones, "19.00")
        suggestions = ProductAutocomplete().suggest("phone")
        self.assertEqual(suggestions[0]["name"], "Phone Stand")
        self.assertEqual(suggestions[0]["score"], 2)

    def test_category_search_counts_products(self):
        results = {c["slug"]: c for c in CategorySearch().search()}
        self.assertEqual(results["smartphones"]["product_count"], 3)
        self.assertEqual([c["slug"] for c in CategorySearch().search("aud")], ["audio"])

    def test_brand_search(self):
        results = BrandSearch().search("son")
        self.assertEqual([b["id"] for b in results], [self.sony.pk])
        self.assertEqual(results[0]["product_count"], 1)


class SearchEndpointTest(CatalogFixtureMixin, TestCase):
    def test_search_products_endpoint(self):
        resp = self.client.get("/api/search/products/", {"q": "iphone", "ordering": "price"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["results"][0]["id"], self.iphone_se.pk)

    def test_autocomplete_endpoint(self):
        resp = self.client.get("/api/search/autocomplete/", {"q": "wh"})
        self.assertEqual(resp.json()["data"]["suggestions"][0]["id"], self.headphones.pk)


# ---------------------------------------------------------------------------
# Legacy /api/products/
# ---------------------------------------------------------------------------


class LegacyProductApiTest(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {_admin_token()}")

    def _create(self, **overrides):
        payload = {
            "name": "Galaxy S24",
            "brand_name": "Samsung",
            "category_name": "Smartphones",
            "sku": "GS24",
            "price": 799.0,
            "discount_price": 749.0,
            "cost_price": 500.0,
            "stock_quantity": 10,
            "min_stock_alert": 3,
            "tags": "android, 5g",
            "color": "Black",
            "specifications": "Snapdragon 8 Gen 3",
            "images": "https://img.example/a.jpg,https://img.example/b.jpg",
            **overrides,
        }
        return self.api.post("/api/products/", payload, format="multipart")

    def test_create_maps_onto_catalog_models(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.content)
        doc = resp.json()["data"]

        self.assertEqual(doc["brand_name"], "Samsung")
        self.assertEqual(doc["category_name"], "Smartphones")
        self.assertEqual(doc["price"], 799.0)
        self.assertEqual(doc["discount_price"], 749.0)
        self.assertEqual(doc["stock_quantity"], 10)
        self.assertEqual(doc["min_stock_alert"], 3)
        self.assertEqual(doc["tags"], ["android", "5g"])
        self.assertEqual(doc["thumbnail"], "https://img.example/a.jpg")
        self.assertEqual(doc["status"], "active")

        product = Product.objects.get(pk=doc["id"])
        variant = product.variants.get()
        self.assertEqual(variant.sku, "GS24")
        self.assertEqual(variant.inventory.stock_quantity, 10)
        self.assertTrue(InventoryLog.objects.filter(variant=variant, action="create").exists())

    def test_duplicate_sku_rejected(self):
        self._create()
        resp = self._create(name="Other")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("sku", resp.json()["data"])

    def test_update_changes_stock_and_raises_low_stock_alert(self):
        product_id = self._create().json()["data"]["id"]
        resp = self.api.put(
            f"/api/products/{product_id}/",
            {
                "name": "Galaxy S24 Ultra", "brand_name": "Samsung",
                "category_name": "Smartphones", "sku": "GS24U", "price": 1199.0,
                "stock_quantity": 2,
            },
            format="multipart",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        doc = resp.json()["data"]
        self.assertEqual(doc["name"], "Galaxy S24 Ultra")
        self.assertEqual(doc["sku"], "GS24U")
        self.assertEqual(doc["price"], 1199.0)
        self.assertEqual(doc["stock_quantity"], 2)
        self.assertEqual(doc["images"], [])
        self.assertTrue(LowStockAlert.objects.filter(variant__product_id=product_id, status="open").exists())

    def test_public_search_and_admin_list(self):
        self._create()
        self._create(name="Bravia TV", brand_name="Sony", category_name="TV", sku="BRV",
                     price=1500.0, specifications="OLED panel", tags="", color="")

        def names(resp):
            return [p["name"] for p in resp.json()["data"]["results"]]

        # Storefront sends the same term as name/brand/specification.
        resp = self.client.get("/api/products/", {"name": "oled", "brand": "oled", "specification": "oled"})
        self.assertEqual(names(resp), ["Bravia TV"])
        resp = self.client.get("/api/products/", {"name": "samsung", "brand": "samsung", "specification": "samsung"})
        self.assertEqual(names(resp), ["Galaxy S24"])

        resp = self.client.get("/api/products/", {"sort": "price", "order": "desc", "page": 1, "page_size": 1})
        data = resp.json()["data"]
        self.assertEqual(data["count"], 2)
        self.assertTrue(data["has_next"])
        self.assertEqual(names(resp), ["Bravia TV"])

        resp = self.client.get("/api/products/", {"brand": "Sony", "stock_status": "in_stock"})
        self.assertEqual(names(resp), ["Bravia TV"])

        meta = self.client.get("/api/products/meta/").json()["data"]
        self.assertEqual(meta, {"categories": ["Smartphones", "TV"], "brands": ["Samsung", "Sony"]})

    def test_stock_endpoint_and_delete(self):
        product_id = self._create().json()["data"]["id"]
        resp = self.api.patch(f"/api/products/{product_id}/stock/", {"delta": 5}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.client.get(f"/api/products/{product_id}/").json()["data"]["stock_quantity"], 15)

        self.assertEqual(self.api.delete(f"/api/products/{product_id}/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/products/{product_id}/").status_code, 404)
        self.assertTrue(Product.objects.get(pk=product_id).is_deleted)

    def test_writes_require_admin(self):
        resp = APIClient().post("/api/products/", {"name": "x"}, format="json")
        self.assertIn(resp.status_code, (401, 403))


class LegacyCatalogApiTest(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {_admin_token()}")

    def test_category_crud_with_counts(self):
        resp = self.api.post("/api/categories/", {"name": "Cameras", "description": "Snap"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        cat_id = resp.json()["data"]["id"]

        brand = Brand.objects.create(name="Canon", slug="canon")
        _make_product("EOS R8", "EOSR8", brand, Category.objects.get(pk=cat_id), "1499.00")

        listed = self.client.get("/api/categories/").json()["data"]["results"]
        self.assertEqual(listed[0]["product_count"], 1)

        dup = self.api.post("/api/categories/", {"name": "cameras"}, format="json")
        self.assertEqual(dup.status_code, 400)

        self.assertEqual(self.api.delete(f"/api/categories/{cat_id}/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/categories/{cat_id}/").status_code, 404)

    def test_brand_update(self):
        brand_id = self.api.post("/api/brands/", {"name": "LG"}, format="json").json()["data"]["id"]
        resp = self.api.put(f"/api/brands/{brand_id}/", {"name": "LG Electronics", "is_active": False}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["slug"], "lg-electronics")
        self.assertEqual(self.client.get("/api/brands/").json()["data"]["count"], 0)

    def test_banners_filter_by_type_and_window(self):
        Banner.objects.create(title="Hero", type="hero")
        Banner.objects.create(title="Promo", type="promotion")
        Banner.objects.create(title="Expired", type="hero", end_at="2020-01-01T00:00:00Z")

        titles = [b["title"] for b in self.client.get("/api/banners/", {"type": "hero"}).json()["data"]["results"]]
        self.assertEqual(titles, ["Hero"])
        self.assertEqual(self.client.get("/api/banners/", {"type": "bogus"}).status_code, 400)
