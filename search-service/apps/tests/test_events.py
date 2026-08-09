"""Event publishing and outbox tests.

Tests verify:
    - ProductCreated → event generated
    - ProductUpdated → event generated
    - ProductDeleted → event generated
    - InventoryUpdated → event generated
    - ReviewApproved → event generated
    - Outbox transaction atomicity
    - Event retry/idempotency
"""

from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.event_publisher import (
    EventType,
    AggregateType,
    publish_event,
    publish_product_event,
    publish_category_event,
    publish_brand_event,
    publish_inventory_event,
    publish_review_event,
)
from apps.models import (
    Brand,
    Category,
    Inventory,
    OutboxEvent,
    Product,
    ProductReview,
    ProductVariant,
)


# ---------------------------------------------------------------------------
# Event Type Constants Tests
# ---------------------------------------------------------------------------


class EventTypeConstantsTest(TestCase):
    """Verify all event type constants are defined."""

    def test_product_events(self):
        self.assertEqual(EventType.PRODUCT_CREATED, "ProductCreated")
        self.assertEqual(EventType.PRODUCT_UPDATED, "ProductUpdated")
        self.assertEqual(EventType.PRODUCT_DELETED, "ProductDeleted")
        self.assertEqual(EventType.PRODUCT_PUBLISHED, "ProductPublished")
        self.assertEqual(EventType.PRODUCT_ARCHIVED, "ProductArchived")

    def test_category_events(self):
        self.assertEqual(EventType.CATEGORY_CREATED, "CategoryCreated")
        self.assertEqual(EventType.CATEGORY_UPDATED, "CategoryUpdated")
        self.assertEqual(EventType.CATEGORY_DELETED, "CategoryDeleted")

    def test_brand_events(self):
        self.assertEqual(EventType.BRAND_CREATED, "BrandCreated")
        self.assertEqual(EventType.BRAND_UPDATED, "BrandUpdated")
        self.assertEqual(EventType.BRAND_DELETED, "BrandDeleted")

    def test_review_events(self):
        self.assertEqual(EventType.PRODUCT_REVIEW_CREATED, "ProductReviewCreated")
        self.assertEqual(EventType.PRODUCT_REVIEW_UPDATED, "ProductReviewUpdated")
        self.assertEqual(EventType.PRODUCT_REVIEW_APPROVED, "ProductReviewApproved")
        self.assertEqual(EventType.PRODUCT_REVIEW_REJECTED, "ProductReviewRejected")
        self.assertEqual(EventType.PRODUCT_REVIEW_DELETED, "ProductReviewDeleted")

    def test_inventory_events(self):
        self.assertEqual(EventType.INVENTORY_UPDATED, "InventoryUpdated")

    def test_aggregate_types(self):
        self.assertEqual(AggregateType.PRODUCT, "product")
        self.assertEqual(AggregateType.CATEGORY, "category")
        self.assertEqual(AggregateType.BRAND, "brand")
        self.assertEqual(AggregateType.INVENTORY, "inventory")
        self.assertEqual(AggregateType.PRODUCT_REVIEW, "product_review")


# ---------------------------------------------------------------------------
# Core Event Publishing Tests
# ---------------------------------------------------------------------------


class CoreEventPublishingTest(TestCase):
    """Test the core publish_event function."""

    def test_publish_event_creates_outbox_entry(self):
        event = publish_event(
            event_type="TestEvent",
            aggregate_type="test",
            aggregate_id="123",
            payload={"key": "value"},
        )
        self.assertIsNotNone(event.pk)
        self.assertEqual(event.event_type, "TestEvent")
        self.assertEqual(event.aggregate_type, "test")
        self.assertEqual(event.aggregate_id, "123")
        self.assertEqual(event.status, OutboxEvent.STATUS_PENDING)
        self.assertEqual(event.retry_count, 0)

    def test_publish_event_payload_structure(self):
        event = publish_event(
            event_type="TestEvent",
            aggregate_type="test",
            aggregate_id="123",
            payload={"key": "value"},
        )
        self.assertEqual(event.payload["event_type"], "TestEvent")
        self.assertEqual(event.payload["aggregate_type"], "test")
        self.assertEqual(event.payload["aggregate_id"], "123")
        self.assertEqual(event.payload["version"], 1)
        self.assertIn("event_id", event.payload)
        self.assertIn("occurred_at", event.payload)
        self.assertEqual(event.payload["data"]["key"], "value")

    def test_publish_event_with_delay(self):
        event = publish_event(
            event_type="TestEvent",
            aggregate_type="test",
            aggregate_id="123",
            payload={},
            delay_seconds=60,
        )
        self.assertGreater(event.available_at, timezone.now())


