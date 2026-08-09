"""Catalog REST API views.

All views use PostgreSQL as the source of truth.
Elasticsearch integration will be added in a later phase.
"""

from django.db import models, transaction
from django.db.models import Avg, Q
from django.utils import timezone
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.authentication import SharedTokenAuthentication
from apps.catalog_serializers import (
    AttributeSerializer,
    AttributeListSerializer,
    AttributeValueSerializer,
    BrandSerializer,
    BrandListSerializer,
    CategorySerializer,
    CategoryListSerializer,
    InventoryAdjustSerializer,
    InventoryLogSerializer,
    InventorySerializer,
    LowStockAlertSerializer,
    ProductAttributeValueCreateSerializer,
    ProductAttributeValueSerializer,
    ProductCreateUpdateSerializer,
    ProductDetailSerializer,
    ProductImageSerializer,
    ProductListSerializer,
    ProductReviewCreateSerializer,
    ProductReviewSerializer,
    ProductReviewUpdateSerializer,
    ProductSpecificationSerializer,
    ProductVariantCreateSerializer,
    ProductVariantListSerializer,
    ProductVariantSerializer,
    SubcategorySerializer,
    SubcategoryListSerializer,
    VariantAttributeValueCreateSerializer,
    VariantAttributeValueSerializer,
    VariantPriceSerializer,
)
from apps.event_publisher import (
    EventType,
    publish_category_event,
    publish_image_event,
    publish_inventory_event,
    publish_price_event,
    publish_product_event,
    publish_review_event,
    publish_subcategory_event,
    publish_brand_event,
    publish_variant_event,
)
from apps.models import (
    Attribute,
    AttributeValue,
    Brand,
    Category,
    Inventory,
    InventoryLog,
    LowStockAlert,
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
from apps.permissions import (
    IsAdmin,
    IsAdminOrCatalogManager,
    IsAdminOrInventoryManager,
    IsAuthenticated,
    IsCatalogManager,
    IsInventoryManager,
    IsOwnerOrReadOnly,
    IsReviewOwner,
    IsReadOnlyOrAdmin,
    CanManageProducts,
    CanManageCategories,
    CanManageBrands,
    CanManageAttributes,
    CanManagePricing,
    CanManageInventory,
    CanCreateReview,
    CanModerateReviews,
)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def success_response(data=None, message="Success", status_code=status.HTTP_200_OK):
    return Response(
        {"message": message, "status": status_code, "data": data},
        status=status_code,
    )


def error_response(message="Error", status_code=status.HTTP_400_BAD_REQUEST, data=None):
    return Response(
        {"message": message, "status": status_code, "data": data},
        status=status_code,
    )


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class CategoryViewSet(viewsets.ModelViewSet):
    """CRUD for categories."""

    queryset = Category.objects.filter(is_deleted=False)
    authentication_classes = [SharedTokenAuthentication]
    pagination_class = StandardPagination

    def get_serializer_class(self):
        if self.action == "list":
            return CategoryListSerializer
        return CategorySerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAdminOrCatalogManager()]

    def get_queryset(self):
        qs = Category.objects.filter(is_deleted=False)
        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() in ("true", "1", "yes"))
        return qs.order_by("display_order", "name")

    def perform_create(self, serializer):
        with transaction.atomic():
            instance = serializer.save()
            publish_category_event(EventType.CATEGORY_CREATED, instance)

    def perform_update(self, serializer):
        with transaction.atomic():
            instance = serializer.save()
            publish_category_event(EventType.CATEGORY_UPDATED, instance)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        with transaction.atomic():
            instance.soft_delete()
            publish_category_event(
                EventType.CATEGORY_DELETED,
                instance,
                name=instance.name,
            )
        return success_response(message="Category deleted successfully")


# ---------------------------------------------------------------------------
# Subcategories
# ---------------------------------------------------------------------------


