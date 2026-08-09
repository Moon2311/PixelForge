"""Elasticsearch integration tests.

Tests verify:
    - SearchIndices creates versioned indices with aliases
    - SearchDocumentBuilder produces correct document structure
    - Event consumers are idempotent
    - ES client is properly initialized
"""

from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock
from uuid import uuid4

from django.test import TestCase

from apps.models import (
    Attribute,
    AttributeValue,
    Brand,
    Category,
    Inventory,
    Product,
    ProductAttributeValue,
    ProductImage,
    ProductReview,
    ProductSpecification,
    ProductVariant,
    Subcategory,
    VariantAttributeValue,
    VariantPrice,
)
from apps.search_indices import (
    SearchIndices,
    PRODUCT_INDEX_V1,
    CATEGORY_INDEX_V1,
    BRAND_INDEX_V1,
    PRODUCT_ALIAS,
    CATEGORY_ALIAS,
    BRAND_ALIAS,
    PRODUCT_MAPPING,
    CATEGORY_MAPPING,
    BRAND_MAPPING,
)
from apps.search_document_builder import SearchDocumentBuilder
from apps.event_consumers import CatalogEventConsumer
from apps.event_contracts import EventType, AggregateType


# ---------------------------------------------------------------------------
# SearchIndices Tests
# ---------------------------------------------------------------------------