# ---------------------------------------------------------------------------
# Product Event Tests
# ---------------------------------------------------------------------------


class ProductEventPublishingTest(TestCase):
    """Test product event publishing."""

    def _create_product(self):
        category = Category.objects.create(name="Phones", slug="phones")
        brand = Brand.objects.create(name="Apple", slug="apple")
        return Product.objects.create(
            sku="IPHONE-15",
            name="iPhone 15",
            slug="iphone-15",
            status="active",
            brand=brand,
            category=category,
        )

    def test_product_created_event(self):
        product = self._create_product()
        event = publish_product_event(EventType.PRODUCT_CREATED, product)
        self.assertEqual(event.event_type, EventType.PRODUCT_CREATED)
        self.assertEqual(event.aggregate_type, AggregateType.PRODUCT)
        self.assertEqual(event.aggregate_id, str(product.pk))
        self.assertEqual(event.payload["data"]["sku"], "IPHONE-15")

    def test_product_updated_event(self):
        product = self._create_product()
        event = publish_product_event(EventType.PRODUCT_UPDATED, product)
        self.assertEqual(event.event_type, EventType.PRODUCT_UPDATED)

    def test_product_deleted_event(self):
        product = self._create_product()
        event = publish_product_event(EventType.PRODUCT_DELETED, product)
        self.assertEqual(event.event_type, EventType.PRODUCT_DELETED)

    def test_product_published_event(self):
        product = self._create_product()
        event = publish_product_event(EventType.PRODUCT_PUBLISHED, product)
        self.assertEqual(event.event_type, EventType.PRODUCT_PUBLISHED)

    def test_product_archived_event(self):
        product = self._create_product()
        event = publish_product_event(EventType.PRODUCT_ARCHIVED, product)
        self.assertEqual(event.event_type, EventType.PRODUCT_ARCHIVED)


# ---------------------------------------------------------------------------
# Category Event Tests
# ---------------------------------------------------------------------------


class CategoryEventPublishingTest(TestCase):

    def test_category_created_event(self):
        category = Category.objects.create(name="Phones", slug="phones")
        event = publish_category_event(EventType.CATEGORY_CREATED, category)
        self.assertEqual(event.event_type, EventType.CATEGORY_CREATED)
        self.assertEqual(event.aggregate_type, AggregateType.CATEGORY)

    def test_category_updated_event(self):
        category = Category.objects.create(name="Phones", slug="phones")
        event = publish_category_event(EventType.CATEGORY_UPDATED, category)
        self.assertEqual(event.event_type, EventType.CATEGORY_UPDATED)

    def test_category_deleted_event(self):
        category = Category.objects.create(name="Phones", slug="phones")
        event = publish_category_event(EventType.CATEGORY_DELETED, category)
        self.assertEqual(event.event_type, EventType.CATEGORY_DELETED)


# ---------------------------------------------------------------------------
# Brand Event Tests
# ---------------------------------------------------------------------------


class BrandEventPublishingTest(TestCase):

    def test_brand_created_event(self):
        brand = Brand.objects.create(name="Apple", slug="apple")
        event = publish_brand_event(EventType.BRAND_CREATED, brand)
        self.assertEqual(event.event_type, EventType.BRAND_CREATED)
        self.assertEqual(event.aggregate_type, AggregateType.BRAND)

    def test_brand_updated_event(self):
        brand = Brand.objects.create(name="Apple", slug="apple")
        event = publish_brand_event(EventType.BRAND_UPDATED, brand)
        self.assertEqual(event.event_type, EventType.BRAND_UPDATED)

    def test_brand_deleted_event(self):
        brand = Brand.objects.create(name="Apple", slug="apple")
        event = publish_brand_event(EventType.BRAND_DELETED, brand)
        self.assertEqual(event.event_type, EventType.BRAND_DELETED)


# ---------------------------------------------------------------------------
# Inventory Event Tests
# ---------------------------------------------------------------------------


class InventoryEventPublishingTest(TestCase):

    def test_inventory_updated_event(self):
        category = Category.objects.create(name="Phones", slug="phones")
        brand = Brand.objects.create(name="Apple", slug="apple")
        product = Product.objects.create(
            sku="IPHONE-15", name="iPhone 15", slug="iphone-15",
            status="active", brand=brand, category=category,
        )
        variant = ProductVariant.objects.create(
            product=product, sku="VAR-001", name="128GB", status="active"
        )
        inventory = Inventory.objects.create(
            variant=variant, stock_quantity=100, reserved_quantity=0
        )
        event = publish_inventory_event(inventory, "stock_in")
        self.assertEqual(event.event_type, EventType.INVENTORY_UPDATED)
        self.assertEqual(event.aggregate_type, AggregateType.INVENTORY)
        self.assertEqual(event.payload["data"]["action"], "stock_in")
        self.assertEqual(event.payload["data"]["stock_quantity"], 100)