class SubcategoryViewSet(viewsets.ModelViewSet):
    """CRUD for subcategories."""

    queryset = Subcategory.objects.filter(is_deleted=False)
    authentication_classes = [SharedTokenAuthentication]
    pagination_class = StandardPagination

    def get_serializer_class(self):
        if self.action == "list":
            return SubcategoryListSerializer
        return SubcategorySerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAdminOrCatalogManager()]

    def get_queryset(self):
        qs = Subcategory.objects.filter(is_deleted=False)
        category_id = self.request.query_params.get("category_id")
        if category_id:
            qs = qs.filter(category_id=category_id)
        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() in ("true", "1", "yes"))
        return qs.order_by("display_order", "name")

    def perform_create(self, serializer):
        with transaction.atomic():
            instance = serializer.save()
            publish_subcategory_event(EventType.SUBCATEGORY_CREATED, instance)

    def perform_update(self, serializer):
        with transaction.atomic():
            instance = serializer.save()
            publish_subcategory_event(EventType.SUBCATEGORY_UPDATED, instance)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        with transaction.atomic():
            instance.soft_delete()
            publish_subcategory_event(
                EventType.SUBCATEGORY_DELETED,
                instance,
                name=instance.name,
            )
        return success_response(message="Subcategory deleted successfully")


# ---------------------------------------------------------------------------
# Brands
# ---------------------------------------------------------------------------


class BrandViewSet(viewsets.ModelViewSet):
    """CRUD for brands."""

    queryset = Brand.objects.filter(is_deleted=False)
    authentication_classes = [SharedTokenAuthentication]
    pagination_class = StandardPagination

    def get_serializer_class(self):
        if self.action == "list":
            return BrandListSerializer
        return BrandSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAdminOrCatalogManager()]

    def get_queryset(self):
        qs = Brand.objects.filter(is_deleted=False)
        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() in ("true", "1", "yes"))
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(slug__icontains=search)
            )
        return qs.order_by("name")

    def perform_create(self, serializer):
        with transaction.atomic():
            instance = serializer.save()
            publish_brand_event(EventType.BRAND_CREATED, instance)

    def perform_update(self, serializer):
        with transaction.atomic():
            instance = serializer.save()
            publish_brand_event(EventType.BRAND_UPDATED, instance)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        with transaction.atomic():
            instance.soft_delete()
            publish_brand_event(
                EventType.BRAND_DELETED,
                instance,
                name=instance.name,
            )
        return success_response(message="Brand deleted successfully")


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


class ProductViewSet(viewsets.ModelViewSet):
    """CRUD for products."""

    queryset = Product.objects.filter(is_deleted=False)
    authentication_classes = [SharedTokenAuthentication]
    pagination_class = StandardPagination

    def get_serializer_class(self):
        if self.action == "list":
            return ProductListSerializer
        if self.action in ("create", "update", "partial_update"):
            return ProductCreateUpdateSerializer
        return ProductDetailSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAdminOrCatalogManager()]

    def get_queryset(self):
        qs = Product.objects.filter(is_deleted=False).select_related(
            "brand", "category", "subcategory"
        ).prefetch_related("images", "variants__price", "variants__inventory")

        # Filters
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        brand_id = self.request.query_params.get("brand_id")
        if brand_id:
            qs = qs.filter(brand_id=brand_id)

        category_id = self.request.query_params.get("category_id")
        if category_id:
            qs = qs.filter(category_id=category_id)

        subcategory_id = self.request.query_params.get("subcategory_id")
        if subcategory_id:
            qs = qs.filter(subcategory_id=subcategory_id)

        is_featured = self.request.query_params.get("is_featured")
        if is_featured is not None:
            qs = qs.filter(is_featured=is_featured.lower() in ("true", "1", "yes"))

        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(sku__icontains=search) |
                Q(short_description__icontains=search)
            )

        # Ordering
        ordering = self.request.query_params.get("ordering", "-created_at")
        valid_orderings = [
            "created_at", "-created_at", "name", "-name",
            "sku", "-sku", "updated_at", "-updated_at",
        ]
        if ordering in valid_orderings:
            qs = qs.order_by(ordering)

        return qs

    def perform_create(self, serializer):
        with transaction.atomic():
            user = getattr(self.request, "user", None)
            user_id = getattr(user, "pk", None) if user else None
            instance = serializer.save(created_by=user_id, updated_by=user_id)
            publish_product_event(EventType.PRODUCT_CREATED, instance)

    def perform_update(self, serializer):
        with transaction.atomic():
            user = getattr(self.request, "user", None)
            user_id = getattr(user, "pk", None) if user else None
            instance = serializer.save(updated_by=user_id)
            publish_product_event(EventType.PRODUCT_UPDATED, instance)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        with transaction.atomic():
            instance.soft_delete()
            publish_product_event(
                EventType.PRODUCT_DELETED,
                instance,
                deleted_by=getattr(request.user, "pk", None) if hasattr(request, "user") else None,
            )
        return success_response(message="Product archived successfully")