class SearchIndicesTest(TestCase):
    """Test SearchIndices creates/manages versioned indices."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.indices = SearchIndices(self.mock_client)

    def test_create_all_calls_three_times(self):
        self.mock_client.indices.exists.return_value = False
        self.indices.create_all()
        self.assertEqual(self.mock_client.indices.create.call_count, 3)

    def test_create_all_skips_existing(self):
        self.mock_client.indices.exists.return_value = True
        self.indices.create_all()
        self.mock_client.indices.create.assert_not_called()

    def test_create_all_creates_aliases(self):
        self.mock_client.indices.exists.return_value = False
        self.indices.create_all()
        self.assertEqual(self.mock_client.indices.put_alias.call_count, 3)
        alias_calls = [call.kwargs for call in self.mock_client.indices.put_alias.call_args_list]
        alias_names = {c["name"] for c in alias_calls}
        self.assertEqual(alias_names, {PRODUCT_ALIAS, CATEGORY_ALIAS, BRAND_ALIAS})

    def test_switch_alias_handles_missing_alias(self):
        self.mock_client.indices.get_alias.side_effect = Exception("alias not found")
        self.indices.switch_alias()
        self.assertEqual(self.mock_client.indices.put_alias.call_count, 3)

    def test_switch_alias_removes_old_indices(self):
        self.mock_client.indices.get_alias.return_value = {
            "products-v0": {}, "products-v1": {}
        }
        self.indices._switch_alias(PRODUCT_INDEX_V1, PRODUCT_ALIAS)
        self.mock_client.indices.update_aliases.assert_called_once()

    def test_verify_returns_counts(self):
        self.mock_client.count.return_value = {"count": 42}
        self.mock_client.cluster.health.return_value = {"status": "green"}
        result = self.indices.verify()
        self.assertEqual(result["products"]["count"], 42)
        self.assertEqual(result["products"]["status"], "ok")

    def test_verify_handles_errors(self):
        self.mock_client.count.side_effect = Exception("ES down")
        result = self.indices.verify()
        self.assertEqual(result["products"]["status"], "error")

    def test_delete_old_indices_keeps_latest(self):
        self.mock_client.indices.get.return_value = {
            "products-v1": {}, "products-v2": {}, "products-v3": {}
        }
        self.indices.delete_old_indices(keep_versions=1)
        # 3 index groups × 2 old indices each = 6 deletes
        self.assertEqual(self.mock_client.indices.delete.call_count, 6)


# ---------------------------------------------------------------------------
# Index Mapping Tests
# ---------------------------------------------------------------------------


class IndexMappingTest(TestCase):

    def test_product_mapping_has_required_fields(self):
        props = PRODUCT_MAPPING["mappings"]["properties"]
        required = [
            "product_id", "sku", "name", "slug", "status",
            "brand", "category", "variants", "price", "rating",
            "review_count", "availability", "images",
        ]
        for field in required:
            self.assertIn(field, props, f"Missing product field: {field}")

    def test_category_mapping_has_required_fields(self):
        props = CATEGORY_MAPPING["mappings"]["properties"]
        required = ["category_id", "name", "slug", "is_active", "subcategories"]
        for field in required:
            self.assertIn(field, props, f"Missing category field: {field}")

    def test_brand_mapping_has_required_fields(self):
        props = BRAND_MAPPING["mappings"]["properties"]
        required = ["brand_id", "name", "slug", "is_active", "product_count"]
        for field in required:
            self.assertIn(field, props, f"Missing brand field: {field}")

    def test_product_mapping_has_nested_variants(self):
        props = PRODUCT_MAPPING["mappings"]["properties"]
        self.assertEqual(props["variants"]["type"], "nested")

    def test_product_mapping_has_nested_images(self):
        props = PRODUCT_MAPPING["mappings"]["properties"]
        self.assertEqual(props["images"]["type"], "nested")

    def test_product_mapping_has_autocomplete_analyzer(self):
        name_field = PRODUCT_MAPPING["mappings"]["properties"]["name"]
        self.assertIn("autocomplete", name_field["fields"])

    def test_product_mapping_settings_has_analyzers(self):
        settings = PRODUCT_MAPPING["settings"]["analysis"]
        self.assertIn("product_analyzer", settings["analyzer"])
        self.assertIn("autocomplete_analyzer", settings["analyzer"])


# ---------------------------------------------------------------------------
# SearchDocumentBuilder Tests
# ---------------------------------------------------------------------------


class SearchDocumentBuilderTest(TestCase):
    """Test SearchDocumentBuilder produces correct document structure."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.builder = SearchDocumentBuilder(self.mock_client)

        # Create test data
        self.category = Category.objects.create(name="Phones", slug="phones")
        self.brand = Brand.objects.create(name="Apple", slug="apple")
        self.product = Product.objects.create(
            sku="IPHONE-15", name="iPhone 15", slug="iphone-15",
            status="active", brand=self.brand, category=self.category,
            description="Latest iPhone",
        )
        self.variant = ProductVariant.objects.create(
            product=self.product, sku="VAR-001", name="128GB", status="active"
        )
        self.price = VariantPrice.objects.create(
            variant=self.variant, regular_price=Decimal("999.99"), currency="USD"
        )
        self.inventory = Inventory.objects.create(
            variant=self.variant, stock_quantity=50, reserved_quantity=5
        )
        self.image = ProductImage.objects.create(
            product=self.product, image_url="http://example.com/img.jpg",
            is_primary=True, display_order=1
        )
        self.spec = ProductSpecification.objects.create(
            product=self.product, name="RAM", value="8GB", unit="GB"
        )
        self.review = ProductReview.objects.create(
            product=self.product, user_id=1, rating=5, title="Great", status="approved"
        )

    def test_build_product_document_structure(self):
        doc = self.builder.build_product_document(self.product)
        self.assertEqual(doc["product_id"], self.product.pk)
        self.assertEqual(doc["sku"], "IPHONE-15")
        self.assertEqual(doc["name"], "iPhone 15")
        self.assertEqual(doc["status"], "active")
        self.assertIn("brand", doc)
        self.assertIn("category", doc)
        self.assertIn("variants", doc)
        self.assertIn("images", doc)
        self.assertIn("specifications", doc)
        self.assertIn("price", doc)
        self.assertIn("availability", doc)
        self.assertIn("rating", doc)
        self.assertIn("review_count", doc)

    def test_build_product_includes_brand(self):
        doc = self.builder.build_product_document(self.product)
        self.assertEqual(doc["brand"]["id"], self.brand.pk)
        self.assertEqual(doc["brand"]["name"], "Apple")

    def test_build_product_includes_category(self):
        doc = self.builder.build_product_document(self.product)
        self.assertEqual(doc["category"]["id"], self.category.pk)
        self.assertEqual(doc["category"]["name"], "Phones")

    def test_build_product_includes_variants(self):
        doc = self.builder.build_product_document(self.product)
        self.assertEqual(len(doc["variants"]), 1)
        v = doc["variants"][0]
        self.assertEqual(v["sku"], "VAR-001")
        self.assertEqual(v["price"]["regular"], 999.99)

    def test_build_product_includes_availability(self):
        doc = self.builder.build_product_document(self.product)
        self.assertTrue(doc["availability"]["in_stock"])
        self.assertEqual(doc["availability"]["quantity"], 45)

    def test_build_product_includes_images(self):
        doc = self.builder.build_product_document(self.product)
        self.assertEqual(len(doc["images"]), 1)
        self.assertEqual(doc["primary_image"], "http://example.com/img.jpg")

    def test_build_product_includes_specs(self):
        doc = self.builder.build_product_document(self.product)
        self.assertEqual(len(doc["specifications"]), 1)
        self.assertEqual(doc["specifications"][0]["name"], "RAM")

    def test_build_product_includes_rating(self):
        doc = self.builder.build_product_document(self.product)
        self.assertEqual(doc["rating"], 5.0)
        self.assertEqual(doc["review_count"], 1)

    def test_build_category_document(self):
        doc = self.builder.build_category_document(self.category)
        self.assertEqual(doc["category_id"], self.category.pk)
        self.assertEqual(doc["name"], "Phones")
        self.assertIn("subcategories", doc)
        self.assertIn("product_count", doc)

    def test_build_brand_document(self):
        doc = self.builder.build_brand_document(self.brand)
        self.assertEqual(doc["brand_id"], self.brand.pk)
        self.assertEqual(doc["name"], "Apple")
        self.assertIn("product_count", doc)

    def test_index_product_calls_es(self):
        self.builder.index_product(self.product)
        self.mock_client.index.assert_called_once()
        call_kwargs = self.mock_client.index.call_args.kwargs
        self.assertEqual(call_kwargs["index"], PRODUCT_ALIAS)

    def test_delete_product_calls_es(self):
        self.builder.delete_product(self.product.pk)
        self.mock_client.delete.assert_called_once()

    def test_delete_nonexistent_product_no_exception(self):
        self.mock_client.delete.side_effect = Exception("not found")
        self.builder.delete_product(99999)

    def test_reindex_products_returns_count(self):
        count = self.builder.reindex_products()
        self.assertEqual(count, 1)

    def test_reindex_categories_returns_count(self):
        count = self.builder.reindex_categories()
        self.assertEqual(count, 1)

    def test_reindex_brands_returns_count(self):
        count = self.builder.reindex_brands()
        self.assertEqual(count, 1)


