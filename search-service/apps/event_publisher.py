"""Event publisher service for the Transactional Outbox pattern.

This module provides functions to publish domain events within database
transactions. Events are written to the OutboxEvent table in the same
transaction as the domain model changes, ensuring atomicity.

A background relay process (see event_relay.py) publishes pending events
to the message broker (Kafka).
"""

import logging
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.models import OutboxEvent

logger = logging.getLogger("catalog.events")


# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------


class EventType:
    """Domain event type constants."""

    # Product events
    PRODUCT_CREATED = "ProductCreated"
    PRODUCT_UPDATED = "ProductUpdated"
    PRODUCT_DELETED = "ProductDeleted"
    PRODUCT_PUBLISHED = "ProductPublished"
    PRODUCT_ARCHIVED = "ProductArchived"

    # Category events
    CATEGORY_CREATED = "CategoryCreated"
    CATEGORY_UPDATED = "CategoryUpdated"
    CATEGORY_DELETED = "CategoryDeleted"

    # Subcategory events
    SUBCATEGORY_CREATED = "SubcategoryCreated"
    SUBCATEGORY_UPDATED = "SubcategoryUpdated"
    SUBCATEGORY_DELETED = "SubcategoryDeleted"

    # Brand events
    BRAND_CREATED = "BrandCreated"
    BRAND_UPDATED = "BrandUpdated"
    BRAND_DELETED = "BrandDeleted"

    # Product Image events
    PRODUCT_IMAGE_ADDED = "ProductImageAdded"
    PRODUCT_IMAGE_REMOVED = "ProductImageRemoved"

    # Product Variant events
    PRODUCT_VARIANT_CREATED = "ProductVariantCreated"
    PRODUCT_VARIANT_UPDATED = "ProductVariantUpdated"

    # Price events
    PRICE_CREATED = "PriceCreated"
    PRICE_UPDATED = "PriceUpdated"

    # Inventory events
    INVENTORY_UPDATED = "InventoryUpdated"

    # Review events
    PRODUCT_REVIEW_CREATED = "ProductReviewCreated"
    PRODUCT_REVIEW_UPDATED = "ProductReviewUpdated"
    PRODUCT_REVIEW_APPROVED = "ProductReviewApproved"
    PRODUCT_REVIEW_REJECTED = "ProductReviewRejected"
    PRODUCT_REVIEW_DELETED = "ProductReviewDeleted"


class AggregateType:
    """Aggregate type constants."""

    PRODUCT = "product"
    CATEGORY = "category"
    SUBCATEGORY = "subcategory"
    BRAND = "brand"
    PRODUCT_IMAGE = "product_image"
    PRODUCT_VARIANT = "product_variant"
    VARIANT_PRICE = "variant_price"
    INVENTORY = "inventory"
    PRODUCT_REVIEW = "product_review"


# ---------------------------------------------------------------------------
# Core event publishing functions
# ---------------------------------------------------------------------------


def publish_event(
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict,
    delay_seconds: int = 0,
) -> OutboxEvent:
    """Publish a domain event to the outbox.

    This function should be called WITHIN an existing transaction.
    The event will be committed only if the calling transaction commits.

    Args:
        event_type: The type of event (e.g., EventType.PRODUCT_CREATED)
        aggregate_type: The aggregate type (e.g., AggregateType.PRODUCT)
        aggregate_id: The ID of the aggregate
        payload: Event payload data
        delay_seconds: Delay before event becomes available for publishing

    Returns:
        The created OutboxEvent instance
    """
    event_id = str(uuid.uuid4())
    now = timezone.now()
    available_at = now + timedelta(seconds=delay_seconds)

    event = OutboxEvent(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        payload={
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": str(aggregate_id),
            "version": 1,
            "occurred_at": now.isoformat(),
            "data": payload,
        },
        status=OutboxEvent.STATUS_PENDING,
        available_at=available_at,
    )
    event.save()

    logger.info(
        "Event published to outbox",
        extra={
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
        },
    )

    return event


def publish_events(events: list[dict]) -> list[OutboxEvent]:
    """Publish multiple domain events to the outbox.

    This function should be called WITHIN an existing transaction.

    Args:
        events: List of event dictionaries with keys:
            - event_type
            - aggregate_type
            - aggregate_id
            - payload
            - delay_seconds (optional)

    Returns:
        List of created OutboxEvent instances
    """
    created_events = []

    for event_data in events:
        event = publish_event(
            event_type=event_data["event_type"],
            aggregate_type=event_data["aggregate_type"],
            aggregate_id=event_data["aggregate_id"],
            payload=event_data["payload"],
            delay_seconds=event_data.get("delay_seconds", 0),
        )
        created_events.append(event)

    return created_events


# ---------------------------------------------------------------------------
# Convenience functions for common event patterns
# ---------------------------------------------------------------------------


