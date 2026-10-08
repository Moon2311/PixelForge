import json
import os
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import F, Q
from rest_framework.views import APIView
from rest_framework import status

from apps.authentication.authentication import BearerTokenAuthentication
from apps.common.custom_response import CustomResponse
from apps.catalog.inventory import InventoryManager, InventoryError, InsufficientStockError, VariantNotFoundError
from apps.catalog.legacy_inventory_serializers import LegacyInventoryLogSerializer, LegacyLowStockAlertSerializer
from apps.catalog.models import Product, RecentlyViewed
from apps.authentication.permissions import IsAdmin
from apps.catalog import cache as catalog_cache, catalog_store, product_store
from apps.search import services as search_services
from apps.catalog.legacy_serializers import (
    CatalogSerializer,
    ProductSerializer,
    StockUpdateSerializer,
)

ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp"}
ADMIN_LIST_PARAMS = {
    "search",
    "category",
    "stock_status",
    "min_price",
    "max_price",
    "status",
    "sort",
    "order",
    "featured",
    "on_sale",
    "flash_sale",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _image_is_allowed(upload):
    ext = os.path.splitext(upload.name)[1].lower().lstrip(".")
    if ext not in ALLOWED_IMAGE_EXT:
        return False
    if upload.size and upload.size > settings.MAX_UPLOAD_SIZE:
        return False
    return True


def _save_uploaded_image(request, upload):
    ext = os.path.splitext(upload.name)[1].lower()
    filename = f"products/{uuid.uuid4().hex}{ext}"
    path = default_storage.save(filename, ContentFile(upload.read()))
    return request.build_absolute_uri(settings.MEDIA_URL + path)


def collect_images(request):
    """Return the image URL list for a request: existing URLs + uploaded files."""
    urls = []
    raw = request.data.get("images")
    if hasattr(request.data, "getlist"):
        raw_values = request.data.getlist("images")
    elif isinstance(raw, (list, tuple)):
        raw_values = list(raw)
    elif raw in (None, ""):
        raw_values = []
    else:
        raw_values = [raw]

    for item in raw_values:
        if not isinstance(item, str) or not item.strip():
            continue
        item = item.strip()
        if item.startswith("["):
            try:
                parsed = json.loads(item)
            except (ValueError, TypeError):
                continue
            for url in parsed if isinstance(parsed, list) else [parsed]:
                if isinstance(url, str) and url.strip():
                    urls.append(url.strip())
        else:
            urls.extend(u.strip() for u in item.split(",") if u.strip())

    images = list(dict.fromkeys(urls))
    for upload in request.FILES.getlist("images"):
        if not _image_is_allowed(upload):
            raise ValueError(
                f"Invalid image file: {upload.name}. Allowed formats: JPG, PNG, WEBP (max 5 MB)."
            )
        images.append(_save_uploaded_image(request, upload))

    return images


def _actor_id(request):
    user = getattr(request, "user", None)
    return getattr(user, "pk", None) if user else None


# ---------------------------------------------------------------------------
# Public views
# ---------------------------------------------------------------------------


class ListProductsView(APIView):
    """GET/POST /api/products/

    Admin list mode (paginated, filtered, sorted) is activated by any
    admin-only parameter. Otherwise GET behaves as the public product search.
    Both are paginated in the database with ``page`` and ``page_size``.
    POST creates a product and requires an authenticated admin.
    """

    def get_authenticators(self):
        if self.request.method == "POST":
            return [BearerTokenAuthentication()]
        return []

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdmin()]
        return []

    def get(self, request):
        try:
            params = request.query_params
            if any(key in params for key in ADMIN_LIST_PARAMS):
                total, results, pagination = search_services.admin_product_list(params)
            else:
                page, page_size = search_services.page_params(params)
                total, results, pagination = search_services.storefront_search(
                    name=params.get("name") or params.get("q") or "",
                    brand=params.get("brand") or "",
                    specification=params.get("specification") or "",
                    page=page,
                    page_size=page_size,
                )
            return CustomResponse.successful_response({"count": total, "results": results, **pagination})
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def post(self, request):
        serializer = ProductSerializer(data=request.data)
        if not serializer.is_valid():
            return CustomResponse.failed_response(
                "Validation failed",
                data=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = serializer.validated_data

        if product_store.sku_in_use(data["sku"].strip()):
            return _sku_taken()

        try:
            images = collect_images(request)
        except ValueError as e:
            return CustomResponse.failed_response(str(e), status=status.HTTP_400_BAD_REQUEST)

        try:
            doc, _ = product_store.save_product(data, images, actor_user_id=_actor_id(request))
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return CustomResponse.successful_response(
            doc,
            "Product created successfully",
            status=status.HTTP_201_CREATED,
        )


def _sku_taken():
    return CustomResponse.failed_response(
        "A product with this SKU already exists",
        data={"sku": "A product with this SKU already exists"},
        status=status.HTTP_400_BAD_REQUEST,
    )


class ProductMetaView(APIView):
    """GET /api/products/meta/ — filter options (categories, brands)."""

    def get(self, request):
        try:
            return CustomResponse.successful_response(product_store.get_filter_options())
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AdminProductWriteViewMixin:
    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [IsAdmin]


class ProductDetailView(APIView):
    """GET/PUT/PATCH/DELETE /api/products/<id>

    GET is public; write methods require an authenticated admin.
    """

    def get_authenticators(self):
        if self.request.method in ("PUT", "PATCH", "DELETE"):
            return [BearerTokenAuthentication()]
        return []

    def get_permissions(self):
        if self.request.method in ("PUT", "PATCH", "DELETE"):
            return [IsAdmin()]
        return []

    def get(self, request, product_id):
        try:
            product = catalog_cache.get_product(product_id)
        except product_store.ProductDoesNotExist as exc:
            return CustomResponse.failed_response(str(exc), status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return CustomResponse.successful_response(product)

    def _update(self, request, product_id):
        serializer = ProductSerializer(data=request.data)
        if not serializer.is_valid():
            return CustomResponse.failed_response(
                "Validation failed",
                data=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = serializer.validated_data

        if product_store.sku_in_use(data["sku"].strip(), exclude_id=product_id):
            return _sku_taken()

        try:
            images = collect_images(request)
        except ValueError as e:
            return CustomResponse.failed_response(str(e), status=status.HTTP_400_BAD_REQUEST)

        try:
            doc, _ = product_store.save_product(
                data, images, product_id=product_id, actor_user_id=_actor_id(request)
            )
        except product_store.ProductDoesNotExist as exc:
            return CustomResponse.failed_response(str(exc), status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return CustomResponse.successful_response(doc, "Product updated successfully")

    def put(self, request, product_id):
        return self._update(request, product_id)

    def patch(self, request, product_id):
        return self._update(request, product_id)

    def delete(self, request, product_id):
        try:
            product_store.delete_product(product_id, actor_user_id=_actor_id(request))
        except product_store.ProductDoesNotExist as exc:
            return CustomResponse.failed_response(str(exc), status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return CustomResponse.successful_response(message="Product deleted successfully")


class StockUpdateView(AdminProductWriteViewMixin, APIView):
    """PATCH /api/products/<id>/stock/"""

    def patch(self, request, product_id):
        serializer = StockUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return CustomResponse.failed_response(
                "Validation failed",
                data=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = serializer.validated_data

        user = getattr(request, "user", None)
        actor_id = getattr(user, "pk", None) if user else None

        try:
            result = InventoryManager.update_product_stock(
                product_id=product_id,
                stock_quantity=data.get("stock_quantity"),
                delta=data.get("delta"),
                min_stock_alert=data.get("min_stock_alert"),
                note=data.get("note", ""),
                actor_user_id=actor_id,
            )
        except InsufficientStockError as e:
            return CustomResponse.failed_response(str(e), status=status.HTTP_400_BAD_REQUEST)
        except VariantNotFoundError as e:
            return CustomResponse.failed_response(str(e), status=status.HTTP_404_NOT_FOUND)
        except InventoryError as e:
            return CustomResponse.failed_response(str(e), status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return CustomResponse.successful_response(result, "Stock updated successfully")


class StockHistoryView(AdminProductWriteViewMixin, APIView):
    """GET /api/products/<id>/stock/history/"""

    def get(self, request, product_id):
        logs = InventoryManager.get_inventory_logs(product_id=product_id)
        return CustomResponse.successful_response(
            LegacyInventoryLogSerializer(logs, many=True).data,
        )


class InventoryLogsView(AdminProductWriteViewMixin, APIView):
    """GET /api/products/inventory/logs/"""

    def get(self, request):
        product_id = None
        product_id_str = (request.query_params.get("product_id") or "").strip()
        if product_id_str.isdigit():
            product_id = int(product_id_str)

        action = (request.query_params.get("action") or "").strip() or None

        logs = InventoryManager.get_inventory_logs(
            product_id=product_id,
            action=action,
        )
        return CustomResponse.successful_response(
            LegacyInventoryLogSerializer(logs, many=True).data,
        )


class LowStockAlertsView(AdminProductWriteViewMixin, APIView):
    """GET /api/products/inventory/low-stock/ — active alerts by default."""

    def get(self, request):
        active_only = (request.query_params.get("all") or "").strip().lower() not in ("1", "true", "yes")
        alerts = InventoryManager.get_low_stock_alerts(active_only=active_only)
        return CustomResponse.successful_response(
            LegacyLowStockAlertSerializer(alerts, many=True).data,
        )


class LowStockAlertResolveView(AdminProductWriteViewMixin, APIView):
    """PATCH /api/products/inventory/low-stock/<alert_id>/resolve/"""

    def patch(self, request, alert_id):
        user = getattr(request, "user", None)
        actor_id = getattr(user, "pk", None) if user else None

        try:
            alert = InventoryManager.resolve_alert(
                alert_id=alert_id,
                resolved_by=actor_id,
            )
        except InventoryError as e:
            return CustomResponse.failed_response(str(e), status=status.HTTP_404_NOT_FOUND)

        return CustomResponse.successful_response(
            LegacyLowStockAlertSerializer(alert).data,
            "Alert resolved",
        )


# ---------------------------------------------------------------------------
# Catalog (categories, brands, banners) + homepage aggregation views
# ---------------------------------------------------------------------------


def _collect_single_image(request, field):
    """Return an uploaded file URL or the string value for the given field."""
    if request.FILES.get(field):
        return _save_uploaded_image(request, request.FILES[field])
    value = request.data.get(field, "")
    return value.strip() if isinstance(value, str) else ""


def _catalog_data(request):
    serializer = CatalogSerializer(data=request.data)
    if not serializer.is_valid():
        return None, CustomResponse.failed_response(
            "Validation failed",
            data=serializer.errors,
            status=status.HTTP_400_BAD_REQUEST,
        )
    data = serializer.validated_data
    data["image"] = _collect_single_image(request, "image")
    data["logo"] = _collect_single_image(request, "logo")
    return data, None


class CatalogWriteViewMixin(APIView):
    kind = None

    def get_authenticators(self):
        if self.request.method == "POST":
            return [BearerTokenAuthentication()]
        return []

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdmin()]
        return []

    def get(self, request):
        try:
            items = self._list(request)
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return CustomResponse.successful_response({"count": len(items), "results": items})

    def post(self, request):
        data, error = _catalog_data(request)
        if error:
            return error
        try:
            doc = catalog_store.create_document(self.kind, data)
        except catalog_store.CatalogConflict as exc:
            return CustomResponse.failed_response(str(exc), status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return CustomResponse.successful_response(
            doc,
            "Created successfully",
            status=status.HTTP_201_CREATED,
        )

    def _list(self, request):
        raise NotImplementedError


class CatalogDetailViewMixin(APIView):
    kind = None

    def get_authenticators(self):
        if self.request.method in ("PUT", "DELETE"):
            return [BearerTokenAuthentication()]
        return []

    def get_permissions(self):
        if self.request.method in ("PUT", "DELETE"):
            return [IsAdmin()]
        return []

    def get(self, request, doc_id):
        try:
            doc = catalog_store.get_document(self.kind, doc_id)
        except catalog_store.CatalogDoesNotExist as exc:
            return CustomResponse.failed_response(str(exc), status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return CustomResponse.successful_response(doc)

    def put(self, request, doc_id):
        data, error = _catalog_data(request)
        if error:
            return error
        try:
            doc = catalog_store.update_document(self.kind, doc_id, data)
        except catalog_store.CatalogDoesNotExist as exc:
            return CustomResponse.failed_response(str(exc), status=status.HTTP_404_NOT_FOUND)
        except catalog_store.CatalogConflict as exc:
            return CustomResponse.failed_response(str(exc), status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return CustomResponse.successful_response(doc, "Updated successfully")

    def delete(self, request, doc_id):
        try:
            catalog_store.delete_document(self.kind, doc_id)
        except catalog_store.CatalogDoesNotExist as exc:
            return CustomResponse.failed_response(str(exc), status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return CustomResponse.successful_response(message="Deleted successfully")


class CategoriesView(CatalogWriteViewMixin):
    """GET/POST /api/categories/ — list categories (with product counts) or create one."""

    kind = catalog_store.CATEGORY

    def _list(self, request):
        return catalog_cache.list_categories()


class CategoryDetailView(CatalogDetailViewMixin):
    kind = catalog_store.CATEGORY


class BrandsView(CatalogWriteViewMixin):
    """GET/POST /api/brands/ — list brands (with product counts) or create one."""

    kind = catalog_store.BRAND

    def _list(self, request):
        return catalog_cache.list_brands()


class BrandDetailView(CatalogDetailViewMixin):
    kind = catalog_store.BRAND


class BannersView(CatalogWriteViewMixin):
    """GET/POST /api/banners/ — list active banners (optionally by type) or create one."""

    kind = catalog_store.BANNER

    def get(self, request):
        banner_type = (request.query_params.get("type") or "").strip()
        if banner_type and banner_type not in catalog_store.BANNER_TYPES:
            return CustomResponse.failed_response(
                "type must be one of: hero, promotion",
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().get(request)

    def _list(self, request):
        banner_type = (request.query_params.get("type") or "").strip()
        return catalog_store.list_banners(banner_type=banner_type or None, active_only=True)


class BannerDetailView(CatalogDetailViewMixin):
    kind = catalog_store.BANNER


class RecommendedProductsView(APIView):
    """GET /api/products/recommended/

    Personalized for authenticated users (boosted by recently viewed categories),
    otherwise trending (best-selling, highly rated) products.
    """

    def get_authenticators(self):
        return [BearerTokenAuthentication()]

    def get(self, request):
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            user = None

        try:
            queryset = product_store.products_queryset().filter(status="active")
            if user is not None:
                history_ids = list(
                    RecentlyViewed.objects.filter(user_id=user.pk)
                    .values_list("product_id", flat=True)[:10]
                )
                category_ids = list(
                    Product.objects.filter(pk__in=history_ids, is_deleted=False)
                    .exclude(category=None)
                    .values_list("category_id", flat=True)
                    .distinct()[:5]
                )
                queryset = queryset.filter(Q(category_id__in=category_ids) | Q(is_featured=True))
            queryset = queryset.order_by(
                F("total_sales").desc(nulls_last=True),
                F("flat_rating").desc(nulls_last=True),
                "-pk",
            )
            products = [product_store.serialize(p) for p in queryset[:8]]
            return CustomResponse.successful_response({"count": len(products), "results": products})
        except Exception as e:
            return CustomResponse.failed_response(
                str(e),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class RecentlyViewedView(APIView):
    """GET/POST/DELETE /api/products/recently-viewed/

    GET with an authenticated user returns server history; otherwise the caller
    may pass ?ids=1,2,3 (guest history stored in localStorage) to hydrate products.
    """

    def get_authenticators(self):
        return [BearerTokenAuthentication()]

    def get(self, request):
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            product_ids = [
                r.product_id
                for r in RecentlyViewed.objects.filter(user_id=user.pk)[:20]
            ]
        else:
            product_ids = []
            raw = (request.query_params.get("ids") or "").strip()
            for part in raw.split(","):
                part = part.strip()
                if part.isdigit():
                    product_ids.append(int(part))
            product_ids = product_ids[:20]

        products = product_store.get_products(product_ids)
        return CustomResponse.successful_response({"count": len(products), "results": products})

    def post(self, request):
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return CustomResponse.successful_response(
                {"saved": False},
                "Guest history is stored client-side",
            )

        raw = (request.data.get("ids") or request.data.get("product_ids") or "").strip()
        product_ids = [int(p) for p in raw.split(",") if p.strip().isdigit()][:20]
        for product_id in product_ids:
            try:
                product = product_store.get_product(product_id)
            except product_store.ProductDoesNotExist:
                continue
            RecentlyViewed.objects.update_or_create(
                user_id=user.pk,
                product_id=product_id,
                defaults={
                    "product_name": product.get("name", ""),
                    "thumbnail": product.get("thumbnail", ""),
                },
            )
        return CustomResponse.successful_response({"saved": len(product_ids)})

    def delete(self, request):
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            RecentlyViewed.objects.filter(user_id=user.pk).delete()
            return CustomResponse.successful_response(message="History cleared")
        return CustomResponse.successful_response(message="Nothing to clear for guests")
