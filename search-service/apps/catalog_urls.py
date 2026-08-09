"""URL configuration for the catalog API."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.catalog_views import (
    AttributeViewSet,
    BrandViewSet,
    CatalogSearchView,
    CategoryViewSet,
    InventoryLogsViewSet,
    LowStockAlertViewSet,
    ProductAttributeViewSet,
    ProductImageViewSet,
    ProductReviewViewSet,
    ProductSpecificationViewSet,
    ProductVariantViewSet,
    ProductViewSet,
    SubcategoryViewSet,
    VariantInventoryViewSet,
    VariantPriceViewSet,
)

# ---------------------------------------------------------------------------
# Main router
# ---------------------------------------------------------------------------

router = DefaultRouter()
router.register(r"categories", CategoryViewSet, basename="category")
router.register(r"subcategories", SubcategoryViewSet, basename="subcategory")
router.register(r"brands", BrandViewSet, basename="brand")
router.register(r"products", ProductViewSet, basename="product")
router.register(r"attributes", AttributeViewSet, basename="attribute")

# Inventory routes
router.register(r"inventory/logs", InventoryLogsViewSet, basename="inventory-log")
router.register(r"inventory/low-stock", LowStockAlertViewSet, basename="low-stock-alert")

# Search
router.register(r"search", CatalogSearchView, basename="catalog-search")

# ---------------------------------------------------------------------------
# Nested routers for product sub-resources
# ---------------------------------------------------------------------------

product_images = ProductImageViewSet.as_view({
    "get": "list",
    "post": "create",
})
product_image_detail = ProductImageViewSet.as_view({
    "delete": "destroy",
})

product_variants = ProductVariantViewSet.as_view({
    "get": "list",
    "post": "create",
})
product_variant_detail = ProductVariantViewSet.as_view({
    "get": "retrieve",
    "put": "update",
    "patch": "partial_update",
})

product_attributes = ProductAttributeViewSet.as_view({
    "get": "list",
    "post": "create",
})
product_attribute_detail = ProductAttributeViewSet.as_view({
    "get": "retrieve",
    "delete": "destroy",
})

product_specifications = ProductSpecificationViewSet.as_view({
    "get": "list",
    "post": "create",
})
product_specification_detail = ProductSpecificationViewSet.as_view({
    "get": "retrieve",
    "put": "update",
    "patch": "partial_update",
    "delete": "destroy",
})

product_prices = VariantPriceViewSet.as_view({
    "get": "list",
    "post": "create",
})
price_detail = VariantPriceViewSet.as_view({
    "get": "retrieve",
    "put": "update",
    "patch": "partial_update",
})

product_reviews = ProductReviewViewSet.as_view({
    "get": "list",
    "post": "create",
})

variant_inventory = VariantInventoryViewSet.as_view({
    "get": "retrieve",
    "patch": "adjust",
})

# Variant standalone routes
variant_detail = ProductVariantViewSet.as_view({
    "get": "retrieve",
    "put": "update",
    "patch": "partial_update",
})

variant_inventory_standalone = VariantInventoryViewSet.as_view({
    "get": "retrieve",
    "patch": "adjust",
})

# Review standalone routes
review_detail = ProductReviewViewSet.as_view({
    "get": "retrieve",
    "patch": "partial_update",
    "delete": "destroy",
})

# ---------------------------------------------------------------------------
# URL patterns
# ---------------------------------------------------------------------------

urlpatterns = [
    # Router URLs (categories, subcategories, brands, products, attributes, inventory)
    path("", include(router.urls)),

    # Product nested resources
    path(
        "products/<int:product_pk>/images/",
        product_images,
        name="product-images",
    ),
    path(
        "products/<int:product_pk>/images/<int:pk>/",
        product_image_detail,
        name="product-image-detail",
    ),
    path(
        "products/<int:product_pk>/variants/",
        product_variants,
        name="product-variants",
    ),
    path(
        "products/<int:product_pk>/variants/<int:pk>/",
        product_variant_detail,
        name="product-variant-detail",
    ),
    path(
        "products/<int:product_pk>/attributes/",
        product_attributes,
        name="product-attributes",
    ),
    path(
        "products/<int:product_pk>/attributes/<int:pk>/",
        product_attribute_detail,
        name="product-attribute-detail",
    ),
    path(
        "products/<int:product_pk>/specifications/",
        product_specifications,
        name="product-specifications",
    ),
    path(
        "products/<int:product_pk>/specifications/<int:pk>/",
        product_specification_detail,
        name="product-specification-detail",
    ),
    path(
        "products/<int:product_pk>/prices/",
        product_prices,
        name="product-prices",
    ),
    path(
        "prices/<int:pk>/",
        price_detail,
        name="price-detail",
    ),
    path(
        "products/<int:product_pk>/reviews/",
        product_reviews,
        name="product-reviews",
    ),

    # Variant standalone routes
    path(
        "variants/<int:pk>/",
        variant_detail,
        name="variant-detail",
    ),
    path(
        "variants/<int:variant_pk>/inventory/",
        variant_inventory,
        name="variant-inventory",
    ),

    # Inventory standalone routes
    path(
        "variants/<int:variant_pk>/inventory/adjust/",
        variant_inventory_standalone,
        name="variant-inventory-adjust",
    ),

    # Review standalone routes
    path(
        "reviews/<int:pk>/",
        review_detail,
        name="review-detail",
    ),
    path(
        "reviews/<int:pk>/approve/",
        ProductReviewViewSet.as_view({"post": "approve"}),
        name="review-approve",
    ),
    path(
        "reviews/<int:pk>/reject/",
        ProductReviewViewSet.as_view({"post": "reject"}),
        name="review-reject",
    ),
]