# ---------------------------------------------------------------------------
# Product Images
# ---------------------------------------------------------------------------


class ProductImageViewSet(viewsets.ModelViewSet):
    """CRUD for product images."""

    serializer_class = ProductImageSerializer
    authentication_classes = [SharedTokenAuthentication]
    permission_classes = [IsAdminOrCatalogManager]

    def get_queryset(self):
        product_id = self.kwargs.get("product_pk")
        return ProductImage.objects.filter(product_id=product_id)

    def perform_create(self, serializer):
        with transaction.atomic():
            product_id = self.kwargs.get("product_pk")
            instance = serializer.save(product_id=product_id)
            publish_image_event(EventType.PRODUCT_IMAGE_ADDED, instance)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        with transaction.atomic():
            publish_image_event(EventType.PRODUCT_IMAGE_REMOVED, instance)
            instance.delete()
        return success_response(message="Image deleted successfully")


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


class ProductVariantViewSet(viewsets.ModelViewSet):
    """CRUD for product variants."""

    authentication_classes = [SharedTokenAuthentication]

    def get_serializer_class(self):
        if self.action == "list":
            return ProductVariantListSerializer
        if self.action == "create":
            return ProductVariantCreateSerializer
        return ProductVariantSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAdminOrCatalogManager()]

    def get_queryset(self):
        product_id = self.kwargs.get("product_pk")
        if product_id:
            return ProductVariant.objects.filter(
                product_id=product_id, is_deleted=False
            ).select_related("price", "inventory")
        return ProductVariant.objects.filter(
            is_deleted=False
        ).select_related("price", "inventory")

    def perform_create(self, serializer):
        with transaction.atomic():
            product_id = self.kwargs.get("product_pk")
            instance = serializer.save(product_id=product_id)
            publish_variant_event(EventType.PRODUCT_VARIANT_CREATED, instance)

    def perform_update(self, serializer):
        with transaction.atomic():
            instance = serializer.save()
            publish_variant_event(EventType.PRODUCT_VARIANT_UPDATED, instance)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        with transaction.atomic():
            instance.soft_delete()
            publish_variant_event(EventType.PRODUCT_VARIANT_UPDATED, instance)
        return success_response(message="Variant archived successfully")

    @action(detail=False, methods=["get"], url_path="(?P<variant_pk>[^/.]+)")
    def retrieve_variant(self, request, product_pk=None, variant_pk=None):
        """GET /api/catalog/products/{product_id}/variants/{variant_id}/"""
        try:
            variant = ProductVariant.objects.get(
                pk=variant_pk, product_id=product_pk, is_deleted=False
            )
        except ProductVariant.DoesNotExist:
            return error_response("Variant not found", status.HTTP_404_NOT_FOUND)

        serializer = ProductVariantSerializer(variant)
        return success_response(data=serializer.data)


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------


class AttributeViewSet(viewsets.ModelViewSet):
    """CRUD for attributes."""

    queryset = Attribute.objects.all()
    authentication_classes = [SharedTokenAuthentication]
    pagination_class = StandardPagination

    def get_serializer_class(self):
        if self.action == "list":
            return AttributeListSerializer
        return AttributeSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAdminOrCatalogManager()]


