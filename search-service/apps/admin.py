from django.contrib import admin
from django.utils import timezone

from apps.models import (
    Attribute,
    AttributeValue,
    Brand,
    Category,
    Inventory,
    InventoryLog,
    LowStockAlert,
    OutboxEvent,
    Product,
    ProductAttributeValue,
    ProductImage,
    ProductReview,
    ProductSpecification,
    ProductVariant,
    RecentlyViewed,
    Subcategory,
    VariantAttributeValue,
    VariantPrice,
)


# ---------------------------------------------------------------------------
# Category / Subcategory
# ---------------------------------------------------------------------------


class SubcategoryInline(admin.TabularInline):
    model = Subcategory
    extra = 0
    fields = ("name", "slug", "is_active", "display_order")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "is_active", "display_order", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [SubcategoryInline]
    ordering = ("display_order", "name")


@admin.register(Subcategory)
class SubcategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "category", "is_active", "display_order", "created_at")
    list_filter = ("category", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("category", "display_order", "name")


# ---------------------------------------------------------------------------
# Brand
# ---------------------------------------------------------------------------


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 0
    fields = ("image_url", "alt_text", "is_primary", "display_order")


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0
    fields = ("sku", "name", "barcode", "status")


class ProductSpecificationInline(admin.TabularInline):
    model = ProductSpecification
    extra = 0
    fields = ("name", "value", "unit", "display_order")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "sku", "name", "brand", "category", "status", "is_featured", "created_at")
    list_filter = ("status", "is_featured", "brand", "category")
    search_fields = ("sku", "name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    raw_id_fields = ("brand", "category", "subcategory")
    inlines = [ProductImageInline, ProductVariantInline, ProductSpecificationInline]
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "is_primary", "display_order", "alt_text")
    list_filter = ("is_primary",)
    raw_id_fields = ("product",)


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ("id", "sku", "product", "name", "barcode", "status", "created_at")
    list_filter = ("status", "product")
    search_fields = ("sku", "barcode", "name")
    raw_id_fields = ("product",)


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------


class AttributeValueInline(admin.TabularInline):
    model = AttributeValue
    extra = 0
    fields = ("value",)


@admin.register(Attribute)
class AttributeAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "created_at")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [AttributeValueInline]


@admin.register(AttributeValue)
class AttributeValueAdmin(admin.ModelAdmin):
    list_display = ("id", "attribute", "value", "created_at")
    list_filter = ("attribute",)
    search_fields = ("value",)
    raw_id_fields = ("attribute",)


@admin.register(ProductAttributeValue)
class ProductAttributeValueAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "attribute_value")
    raw_id_fields = ("product", "attribute_value")


@admin.register(VariantAttributeValue)
class VariantAttributeValueAdmin(admin.ModelAdmin):
    list_display = ("id", "variant", "attribute_value")
    raw_id_fields = ("variant", "attribute_value")


# ---------------------------------------------------------------------------
# Specifications
# ---------------------------------------------------------------------------


@admin.register(ProductSpecification)
class ProductSpecificationAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "name", "value", "unit", "display_order")
    list_filter = ("product",)
    search_fields = ("name", "value")
    raw_id_fields = ("product",)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


@admin.register(VariantPrice)
class VariantPriceAdmin(admin.ModelAdmin):
    list_display = ("id", "variant", "regular_price", "sale_price", "currency", "is_active", "effective_from", "effective_to")
    list_filter = ("is_active", "currency")
    raw_id_fields = ("variant",)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ("id", "variant", "stock_quantity", "reserved_quantity", "low_stock_threshold", "updated_at")
    raw_id_fields = ("variant",)


@admin.register(InventoryLog)
class InventoryLogAdmin(admin.ModelAdmin):
    list_display = ("id", "variant", "action", "previous_quantity", "new_quantity", "quantity_change", "actor_user_id", "created_at")
    list_filter = ("action",)
    raw_id_fields = ("variant",)
    ordering = ("-created_at",)


@admin.register(LowStockAlert)
class LowStockAlertAdmin(admin.ModelAdmin):
    list_display = ("id", "variant", "threshold", "quantity_at_alert", "status", "created_at", "resolved_at")
    list_filter = ("status",)
    raw_id_fields = ("variant",)
    ordering = ("-created_at",)


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


@admin.register(ProductReview)
class ProductReviewAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "user_id", "rating", "title", "status", "created_at")
    list_filter = ("status", "rating")
    search_fields = ("title", "comment")
    raw_id_fields = ("product",)
    ordering = ("-created_at",)


# ---------------------------------------------------------------------------
# Legacy models
# ---------------------------------------------------------------------------


@admin.register(RecentlyViewed)
class RecentlyViewedAdmin(admin.ModelAdmin):
    list_display = ("id", "user_id", "product_id", "product_name", "viewed_at")
    search_fields = ("product_name",)
    ordering = ("-viewed_at",)


# ---------------------------------------------------------------------------
# Outbox Events
# ---------------------------------------------------------------------------


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "status",
        "retry_count",
        "created_at",
        "published_at",
    )
    list_filter = ("status", "event_type", "aggregate_type")
    search_fields = ("event_type", "aggregate_id", "last_error")
    readonly_fields = ("payload", "created_at", "published_at")
    ordering = ("-created_at",)
    actions = ["reset_events", "retry_failed"]

    def reset_events(self, request, queryset):
        """Reset selected events to pending status."""
        count = queryset.update(
            status="pending",
            retry_count=0,
            last_error="",
            available_at=django.utils.timezone.now(),
        )
        self.message_user(request, f"Reset {count} events")

    def retry_failed(self, request, queryset):
        """Retry failed events."""
        count = queryset.filter(status="failed").update(
            status="pending",
            retry_count=0,
            last_error="",
            available_at=django.utils.timezone.now(),
        )
        self.message_user(request, f"Reset {count} failed events for retry")