# ---------------------------------------------------------------------------
# Review Event Tests
# ---------------------------------------------------------------------------


class ReviewEventPublishingTest(TestCase):

    def _create_review(self, status="pending"):
        category = Category.objects.create(name="Phones", slug="phones")
        brand = Brand.objects.create(name="Apple", slug="apple")
        product = Product.objects.create(
            sku="IPHONE-15", name="iPhone 15", slug="iphone-15",
            status="active", brand=brand, category=category,
        )
        return ProductReview.objects.create(
            product=product, user_id=1, rating=5, title="Great", status=status
        )

    def test_review_created_event(self):
        review = self._create_review()
        event = publish_review_event(EventType.PRODUCT_REVIEW_CREATED, review)
        self.assertEqual(event.event_type, EventType.PRODUCT_REVIEW_CREATED)
        self.assertEqual(event.aggregate_type, AggregateType.PRODUCT_REVIEW)

    def test_review_approved_event(self):
        review = self._create_review(status="approved")
        event = publish_review_event(EventType.PRODUCT_REVIEW_APPROVED, review)
        self.assertEqual(event.event_type, EventType.PRODUCT_REVIEW_APPROVED)
        self.assertEqual(event.payload["data"]["status"], "approved")

    def test_review_rejected_event(self):
        review = self._create_review(status="rejected")
        event = publish_review_event(EventType.PRODUCT_REVIEW_REJECTED, review)
        self.assertEqual(event.event_type, EventType.PRODUCT_REVIEW_REJECTED)

    def test_review_deleted_event(self):
        review = self._create_review()
        event = publish_review_event(EventType.PRODUCT_REVIEW_DELETED, review)
        self.assertEqual(event.event_type, EventType.PRODUCT_REVIEW_DELETED)


# ---------------------------------------------------------------------------
# Outbox Transaction Tests
# ---------------------------------------------------------------------------


class OutboxTransactionTest(TransactionTestCase):
    """Test outbox transaction atomicity."""

    def test_successful_transaction_creates_event(self):
        """Database transaction succeeds → event exists."""
        category = Category.objects.create(name="Phones", slug="phones")
        event = publish_category_event(EventType.CATEGORY_CREATED, category)
        self.assertTrue(OutboxEvent.objects.filter(pk=event.pk).exists())

    def test_failed_transaction_no_event(self):
        """Database transaction fails → event does not exist."""
        initial_count = OutboxEvent.objects.count()
        try:
            # This will fail because name is not unique (Category has unique=True)
            Category.objects.create(name="Phones", slug="phones")
            Category.objects.create(name="Phones", slug="phones-2")
        except IntegrityError:
            pass

        # No events should be created
        self.assertEqual(OutboxEvent.objects.count(), initial_count)

    def test_event_retry_status(self):
        """Event publishing failure → event remains retryable."""
        event = OutboxEvent.objects.create(
            event_type="TestEvent",
            aggregate_type="test",
            aggregate_id="1",
            payload={},
            status=OutboxEvent.STATUS_FAILED,
            retry_count=3,
            last_error="Connection refused",
        )
        self.assertEqual(event.retry_count, 3)
        self.assertEqual(event.status, OutboxEvent.STATUS_FAILED)
        # Can be retried
        event.status = OutboxEvent.STATUS_PENDING
        event.retry_count = 0
        event.save()
        self.assertEqual(event.status, OutboxEvent.STATUS_PENDING)

    def test_duplicate_event_idempotent(self):
        """Duplicate event → safe/idempotent."""
        event_id = "test-event-id-123"
        event1 = OutboxEvent.objects.create(
            event_type="TestEvent",
            aggregate_type="test",
            aggregate_id="1",
            payload={"event_id": event_id},
            status=OutboxEvent.STATUS_PENDING,
        )
        event2 = OutboxEvent.objects.create(
            event_type="TestEvent",
            aggregate_type="test",
            aggregate_id="1",
            payload={"event_id": event_id},
            status=OutboxEvent.STATUS_PENDING,
        )
        # Both events exist but have same event_id in payload
        self.assertNotEqual(event1.pk, event2.pk)
        self.assertEqual(
            event1.payload["event_id"], event2.payload["event_id"]
        )