class ProductAttributeViewSet(viewsets.ModelViewSet):
    """Assign attributes to products."""

    serializer_class = ProductAttributeValueSerializer
    authentication_classes = [SharedTokenAuthentication]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAdminOrCatalogManager()]

    def get_queryset(self):
        product_id = self.kwargs.get("product_pk")
        return ProductAttributeValue.objects.filter(
            product_id=product_id
        ).select_related("attribute_value__attribute")

    def get_serializer_class(self):
        if self.action == "create":
            return ProductAttributeValueCreateSerializer
        return ProductAttributeValueSerializer

    def perform_create(self, serializer):
        product_id = self.kwargs.get("product_pk")
        attr_value = serializer.validated_data["attribute_value"]
        ProductAttributeValue.objects.get_or_create(
            product_id=product_id, attribute_value=attr_value
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.delete()
        return success_response(message="Attribute removed from product")


# ---------------------------------------------------------------------------
# Specifications
# ---------------------------------------------------------------------------


class ProductSpecificationViewSet(viewsets.ModelViewSet):
    """CRUD for product specifications."""

    serializer_class = ProductSpecificationSerializer
    authentication_classes = [SharedTokenAuthentication]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAdminOrCatalogManager()]

    def get_queryset(self):
        product_id = self.kwargs.get("product_pk")
        return ProductSpecification.objects.filter(product_id=product_id)

    def perform_create(self, serializer):
        product_id = self.kwargs.get("product_pk")
        serializer.save(product_id=product_id)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


class VariantPriceViewSet(viewsets.ModelViewSet):
    """CRUD for variant pricing."""

    serializer_class = VariantPriceSerializer
    authentication_classes = [SharedTokenAuthentication]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAdminOrCatalogManager()]

    def get_queryset(self):
        product_id = self.kwargs.get("product_pk")
        if product_id:
            return VariantPrice.objects.filter(
                variant__product_id=product_id
            ).select_related("variant")
        return VariantPrice.objects.all().select_related("variant")

    def perform_create(self, serializer):
        with transaction.atomic():
            variant_id = self.kwargs.get("variant_pk")
            instance = serializer.save(variant_id=variant_id)
            publish_price_event(EventType.PRICE_CREATED, instance)

    def perform_update(self, serializer):
        with transaction.atomic():
            instance = serializer.save()
            publish_price_event(EventType.PRICE_UPDATED, instance)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


class VariantInventoryViewSet(viewsets.GenericViewSet):
    """Inventory management for variants."""

    serializer_class = InventorySerializer
    authentication_classes = [SharedTokenAuthentication]

    def get_permissions(self):
        if self.action == "retrieve":
            return [AllowAny()]
        return [IsAdminOrInventoryManager()]

    def retrieve(self, request, product_pk=None, variant_pk=None):
        """GET /api/catalog/variants/{id}/inventory/"""
        try:
            variant = ProductVariant.objects.get(pk=variant_pk, is_deleted=False)
        except ProductVariant.DoesNotExist:
            return error_response("Variant not found", status.HTTP_404_NOT_FOUND)

        inventory, _ = Inventory.objects.get_or_create(variant=variant)
        serializer = InventorySerializer(inventory)
        return success_response(data=serializer.data)

    @action(detail=False, methods=["patch"])
    def adjust(self, request, product_pk=None, variant_pk=None):
        """PATCH /api/catalog/variants/{id}/inventory/"""
        try:
            variant = ProductVariant.objects.get(pk=variant_pk, is_deleted=False)
        except ProductVariant.DoesNotExist:
            return error_response("Variant not found", status.HTTP_404_NOT_FOUND)

        serializer = InventoryAdjustSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            inventory, _ = Inventory.objects.get_or_create(variant=variant)
            data = serializer.validated_data
            quantity = data["quantity"]
            action = data["action"]
            reason = data.get("reason", "")

            previous = inventory.stock_quantity

            if action == "stock_in":
                inventory.stock_quantity += quantity
            elif action == "stock_out":
                inventory.stock_quantity = max(0, inventory.stock_quantity - quantity)
            elif action == "adjustment":
                inventory.stock_quantity = max(0, inventory.stock_quantity + quantity)
            elif action == "reservation":
                inventory.reserved_quantity += min(quantity, inventory.available_quantity)
            elif action == "release":
                inventory.reserved_quantity = max(0, inventory.reserved_quantity - quantity)
            elif action == "return":
                inventory.stock_quantity += quantity

            inventory.save()

            user = getattr(request, "user", None)
            actor_id = getattr(user, "pk", None) if user else None

            InventoryLog.objects.create(
                variant=variant,
                previous_quantity=previous,
                new_quantity=inventory.stock_quantity,
                quantity_change=inventory.stock_quantity - previous,
                action=action,
                reason=reason,
                actor_user_id=actor_id,
            )

            if inventory.is_low_stock:
                LowStockAlert.objects.get_or_create(
                    variant=variant,
                    status="open",
                    defaults={
                        "threshold": inventory.low_stock_threshold,
                        "quantity_at_alert": inventory.stock_quantity,
                    },
                )

            publish_inventory_event(inventory, action, actor_user_id=actor_id)

        return success_response(
            data=InventorySerializer(inventory).data,
            message="Inventory adjusted successfully",
        )


class InventoryLogsViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only view for inventory logs."""

    serializer_class = InventoryLogSerializer
    authentication_classes = [SharedTokenAuthentication]
    permission_classes = [IsAdminOrInventoryManager]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = InventoryLog.objects.all().select_related("variant")

        variant_id = self.request.query_params.get("variant_id")
        if variant_id:
            qs = qs.filter(variant_id=variant_id)

        action = self.request.query_params.get("action")
        if action:
            qs = qs.filter(action=action)

        return qs.order_by("-created_at")


class LowStockAlertViewSet(viewsets.ModelViewSet):
    """Low stock alerts management."""

    serializer_class = LowStockAlertSerializer
    authentication_classes = [SharedTokenAuthentication]
    permission_classes = [IsAdminOrInventoryManager]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = LowStockAlert.objects.all().select_related("variant")

        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        else:
            qs = qs.filter(status="open")

        return qs.order_by("-created_at")

    @action(detail=True, methods=["patch"])
    def resolve(self, request, pk=None):
        """PATCH /api/catalog/inventory/low-stock/{id}/resolve/"""
        alert = self.get_object()
        if alert.status == "resolved":
            return error_response("Alert already resolved")

        user = getattr(request, "user", None)
        actor_id = getattr(user, "pk", None) if user else None

        alert.status = "resolved"
        alert.resolved_at = timezone.now()
        alert.resolved_by = actor_id
        alert.save()

        return success_response(
            data=LowStockAlertSerializer(alert).data,
            message="Alert resolved successfully",
        )


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


class ProductReviewViewSet(viewsets.ModelViewSet):
    """CRUD for product reviews.

    Supports:
    - Authenticated users can create reviews (one per product per user)
    - Users can update/delete their own pending reviews
    - Admins/catalog managers can approve/reject reviews
    - Sorting by newest or rating
    - Pagination
    """

    authentication_classes = [SharedTokenAuthentication]
    pagination_class = StandardPagination

    def get_serializer_class(self):
        if self.action == "create":
            return ProductReviewCreateSerializer
        if self.action in ("update", "partial_update"):
            return ProductReviewUpdateSerializer
        return ProductReviewSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        if self.action == "create":
            return [IsAuthenticated()]
        # approve, reject actions require admin/catalog_manager role
        if self.action in ("approve", "reject"):
            return [IsAdminOrCatalogManager()]
        return [IsReviewOwner()]

    def get_queryset(self):
        product_id = self.kwargs.get("product_pk")
        qs = ProductReview.objects.all()

        if product_id:
            qs = qs.filter(product_id=product_id)

        # Sorting
        ordering = self.request.query_params.get("ordering", "-created_at")
        valid_orderings = [
            "created_at", "-created_at",
            "rating", "-rating",
            "updated_at", "-updated_at",
        ]
        if ordering in valid_orderings:
            qs = qs.order_by(ordering)

        return qs

    def perform_create(self, serializer):
        product_id = self.kwargs.get("product_pk")
        user = getattr(self.request, "user", None)
        user_id = getattr(user, "pk", None) if user else None

        # Duplicate review prevention: one review per product per user
        if user_id and ProductReview.objects.filter(
            product_id=product_id, user_id=user_id
        ).exists():
            from rest_framework.exceptions import ValidationError
            raise ValidationError(
                "You have already reviewed this product. "
                "You may update your existing review instead."
            )

        with transaction.atomic():
            instance = serializer.save(
                product_id=product_id,
                user_id=user_id,
                status=ProductReview.STATUS_PENDING,
            )
            publish_review_event(EventType.PRODUCT_REVIEW_CREATED, instance)

    def perform_update(self, serializer):
        instance = self.get_object()
        old_status = instance.status

        with transaction.atomic():
            updated_instance = serializer.save()

            # Determine which event to publish based on status change
            new_status = updated_instance.status
            if old_status != new_status:
                if new_status == ProductReview.STATUS_APPROVED:
                    publish_review_event(
                        EventType.PRODUCT_REVIEW_APPROVED, updated_instance
                    )
                elif new_status == ProductReview.STATUS_REJECTED:
                    publish_review_event(
                        EventType.PRODUCT_REVIEW_REJECTED, updated_instance
                    )
            else:
                publish_review_event(
                    EventType.PRODUCT_REVIEW_UPDATED, updated_instance
                )

    def perform_destroy(self, instance):
        with transaction.atomic():
            product_id = instance.product_id
            publish_review_event(EventType.PRODUCT_REVIEW_DELETED, instance)
            instance.delete()

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None, product_pk=None):
        """POST /api/catalog/products/{product_pk}/reviews/{pk}/approve/

        Approve a pending review. Only admins/catalog managers can approve.
        Customers cannot approve their own reviews.
        """
        review = self.get_object()

        if review.status == ProductReview.STATUS_APPROVED:
            return error_response("Review is already approved")

        # Prevent self-approval
        user = getattr(request, "user", None)
        user_id = getattr(user, "pk", None) if user else None
        if user_id and review.user_id == user_id:
            return error_response(
                "You cannot approve your own review",
                status.HTTP_403_FORBIDDEN,
            )

        with transaction.atomic():
            review.status = ProductReview.STATUS_APPROVED
            review.save(update_fields=["status", "updated_at"])
            publish_review_event(EventType.PRODUCT_REVIEW_APPROVED, review)

        return success_response(
            data=ProductReviewSerializer(review).data,
            message="Review approved successfully",
        )

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None, product_pk=None):
        """POST /api/catalog/products/{product_pk}/reviews/{pk}/reject/

        Reject a pending review. Only admins/catalog managers can reject.
        """
        review = self.get_object()

        if review.status == ProductReview.STATUS_REJECTED:
            return error_response("Review is already rejected")

        with transaction.atomic():
            review.status = ProductReview.STATUS_REJECTED
            review.save(update_fields=["status", "updated_at"])
            publish_review_event(EventType.PRODUCT_REVIEW_REJECTED, review)

        return success_response(
            data=ProductReviewSerializer(review).data,
            message="Review rejected successfully",
        )


# ---------------------------------------------------------------------------
# Catalog Search (public, read-only)
# ---------------------------------------------------------------------------


class CatalogSearchView(viewsets.ReadOnlyModelViewSet):
    """Public search endpoint for the catalog."""

    serializer_class = ProductListSerializer
    pagination_class = StandardPagination
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = Product.objects.filter(
            is_deleted=False, status="active"
        ).select_related("brand", "category").prefetch_related("images")

        search = self.request.query_params.get("q") or self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(sku__icontains=search) |
                Q(short_description__icontains=search) |
                Q(description__icontains=search)
            )

        brand = self.request.query_params.get("brand")
        if brand:
            qs = qs.filter(
                Q(brand__name__icontains=brand) | Q(brand__slug=brand)
            )

        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(
                Q(category__name__icontains=category) | Q(category__slug=category)
            )

        min_price = self.request.query_params.get("min_price")
        if min_price:
            try:
                qs = qs.filter(variants__price__regular_price__gte=float(min_price))
            except (ValueError, TypeError):
                pass

        max_price = self.request.query_params.get("max_price")
        if max_price:
            try:
                qs = qs.filter(variants__price__regular_price__lte=float(max_price))
            except (ValueError, TypeError):
                pass

        is_featured = self.request.query_params.get("is_featured")
        if is_featured is not None:
            qs = qs.filter(is_featured=is_featured.lower() in ("true", "1", "yes"))

        ordering = self.request.query_params.get("ordering", "-created_at")
        valid_orderings = [
            "created_at", "-created_at", "name", "-name",
            "price", "-price",
        ]
        if ordering in valid_orderings:
            qs = qs.order_by(ordering)

        return qs.distinct()