def publish_product_event(event_type: str, product, **extra_data):
    """Publish a product-related event."""
    payload = {
        "product_id": product.pk,
        "sku": product.sku,
        "name": product.name,
        "slug": product.slug,
        "status": product.status,
        "is_featured": product.is_featured,
        "brand_id": product.brand_id,
        "category_id": product.category_id,
        "subcategory_id": product.subcategory_id,
        "created_by": product.created_by,
        "updated_by": product.updated_by,
        **extra_data,
    }
    return publish_event(
        event_type=event_type,
        aggregate_type=AggregateType.PRODUCT,
        aggregate_id=product.pk,
        payload=payload,
    )


def publish_category_event(event_type: str, category, **extra_data):
    """Publish a category-related event."""
    payload = {
        "category_id": category.pk,
        "name": category.name,
        "slug": category.slug,
        "is_active": category.is_active,
        "display_order": category.display_order,
        **extra_data,
    }
    return publish_event(
        event_type=event_type,
        aggregate_type=AggregateType.CATEGORY,
        aggregate_id=category.pk,
        payload=payload,
    )


def publish_subcategory_event(event_type: str, subcategory, **extra_data):
    """Publish a subcategory-related event."""
    payload = {
        "subcategory_id": subcategory.pk,
        "category_id": subcategory.category_id,
        "name": subcategory.name,
        "slug": subcategory.slug,
        "is_active": subcategory.is_active,
        "display_order": subcategory.display_order,
        **extra_data,
    }
    return publish_event(
        event_type=event_type,
        aggregate_type=AggregateType.SUBCATEGORY,
        aggregate_id=subcategory.pk,
        payload=payload,
    )


def publish_brand_event(event_type: str, brand, **extra_data):
    """Publish a brand-related event."""
    payload = {
        "brand_id": brand.pk,
        "name": brand.name,
        "slug": brand.slug,
        "is_active": brand.is_active,
        "website": brand.website,
        **extra_data,
    }
    return publish_event(
        event_type=event_type,
        aggregate_type=AggregateType.BRAND,
        aggregate_id=brand.pk,
        payload=payload,
    )


def publish_image_event(event_type: str, image, **extra_data):
    """Publish a product image event."""
    payload = {
        "image_id": image.pk,
        "product_id": image.product_id,
        "image_url": image.image_url,
        "alt_text": image.alt_text,
        "is_primary": image.is_primary,
        "display_order": image.display_order,
        **extra_data,
    }
    return publish_event(
        event_type=event_type,
        aggregate_type=AggregateType.PRODUCT_IMAGE,
        aggregate_id=image.pk,
        payload=payload,
    )


def publish_variant_event(event_type: str, variant, **extra_data):
    """Publish a product variant event."""
    payload = {
        "variant_id": variant.pk,
        "product_id": variant.product_id,
        "sku": variant.sku,
        "barcode": variant.barcode,
        "name": variant.name,
        "status": variant.status,
        **extra_data,
    }
    return publish_event(
        event_type=event_type,
        aggregate_type=AggregateType.PRODUCT_VARIANT,
        aggregate_id=variant.pk,
        payload=payload,
    )


def publish_price_event(event_type: str, price, **extra_data):
    """Publish a variant price event."""
    payload = {
        "price_id": price.pk,
        "variant_id": price.variant_id,
        "regular_price": str(price.regular_price),
        "sale_price": str(price.sale_price) if price.sale_price else None,
        "currency": price.currency,
        "effective_from": price.effective_from.isoformat() if price.effective_from else None,
        "effective_to": price.effective_to.isoformat() if price.effective_to else None,
        "is_active": price.is_active,
        **extra_data,
    }
    return publish_event(
        event_type=event_type,
        aggregate_type=AggregateType.VARIANT_PRICE,
        aggregate_id=price.pk,
        payload=payload,
    )


def publish_inventory_event(inventory, action: str, **extra_data):
    """Publish an inventory update event."""
    payload = {
        "inventory_id": inventory.pk,
        "variant_id": inventory.variant_id,
        "stock_quantity": inventory.stock_quantity,
        "reserved_quantity": inventory.reserved_quantity,
        "available_quantity": inventory.available_quantity,
        "low_stock_threshold": inventory.low_stock_threshold,
        "is_in_stock": inventory.is_in_stock,
        "is_low_stock": inventory.is_low_stock,
        "action": action,
        **extra_data,
    }
    return publish_event(
        event_type=EventType.INVENTORY_UPDATED,
        aggregate_type=AggregateType.INVENTORY,
        aggregate_id=inventory.pk,
        payload=payload,
    )


def publish_review_event(event_type: str, review, **extra_data):
    """Publish a product review event."""
    payload = {
        "review_id": review.pk,
        "product_id": review.product_id,
        "user_id": review.user_id,
        "rating": review.rating,
        "title": review.title,
        "status": review.status,
        **extra_data,
    }
    return publish_event(
        event_type=event_type,
        aggregate_type=AggregateType.PRODUCT_REVIEW,
        aggregate_id=review.pk,
        payload=payload,
    )
