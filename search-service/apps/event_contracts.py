"""Event contracts for Product Catalog domain events.

This module defines the schema and validation for all domain events
published by the Product Catalog service. Search Service should use
these contracts to validate incoming events.

Event Versioning:
- Events include a version field in the payload
- The current version is 1
- Backward-incompatible changes require a new version
- Consumers should handle multiple versions during migration windows
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Event Type Enums
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    """All supported domain event types."""

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


class AggregateType(str, Enum):
    """All supported aggregate types."""

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
# Base Event Schema
# ---------------------------------------------------------------------------


@dataclass
class EventEnvelope:
    """Base event envelope containing metadata and payload.

    This is the standard structure for all domain events.
    """

    event_id: str
    event_type: EventType
    aggregate_type: AggregateType
    aggregate_id: str
    version: int
    occurred_at: datetime
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "aggregate_type": self.aggregate_type.value,
            "aggregate_id": self.aggregate_id,
            "version": self.version,
            "occurred_at": self.occurred_at.isoformat(),
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EventEnvelope":
        """Create from dictionary."""
        return cls(
            event_id=data["event_id"],
            event_type=EventType(data["event_type"]),
            aggregate_type=AggregateType(data["aggregate_type"]),
            aggregate_id=data["aggregate_id"],
            version=data["version"],
            occurred_at=datetime.fromisoformat(data["occurred_at"]),
            data=data["data"],
        )


# ---------------------------------------------------------------------------
# Product Event Payloads
# ---------------------------------------------------------------------------


@dataclass
class ProductEventData:
    """Base product event data."""

    product_id: int
    sku: str
    name: str
    slug: str
    status: str
    is_featured: bool
    brand_id: Optional[int] = None
    category_id: Optional[int] = None
    subcategory_id: Optional[int] = None
    created_by: Optional[int] = None
    updated_by: Optional[int] = None


@dataclass
class ProductCreatedData(ProductEventData):
    """Data for ProductCreated event."""

    pass


@dataclass
class ProductUpdatedData(ProductEventData):
    """Data for ProductUpdated event."""

    pass


@dataclass
class ProductDeletedData:
    """Data for ProductDeleted event."""

    product_id: int
    sku: str
    deleted_by: Optional[int] = None


@dataclass
class ProductPublishedData:
    """Data for ProductPublished event."""

    product_id: int
    sku: str
    name: str
    published_by: Optional[int] = None


@dataclass
class ProductArchivedData:
    """Data for ProductArchived event."""

    product_id: int
    sku: str
    archived_by: Optional[int] = None


# ---------------------------------------------------------------------------
# Category Event Payloads
# ---------------------------------------------------------------------------


@dataclass
class CategoryEventData:
    """Base category event data."""

    category_id: int
    name: str
    slug: str
    is_active: bool
    display_order: int


@dataclass
class CategoryCreatedData(CategoryEventData):
    """Data for CategoryCreated event."""

    pass


@dataclass
class CategoryUpdatedData(CategoryEventData):
    """Data for CategoryUpdated event."""

    pass


@dataclass
class CategoryDeletedData:
    """Data for CategoryDeleted event."""

    category_id: int
    name: str


# ---------------------------------------------------------------------------
# Subcategory Event Payloads
# ---------------------------------------------------------------------------


@dataclass
class SubcategoryEventData:
    """Base subcategory event data."""

    subcategory_id: int
    category_id: int
    name: str
    slug: str
    is_active: bool
    display_order: int


@dataclass
class SubcategoryCreatedData(SubcategoryEventData):
    """Data for SubcategoryCreated event."""

    pass


@dataclass
class SubcategoryUpdatedData(SubcategoryEventData):
    """Data for SubcategoryUpdated event."""

    pass


@dataclass
class SubcategoryDeletedData:
    """Data for SubcategoryDeleted event."""

    subcategory_id: int
    category_id: int
    name: str


# ---------------------------------------------------------------------------
# Brand Event Payloads
# ---------------------------------------------------------------------------


@dataclass
class BrandEventData:
    """Base brand event data."""

    brand_id: int
    name: str
    slug: str
    is_active: bool
    website: Optional[str] = None


@dataclass
class BrandCreatedData(BrandEventData):
    """Data for BrandCreated event."""

    pass


@dataclass
class BrandUpdatedData(BrandEventData):
    """Data for BrandUpdated event."""

    pass


@dataclass
class BrandDeletedData:
    """Data for BrandDeleted event."""

    brand_id: int
    name: str


# ---------------------------------------------------------------------------
# Product Image Event Payloads
# ---------------------------------------------------------------------------


@dataclass
class ProductImageData:
    """Base product image event data."""

    image_id: int
    product_id: int
    image_url: str
    alt_text: str
    is_primary: bool
    display_order: int


@dataclass
class ProductImageAddedData(ProductImageData):
    """Data for ProductImageAdded event."""

    pass


@dataclass
class ProductImageRemovedData:
    """Data for ProductImageRemoved event."""

    image_id: int
    product_id: int


# ---------------------------------------------------------------------------
# Product Variant Event Payloads
# ---------------------------------------------------------------------------


@dataclass
class ProductVariantEventData:
    """Base product variant event data."""

    variant_id: int
    product_id: int
    sku: str
    status: str
    barcode: Optional[str] = None
    name: Optional[str] = None


@dataclass
class ProductVariantCreatedData(ProductVariantEventData):
    """Data for ProductVariantCreated event."""

    pass


@dataclass
class ProductVariantUpdatedData(ProductVariantEventData):
    """Data for ProductVariantUpdated event."""

    pass


# ---------------------------------------------------------------------------
# Price Event Payloads
# ---------------------------------------------------------------------------


@dataclass
class PriceEventData:
    """Base price event data."""

    price_id: int
    variant_id: int
    regular_price: str
    currency: str
    is_active: bool
    sale_price: Optional[str] = None
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None


@dataclass
class PriceCreatedData(PriceEventData):
    """Data for PriceCreated event."""

    pass


@dataclass
class PriceUpdatedData(PriceEventData):
    """Data for PriceUpdated event."""

    pass


# ---------------------------------------------------------------------------
# Inventory Event Payloads
# ---------------------------------------------------------------------------


@dataclass
class InventoryEventData:
    """Inventory update event data."""

    inventory_id: int
    variant_id: int
    stock_quantity: int
    reserved_quantity: int
    available_quantity: int
    low_stock_threshold: int
    is_in_stock: bool
    is_low_stock: bool
    action: str  # "stock_in", "stock_out", "adjustment", "reservation", "release"


# ---------------------------------------------------------------------------
# Product Review Event Payloads
# ---------------------------------------------------------------------------


@dataclass
class ProductReviewEventData:
    """Base product review event data."""

    review_id: int
    product_id: int
    user_id: int
    rating: int
    status: str
    title: Optional[str] = None


@dataclass
class ProductReviewCreatedData(ProductReviewEventData):
    """Data for ProductReviewCreated event."""

    pass


@dataclass
class ProductReviewUpdatedData(ProductReviewEventData):
    """Data for ProductReviewUpdated event."""

    pass


# ---------------------------------------------------------------------------
# Event Validation
# ---------------------------------------------------------------------------


def validate_event(event_data: dict[str, Any]) -> list[str]:
    """Validate event data structure and return any errors.

    Args:
        event_data: The event data dictionary to validate

    Returns:
        List of validation errors (empty if valid)
    """
    errors = []

    # Check required fields
    required_fields = ["event_id", "event_type", "aggregate_type", "aggregate_id", "version", "occurred_at", "data"]
    for field_name in required_fields:
        if field_name not in event_data:
            errors.append(f"Missing required field: {field_name}")

    if errors:
        return errors

    # Validate event_type
    try:
        EventType(event_data["event_type"])
    except ValueError:
        errors.append(f"Invalid event_type: {event_data['event_type']}")

    # Validate aggregate_type
    try:
        AggregateType(event_data["aggregate_type"])
    except ValueError:
        errors.append(f"Invalid aggregate_type: {event_data['aggregate_type']}")

    # Validate version is a positive integer
    if not isinstance(event_data["version"], int) or event_data["version"] < 1:
        errors.append("version must be a positive integer")

    # Validate occurred_at is a valid datetime
    try:
        datetime.fromisoformat(event_data["occurred_at"])
    except (ValueError, TypeError):
        errors.append("occurred_at must be a valid ISO datetime string")

    # Validate data is a dictionary
    if not isinstance(event_data["data"], dict):
        errors.append("data must be a dictionary")

    return errors


def get_event_data_class(event_type: EventType):
    """Get the data class for a specific event type."""
    mapping = {
        EventType.PRODUCT_CREATED: ProductCreatedData,
        EventType.PRODUCT_UPDATED: ProductUpdatedData,
        EventType.PRODUCT_DELETED: ProductDeletedData,
        EventType.PRODUCT_PUBLISHED: ProductPublishedData,
        EventType.PRODUCT_ARCHIVED: ProductArchivedData,
        EventType.CATEGORY_CREATED: CategoryCreatedData,
        EventType.CATEGORY_UPDATED: CategoryUpdatedData,
        EventType.CATEGORY_DELETED: CategoryDeletedData,
        EventType.SUBCATEGORY_CREATED: SubcategoryCreatedData,
        EventType.SUBCATEGORY_UPDATED: SubcategoryUpdatedData,
        EventType.SUBCATEGORY_DELETED: SubcategoryDeletedData,
        EventType.BRAND_CREATED: BrandCreatedData,
        EventType.BRAND_UPDATED: BrandUpdatedData,
        EventType.BRAND_DELETED: BrandDeletedData,
        EventType.PRODUCT_IMAGE_ADDED: ProductImageAddedData,
        EventType.PRODUCT_IMAGE_REMOVED: ProductImageRemovedData,
        EventType.PRODUCT_VARIANT_CREATED: ProductVariantCreatedData,
        EventType.PRODUCT_VARIANT_UPDATED: ProductVariantUpdatedData,
        EventType.PRICE_CREATED: PriceCreatedData,
        EventType.PRICE_UPDATED: PriceUpdatedData,
        EventType.INVENTORY_UPDATED: InventoryEventData,
        EventType.PRODUCT_REVIEW_CREATED: ProductReviewCreatedData,
        EventType.PRODUCT_REVIEW_UPDATED: ProductReviewUpdatedData,
    }
    return mapping.get(event_type)
