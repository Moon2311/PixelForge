"""Event consumers for catalog domain events.

Consumes events from the outbox (via Kafka or directly) and updates the
Elasticsearch projection. Implements idempotent processing to handle
duplicate events safely.

Usage:
    from apps.event_consumers import CatalogEventConsumer

    consumer = CatalogEventConsumer(es_client)
    consumer.process_event(event_payload)
"""

import logging
from typing import Any

from elasticsearch import Elasticsearch

from apps.event_contracts import EventType, validate_event
from apps.models import (
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
from apps.search_document_builder import SearchDocumentBuilder

logger = logging.getLogger("catalog.search.consumer")


class CatalogEventConsumer:
    """Consumes catalog events and updates Elasticsearch projection.

    All handlers are idempotent - processing the same event twice
    produces the same result.
    """

    def __init__(self, client: Elasticsearch):
        self.client = client
        self.builder = SearchDocumentBuilder(client)

    def process_event(self, event_data: dict[str, Any]) -> bool:
        """Process a single catalog event.

        Args:
            event_data: The event payload (envelope with data field)

        Returns:
            True if processed successfully, False otherwise
        """
        # Validate event structure
        errors = validate_event(event_data)
        if errors:
            logger.error(f"Invalid event: {errors}")
            return False

        event_type = event_data["event_type"]
        data = event_data["data"]

        try:
            handler = self._get_handler(event_type)
            if handler:
                handler(data)
                logger.info(
                    f"Processed event: {event_type}",
                    extra={
                        "event_id": event_data.get("event_id"),
                        "event_type": event_type,
                        "aggregate_id": event_data.get("aggregate_id"),
                    },
                )
                return True
            else:
                logger.warning(f"No handler for event type: {event_type}")
                return False
        except Exception as e:
            logger.error(
                f"Error processing event {event_type}: {e}",
                exc_info=True,
                extra={
                    "event_id": event_data.get("event_id"),
                    "event_type": event_type,
                    "error": str(e),
                },
            )
            return False

    def _get_handler(self, event_type: str):
        """Get the handler function for an event type."""
        handlers = {
            # Product events
            EventType.PRODUCT_CREATED: self._handle_product_created,
            EventType.PRODUCT_UPDATED: self._handle_product_updated,
            EventType.PRODUCT_DELETED: self._handle_product_deleted,
            EventType.PRODUCT_PUBLISHED: self._handle_product_published,
            EventType.PRODUCT_ARCHIVED: self._handle_product_archived,
            # Category events
            EventType.CATEGORY_CREATED: self._handle_category_created,
            EventType.CATEGORY_UPDATED: self._handle_category_updated,
            EventType.CATEGORY_DELETED: self._handle_category_deleted,
            # Subcategory events
            EventType.SUBCATEGORY_CREATED: self._handle_subcategory_created,
            EventType.SUBCATEGORY_UPDATED: self._handle_subcategory_updated,
            EventType.SUBCATEGORY_DELETED: self._handle_subcategory_deleted,
            # Brand events
            EventType.BRAND_CREATED: self._handle_brand_created,
            EventType.BRAND_UPDATED: self._handle_brand_updated,
            EventType.BRAND_DELETED: self._handle_brand_deleted,
            # Image events
            EventType.PRODUCT_IMAGE_ADDED: self._handle_product_image_added,
            EventType.PRODUCT_IMAGE_REMOVED: self._handle_product_image_removed,
            # Variant events
            EventType.PRODUCT_VARIANT_CREATED: self._handle_variant_created,
            EventType.PRODUCT_VARIANT_UPDATED: self._handle_variant_updated,
            # Price events
            EventType.PRICE_CREATED: self._handle_price_created,
            EventType.PRICE_UPDATED: self._handle_price_updated,
            # Inventory events
            EventType.INVENTORY_UPDATED: self._handle_inventory_updated,
            # Review events
            EventType.PRODUCT_REVIEW_CREATED: self._handle_review_created,
            EventType.PRODUCT_REVIEW_UPDATED: self._handle_review_updated,
            EventType.PRODUCT_REVIEW_APPROVED: self._handle_review_approved,
            EventType.PRODUCT_REVIEW_REJECTED: self._handle_review_rejected,
            EventType.PRODUCT_REVIEW_DELETED: self._handle_review_deleted,
        }
        return handlers.get(event_type)

    # ------------------------------------------------------------------
    # Product handlers
    # ------------------------------------------------------------------

    def _handle_product_created(self, data: dict):
        """Handle ProductCreated event."""
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product:
            self.builder.index_product(product)

    def _handle_product_updated(self, data: dict):
        """Handle ProductUpdated event."""
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product and not product.is_deleted:
            self.builder.index_product(product)
        else:
            # Product was deleted or not found - remove from ES
            self.builder.delete_product(product_id)

    def _handle_product_deleted(self, data: dict):
        """Handle ProductDeleted event."""
        product_id = data.get("product_id")
        self.builder.delete_product(product_id)

    def _handle_product_published(self, data: dict):
        """Handle ProductPublished event (status -> active)."""
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product:
            self.builder.index_product(product)

    def _handle_product_archived(self, data: dict):
        """Handle ProductArchived event (status -> inactive/archived)."""
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id, is_deleted=False).first()
        if product:
            self.builder.index_product(product)
        else:
            self.builder.delete_product(product_id)

    # ------------------------------------------------------------------
    # Category handlers
    # ------------------------------------------------------------------

    def _handle_category_created(self, data: dict):
        """Handle CategoryCreated event."""
        category_id = data.get("category_id")
        category = Category.objects.filter(pk=category_id).first()
        if category:
            self.builder.index_category(category)

    def _handle_category_updated(self, data: dict):
        """Handle CategoryUpdated event."""
        category_id = data.get("category_id")
        category = Category.objects.filter(pk=category_id).first()
        if category and not category.is_deleted:
            self.builder.index_category(category)
            # Reindex all products in this category
            self._reindex_category_products(category_id)
        else:
            self.builder.delete_category(category_id)

    def _handle_category_deleted(self, data: dict):
        """Handle CategoryDeleted event."""
        category_id = data.get("category_id")
        # Remove category from ES
        self.builder.delete_category(category_id)
        # Remove all products in this category from ES
        products = Product.objects.filter(
            category_id=category_id, is_deleted=False
        )
        for product in products:
            self.builder.delete_product(product.pk)

    # ------------------------------------------------------------------
    # Subcategory handlers
    # ------------------------------------------------------------------

    def _handle_subcategory_created(self, data: dict):
        """Handle SubcategoryCreated event."""
        # Reindex the parent category (includes subcategories)
        category_id = data.get("category_id")
        if category_id:
            category = Category.objects.filter(pk=category_id).first()
            if category:
                self.builder.index_category(category)

    def _handle_subcategory_updated(self, data: dict):
        """Handle SubcategoryUpdated event."""
        category_id = data.get("category_id")
        if category_id:
            category = Category.objects.filter(pk=category_id).first()
            if category:
                self.builder.index_category(category)
                self._reindex_category_products(category_id)

    def _handle_subcategory_deleted(self, data: dict):
        """Handle SubcategoryDeleted event."""
        category_id = data.get("category_id")
        if category_id:
            category = Category.objects.filter(pk=category_id).first()
            if category:
                self.builder.index_category(category)
            # Reindex products that had this subcategory
            subcategory_id = data.get("subcategory_id")
            products = Product.objects.filter(
                subcategory_id=subcategory_id, is_deleted=False
            )
            for product in products:
                self.builder.index_product(product)

    # ------------------------------------------------------------------
    # Brand handlers
    # ------------------------------------------------------------------

    def _handle_brand_created(self, data: dict):
        """Handle BrandCreated event."""
        brand_id = data.get("brand_id")
        brand = Brand.objects.filter(pk=brand_id).first()
        if brand:
            self.builder.index_brand(brand)

    def _handle_brand_updated(self, data: dict):
        """Handle BrandUpdated event."""
        brand_id = data.get("brand_id")
        brand = Brand.objects.filter(pk=brand_id).first()
        if brand and not brand.is_deleted:
            self.builder.index_brand(brand)
            # Reindex all products for this brand
            self._reindex_brand_products(brand_id)
        else:
            self.builder.delete_brand(brand_id)

    def _handle_brand_deleted(self, data: dict):
        """Handle BrandDeleted event."""
        brand_id = data.get("brand_id")
        self.builder.delete_brand(brand_id)
        # Remove all products for this brand from ES
        products = Product.objects.filter(
            brand_id=brand_id, is_deleted=False
        )
        for product in products:
            self.builder.delete_product(product.pk)

    # ------------------------------------------------------------------
    # Image handlers
    # ------------------------------------------------------------------

    def _handle_product_image_added(self, data: dict):
        """Handle ProductImageAdded event."""
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product:
            self.builder.index_product(product)

    def _handle_product_image_removed(self, data: dict):
        """Handle ProductImageRemoved event."""
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product:
            self.builder.index_product(product)

    # ------------------------------------------------------------------
    # Variant handlers
    # ------------------------------------------------------------------

    def _handle_variant_created(self, data: dict):
        """Handle ProductVariantCreated event."""
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product:
            self.builder.index_product(product)

    def _handle_variant_updated(self, data: dict):
        """Handle ProductVariantUpdated event."""
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product:
            self.builder.index_product(product)

    # ------------------------------------------------------------------
    # Price handlers
    # ------------------------------------------------------------------

    def _handle_price_created(self, data: dict):
        """Handle PriceCreated event."""
        variant_id = data.get("variant_id")
        variant = ProductVariant.objects.filter(pk=variant_id).first()
        if variant:
            self.builder.index_product(variant.product)

    def _handle_price_updated(self, data: dict):
        """Handle PriceUpdated event."""
        variant_id = data.get("variant_id")
        variant = ProductVariant.objects.filter(pk=variant_id).first()
        if variant:
            self.builder.index_product(variant.product)

    # ------------------------------------------------------------------
    # Inventory handlers
    # ------------------------------------------------------------------

    def _handle_inventory_updated(self, data: dict):
        """Handle InventoryUpdated event."""
        variant_id = data.get("variant_id")
        variant = ProductVariant.objects.filter(pk=variant_id).first()
        if variant:
            self.builder.index_product(variant.product)

    # ------------------------------------------------------------------
    # Review handlers
    # ------------------------------------------------------------------

    def _handle_review_created(self, data: dict):
        """Handle ProductReviewCreated event."""
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product:
            self.builder.index_product(product)

    def _handle_review_updated(self, data: dict):
        """Handle ProductReviewUpdated event."""
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product:
            self.builder.index_product(product)

    def _handle_review_approved(self, data: dict):
        """Handle ProductReviewApproved event.

        Only approved reviews affect the product's rating/review_count
        in Elasticsearch. Re-index the product to update these fields.
        """
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product:
            self.builder.index_product(product)

    def _handle_review_rejected(self, data: dict):
        """Handle ProductReviewRejected event.

        Rejected reviews don't count toward rating/review_count,
        but we still re-index to ensure consistency.
        """
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product:
            self.builder.index_product(product)

    def _handle_review_deleted(self, data: dict):
        """Handle ProductReviewDeleted event.

        Re-index the product to update rating/review_count
        since the deleted review no longer contributes.
        """
        product_id = data.get("product_id")
        product = Product.objects.filter(pk=product_id).first()
        if product:
            self.builder.index_product(product)

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _reindex_category_products(self, category_id: int):
        """Reindex all products in a category."""
        products = Product.objects.filter(
            category_id=category_id, is_deleted=False
        )
        for product in products:
            self.builder.index_product(product)

    def _reindex_brand_products(self, brand_id: int):
        """Reindex all products for a brand."""
        products = Product.objects.filter(
            brand_id=brand_id, is_deleted=False
        )
        for product in products:
            self.builder.index_product(product)