# ---------------------------------------------------------------------------
# Event Consumer Tests
# ---------------------------------------------------------------------------


class CatalogEventConsumerTest(TestCase):
    """Test CatalogEventConsumer is idempotent and handles all event types."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.consumer = CatalogEventConsumer(self.mock_client)

        self.category = Category.objects.create(name="Phones", slug="phones")
        self.brand = Brand.objects.create(name="Apple", slug="apple")
        self.product = Product.objects.create(
            sku="IPHONE-15", name="iPhone 15", slug="iphone-15",
            status="active", brand=self.brand, category=self.category,
        )

    def _make_event(self, event_type, data):
        return {
            "event_id": str(uuid4()),
            "event_type": event_type,
            "aggregate_type": AggregateType.PRODUCT,
            "aggregate_id": str(data.get("product_id", data.get("category_id", data.get("brand_id", "")))),
            "version": 1,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }

    def test_product_created_indexes(self):
        event = self._make_event(EventType.PRODUCT_CREATED, {"product_id": self.product.pk})
        result = self.consumer.process_event(event)
        self.assertTrue(result)
        self.mock_client.index.assert_called()

    def test_product_updated_indexes(self):
        event = self._make_event(EventType.PRODUCT_UPDATED, {"product_id": self.product.pk})
        result = self.consumer.process_event(event)
        self.assertTrue(result)

    def test_product_deleted_removes(self):
        event = self._make_event(EventType.PRODUCT_DELETED, {"product_id": self.product.pk})
        result = self.consumer.process_event(event)
        self.assertTrue(result)
        self.mock_client.delete.assert_called()

    def test_product_published_indexes(self):
        event = self._make_event(EventType.PRODUCT_PUBLISHED, {"product_id": self.product.pk})
        result = self.consumer.process_event(event)
        self.assertTrue(result)

    def test_product_archived_indexes(self):
        event = self._make_event(EventType.PRODUCT_ARCHIVED, {"product_id": self.product.pk})
        result = self.consumer.process_event(event)
        self.assertTrue(result)

    def test_category_created_indexes(self):
        event = self._make_event(EventType.CATEGORY_CREATED, {"category_id": self.category.pk})
        result = self.consumer.process_event(event)
        self.assertTrue(result)

    def test_brand_created_indexes(self):
        event = self._make_event(EventType.BRAND_CREATED, {"brand_id": self.brand.pk})
        result = self.consumer.process_event(event)
        self.assertTrue(result)

    def test_invalid_event_returns_false(self):
        result = self.consumer.process_event({"invalid": "event"})
        self.assertFalse(result)

    def test_unknown_event_type_returns_false(self):
        event = {
            "event_type": "UnknownEvent",
            "aggregate_type": "test",
            "aggregate_id": "1",
            "version": 1,
            "data": {},
        }
        result = self.consumer.process_event(event)
        self.assertFalse(result)

    def test_idempotent_processing(self):
        event = self._make_event(EventType.PRODUCT_CREATED, {"product_id": self.product.pk})
        result1 = self.consumer.process_event(event)
        result2 = self.consumer.process_event(event)
        self.assertTrue(result1)
        self.assertTrue(result2)
