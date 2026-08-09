import json
import os
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from apps.authentication import SharedTokenAuthentication
from apps.elasticsearch import get_elasticsearch_client
from apps.inventory_manager import InventoryManager, InventoryError, InsufficientStockError, VariantNotFoundError
from apps.legacy_serializers import LegacyInventoryLogSerializer, LegacyLowStockAlertSerializer
from apps.models import InventoryLog, LowStockAlert, RecentlyViewed
from apps.permissions import IsAdmin
from apps import product_store, catalog_store
from apps.serializers import (
    CatalogSerializer,
    ProductSerializer,
    StockUpdateSerializer,
)

ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp"}
ADMIN_LIST_PARAMS = {
    "page",
    "page_size",
    "search",
    "category",
    "stock_status",
    "min_price",
    "max_price",
    "status",
    "sort",
    "order",
    "limit",
    "featured",
    "flash_sale",
}


class APIResponse:
    @staticmethod
    def success(data=None, message="Success", status_code=status.HTTP_200_OK):
        return Response({"message": message, "status": status_code, "data": data}, status=status_code)

    @staticmethod
    def error(message="Error", status_code=status.HTTP_400_BAD_REQUEST, data=None):
        return Response({"message": message, "status": status_code, "data": data}, status=status_code)


def _split_csv(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def _escape_wildcard(value):
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")


def _wildcard_query(field, value):
    return {
        "wildcard": {
            field: {
                "value": f"*{_escape_wildcard(value)}*",
                "case_insensitive": True,
            }
        }
    }


def build_search_query(request):
    name = (request.query_params.get("name") or request.query_params.get("q") or "").strip()
    brand = (request.query_params.get("brand") or "").strip()
    specification = (request.query_params.get("specification") or "").strip()

    if not any([name, brand, specification]):
        return {"match_all": {}}

    # OR logic: a product matches if ANY field contains the term
    # (case-insensitive, partial substring, leading/trailing spaces trimmed).
    should = []

    for value in _split_csv(name):
        should.append({"match": {"name": {"query": value}}})
        should.append(_wildcard_query("name.keyword", value))

    for value in _split_csv(brand):
        should.append(_wildcard_query("brand_name", value))

    for value in _split_csv(specification):
        for field in ("description", "short_description", "size", "color", "tags"):
            should.append(_wildcard_query(field, value))
        should.append({"match": {"description": {"query": value}}})
        should.append({"match": {"short_description": {"query": value}}})

    return {"bool": {"should": should, "minimum_should_match": 1}}


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


def _record_log(request, action, product, before, after, note=""):
    InventoryLog.objects.create(
        product_id=product.get("id"),
        product_name=product.get("name", ""),
        sku=product.get("sku", ""),
        action=action,
        quantity_before=int(before or 0),
        quantity_after=int(after or 0),
        quantity_change=int((after or 0) - (before or 0)),
        note=note,
        actor=getattr(request.user, "username", "") or str(getattr(request.user, "pk", "")),
    )


def _actor_name(request):
    return getattr(request.user, "username", "") or str(getattr(request.user, "pk", ""))


def _sync_low_stock_alert(product):
    """Resolve open alerts when restocked; create one when below the threshold."""
    product_id = product.get("id")
    quantity = int(product.get("stock_quantity") or 0)
    minimum = int(product.get("min_stock_alert") or 0)

    if minimum and quantity <= minimum:
        exists = LowStockAlert.objects.filter(product_id=product_id, resolved=False).exists()
        if not exists:
            LowStockAlert.objects.create(
                product_id=product_id,
                product_name=product.get("name", ""),
                sku=product.get("sku", ""),
                quantity=quantity,
                min_stock_alert=minimum,
            )
    else:
        LowStockAlert.objects.filter(product_id=product_id, resolved=False).update(
            resolved=True, resolved_at=timezone.now()
        )


def _get_or_404(client, product_id):
    try:
        return product_store.get_product(client, product_id)
    except product_store.ProductDoesNotExist as exc:
        raise exc


# ---------------------------------------------------------------------------
# Public views
# ---------------------------------------------------------------------------


class ListProductsView(APIView):
    """GET/POST /api/products/

    Admin list mode (paginated, filtered, sorted) is activated by any
    admin-only parameter. Otherwise GET behaves as the public product search.
    POST creates a product and requires an authenticated admin.
    """

    def get_authenticators(self):
        if self.request.method == "POST":
            return [SharedTokenAuthentication()]
        return []

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdmin()]
        return []

    def get(self, request):
        client = get_elasticsearch_client()
        try:
            params = request.query_params
            if any(key in params for key in ADMIN_LIST_PARAMS):
                total, results, pagination = product_store.search_products(client, params)
                data = {"count": total, "results": results, **(pagination or {})}
                return APIResponse.success(data=data)

            query = build_search_query(request)
            resp = client.search(index=product_store.PRODUCT_INDEX, query=query, size=1000)
            hits = resp["hits"]["hits"]
            products = [product_store.normalize_hit(hit) for hit in hits]
            return APIResponse.success(data={"count": len(products), "results": products})
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        serializer = ProductSerializer(data=request.data)
        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed", status_code=status.HTTP_400_BAD_REQUEST,
                data=serializer.errors,
            )
        data = serializer.validated_data

        client = get_elasticsearch_client()
        try:
            product_store.ensure_mapping(client)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        existing = product_store.get_product_by_sku(client, data["sku"].strip())
        if existing is not None:
            return APIResponse.error(
                message="A product with this SKU already exists",
                status_code=status.HTTP_400_BAD_REQUEST,
                data={"sku": "A product with this SKU already exists"},
            )

        try:
            images = collect_images(request)
        except ValueError as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_400_BAD_REQUEST)

        product_id = product_store.next_product_id(client)
        doc = product_store.build_document(product_id, data, images=images)
        try:
            product_store.create_document(client, doc)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        _record_log(request, InventoryLog.ACTION_CREATE, doc, 0, doc["stock_quantity"])
        _sync_low_stock_alert(doc)
        return APIResponse.success(
            data=doc, message="Product created successfully", status_code=status.HTTP_201_CREATED
        )


class ProductMetaView(APIView):
    """GET /api/products/meta/ — filter options (categories, brands)."""

    def get(self, request):
        client = get_elasticsearch_client()
        try:
            return APIResponse.success(data=product_store.get_filter_options(client))
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AdminProductWriteViewMixin:
    authentication_classes = [SharedTokenAuthentication]
    permission_classes = [IsAdmin]


class ProductDetailView(APIView):
    """GET/PUT/PATCH/DELETE /api/products/<id>

    GET is public; write methods require an authenticated admin.
    """

    def get_authenticators(self):
        if self.request.method in ("PUT", "PATCH", "DELETE"):
            return [SharedTokenAuthentication()]
        return []

    def get_permissions(self):
        if self.request.method in ("PUT", "PATCH", "DELETE"):
            return [IsAdmin()]
        return []

    def get(self, request, product_id):
        client = get_elasticsearch_client()
        try:
            product = product_store.get_product(client, product_id)
        except product_store.ProductDoesNotExist as exc:
            return APIResponse.error(message=str(exc), status_code=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return APIResponse.success(data=product)

    def _validate_sku(self, client, sku, exclude_id=None):
        existing = product_store.get_product_by_sku(client, sku, exclude_id=exclude_id)
        if existing is not None:
            return APIResponse.error(
                message="A product with this SKU already exists",
                status_code=status.HTTP_400_BAD_REQUEST,
                data={"sku": "A product with this SKU already exists"},
            )
        return None

    def _update(self, request, product_id):
        serializer = ProductSerializer(data=request.data)
        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed", status_code=status.HTTP_400_BAD_REQUEST,
                data=serializer.errors,
            )
        data = serializer.validated_data

        client = get_elasticsearch_client()
        try:
            product = product_store.get_product(client, product_id)
        except product_store.ProductDoesNotExist as exc:
            return APIResponse.error(message=str(exc), status_code=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        sku_error = self._validate_sku(client, data["sku"].strip(), exclude_id=product_id)
        if sku_error:
            return sku_error

        try:
            images = collect_images(request)
        except ValueError as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_400_BAD_REQUEST)

        doc = product_store.build_document(product_id, data, existing=product, images=images)
        try:
            product_store.replace_document(client, doc)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        _record_log(request, InventoryLog.ACTION_UPDATE, doc, product["stock_quantity"], doc["stock_quantity"])
        _sync_low_stock_alert(doc)
        return APIResponse.success(data=doc, message="Product updated successfully")

    def put(self, request, product_id):
        return self._update(request, product_id)

    def patch(self, request, product_id):
        return self._update(request, product_id)

    def delete(self, request, product_id):
        client = get_elasticsearch_client()
        try:
            product = product_store.get_product(client, product_id)
        except product_store.ProductDoesNotExist as exc:
            return APIResponse.error(message=str(exc), status_code=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        try:
            product_store.delete_product_doc(client, product_id)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        _record_log(request, InventoryLog.ACTION_DELETE, product, product["stock_quantity"], 0)
        LowStockAlert.objects.filter(product_id=product_id, resolved=False).delete()
        return APIResponse.success(message="Product deleted successfully")


class StockUpdateView(AdminProductWriteViewMixin, APIView):
    """PATCH /api/products/<id>/stock/"""

    def patch(self, request, product_id):
        serializer = StockUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed", status_code=status.HTTP_400_BAD_REQUEST,
                data=serializer.errors,
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
            return APIResponse.error(
                message=str(e), status_code=status.HTTP_400_BAD_REQUEST
            )
        except VariantNotFoundError as e:
            return APIResponse.error(
                message=str(e), status_code=status.HTTP_404_NOT_FOUND
            )
        except InventoryError as e:
            return APIResponse.error(
                message=str(e), status_code=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return APIResponse.error(
                message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # Also update ES for backward compatibility
        client = get_elasticsearch_client()
        try:
            product = product_store.get_product(client, product_id)
            doc = dict(product)
            doc["stock_quantity"] = result["stock_quantity"]
            doc["min_stock_alert"] = result["min_stock_alert"]
            doc["updated_at"] = product_store.now_string()
            product_store.replace_document(client, doc)
        except Exception:
            pass  # ES update is best-effort; PostgreSQL is source of truth

        return APIResponse.success(data=result, message="Stock updated successfully")


class StockHistoryView(AdminProductWriteViewMixin, APIView):
    """GET /api/products/<id>/stock/history/"""

    def get(self, request, product_id):
        logs = InventoryManager.get_inventory_logs(product_id=product_id)
        return APIResponse.success(
            data=LegacyInventoryLogSerializer(logs, many=True).data
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
        return APIResponse.success(
            data=LegacyInventoryLogSerializer(logs, many=True).data
        )


class LowStockAlertsView(AdminProductWriteViewMixin, APIView):
    """GET /api/products/inventory/low-stock/ — active alerts by default."""

    def get(self, request):
        active_only = (request.query_params.get("all") or "").strip().lower() not in ("1", "true", "yes")
        alerts = InventoryManager.get_low_stock_alerts(active_only=active_only)
        return APIResponse.success(
            data=LegacyLowStockAlertSerializer(alerts, many=True).data
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
            return APIResponse.error(
                message=str(e), status_code=status.HTTP_404_NOT_FOUND
            )

        return APIResponse.success(
            data=LegacyLowStockAlertSerializer(alert).data, message="Alert resolved"
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


class CatalogWriteViewMixin(APIView):
    index = None
    build = staticmethod(lambda doc_id, data, existing=None: {})

    def get_authenticators(self):
        if self.request.method == "POST":
            return [SharedTokenAuthentication()]
        return []

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdmin()]
        return []

    def get(self, request):
        client = get_elasticsearch_client()
        try:
            items = self._list(client)
            return APIResponse.success(data={"count": len(items), "results": items})
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        serializer = CatalogSerializer(data=request.data)
        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed", status_code=status.HTTP_400_BAD_REQUEST,
                data=serializer.errors,
            )
        data = serializer.validated_data
        data["image"] = _collect_single_image(request, "image")
        data["logo"] = _collect_single_image(request, "logo")

        client = get_elasticsearch_client()
        try:
            catalog_store.ensure_mapping(client, self.index)
            doc_id = catalog_store._next_id(client, self.index)
            doc = self.build(doc_id, data)
            catalog_store.create_document(client, self.index, doc)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return APIResponse.success(data=doc, message="Created successfully", status_code=status.HTTP_201_CREATED)

    def _list(self, client):
        raise NotImplementedError


class CatalogDetailViewMixin(APIView):
    index = None
    build = staticmethod(lambda doc_id, data, existing=None: {})

    def get_authenticators(self):
        if self.request.method in ("PUT", "DELETE"):
            return [SharedTokenAuthentication()]
        return []

    def get_permissions(self):
        if self.request.method in ("PUT", "DELETE"):
            return [IsAdmin()]
        return []

    def get(self, request, doc_id):
        client = get_elasticsearch_client()
        try:
            doc = catalog_store.get_document(client, self.index, doc_id)
        except catalog_store.CatalogDoesNotExist as exc:
            return APIResponse.error(message=str(exc), status_code=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return APIResponse.success(data=doc)

    def put(self, request, doc_id):
        serializer = CatalogSerializer(data=request.data)
        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed", status_code=status.HTTP_400_BAD_REQUEST,
                data=serializer.errors,
            )
        data = serializer.validated_data
        data["image"] = _collect_single_image(request, "image")
        data["logo"] = _collect_single_image(request, "logo")

        client = get_elasticsearch_client()
        try:
            existing = catalog_store.get_document(client, self.index, doc_id)
        except catalog_store.CatalogDoesNotExist as exc:
            return APIResponse.error(message=str(exc), status_code=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        doc = self.build(doc_id, data, existing=existing)
        try:
            catalog_store.replace_document(client, self.index, doc)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return APIResponse.success(data=doc, message="Updated successfully")

    def delete(self, request, doc_id):
        client = get_elasticsearch_client()
        try:
            catalog_store.get_document(client, self.index, doc_id)
        except catalog_store.CatalogDoesNotExist as exc:
            return APIResponse.error(message=str(exc), status_code=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        try:
            catalog_store.delete_document(client, self.index, doc_id)
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return APIResponse.success(message="Deleted successfully")


class CategoriesView(CatalogWriteViewMixin):
    """GET/POST /api/categories/ — list categories (with product counts) or create one."""

    index = catalog_store.CATEGORY_INDEX
    build = staticmethod(catalog_store.build_category_document)

    def get(self, request):
        client = get_elasticsearch_client()
        try:
            items = catalog_store.list_categories(client, active_only=True)
            catalog_store.with_counts(client, items, "categories")
            return APIResponse.success(data={"count": len(items), "results": items})
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CategoryDetailView(CatalogDetailViewMixin):
    index = catalog_store.CATEGORY_INDEX
    build = staticmethod(catalog_store.build_category_document)


class BrandsView(CatalogWriteViewMixin):
    """GET/POST /api/brands/ — list brands (with product counts) or create one."""

    index = catalog_store.BRAND_INDEX
    build = staticmethod(catalog_store.build_brand_document)

    def get(self, request):
        client = get_elasticsearch_client()
        try:
            items = catalog_store.list_brands(client, active_only=True)
            catalog_store.with_counts(client, items, "brands")
            return APIResponse.success(data={"count": len(items), "results": items})
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BrandDetailView(CatalogDetailViewMixin):
    index = catalog_store.BRAND_INDEX
    build = staticmethod(catalog_store.build_brand_document)


class BannersView(CatalogWriteViewMixin):
    """GET/POST /api/banners/ — list active banners (optionally by type) or create one."""

    index = catalog_store.BANNER_INDEX
    build = staticmethod(catalog_store.build_banner_document)

    def get(self, request):
        client = get_elasticsearch_client()
        try:
            banner_type = (request.query_params.get("type") or "").strip()
            if banner_type and banner_type not in catalog_store.BANNER_TYPES:
                return APIResponse.error(
                    message="type must be one of: hero, promotion",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            items = catalog_store.list_banners(client, banner_type=banner_type or None, active_only=True)
            return APIResponse.success(data={"count": len(items), "results": items})
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BannerDetailView(CatalogDetailViewMixin):
    index = catalog_store.BANNER_INDEX
    build = staticmethod(catalog_store.build_banner_document)


class RecommendedProductsView(APIView):
    """GET /api/products/recommended/

    Personalized for authenticated users (boosted by recently viewed categories),
    otherwise trending (best-selling, highly rated) products.
    """

    def get_authenticators(self):
        return [SharedTokenAuthentication()]

    def get(self, request):
        client = get_elasticsearch_client()
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            user = None

        try:
            if user is not None:
                history_ids = list(
                    RecentlyViewed.objects.filter(user_id=user.pk)
                    .values_list("product_id", flat=True)
                )[:10]
                query = self._personalized_query(client, history_ids)
            else:
                query = {
                    "bool": {
                        "must": [{"term": {"status": "active"}}],
                        "should": [
                            {"term": {"is_featured": True}},
                            {"exists": {"field": "total_sales"}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            resp = client.search(
                index=product_store.PRODUCT_INDEX,
                query=query,
                sort=[
                    {"total_sales": {"order": "desc", "missing": "_last"}},
                    {"rating": {"order": "desc", "missing": "_last"}},
                ],
                size=8,
            )
            products = [product_store.normalize_hit(hit) for hit in resp["hits"]["hits"]]
            return APIResponse.success(data={"count": len(products), "results": products})
        except Exception as e:
            return APIResponse.error(message=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def _personalized_query(self, client, history_ids):
        categories = set()
        for product_id in history_ids:
            try:
                product = product_store.get_product(client, product_id)
            except product_store.ProductDoesNotExist:
                continue
            if product.get("category_name"):
                categories.add(product["category_name"])
        query = {"bool": {"must": [{"term": {"status": "active"}}], "should": [], "minimum_should_match": 1}}
        if categories:
            query["bool"]["should"].append({"terms": {"category_name": list(categories)[:5]}})
        query["bool"]["should"].append({"term": {"is_featured": True}})
        return query


class RecentlyViewedView(APIView):
    """GET/POST/DELETE /api/products/recently-viewed/

    GET with an authenticated user returns server history; otherwise the caller
    may pass ?ids=1,2,3 (guest history stored in localStorage) to hydrate products.
    """

    def get_authenticators(self):
        return [SharedTokenAuthentication()]

    def get(self, request):
        client = get_elasticsearch_client()
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

        products = []
        for product_id in product_ids:
            try:
                products.append(product_store.get_product(client, product_id))
            except product_store.ProductDoesNotExist:
                continue
        return APIResponse.success(data={"count": len(products), "results": products})

    def post(self, request):
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return APIResponse.success(data={"saved": False}, message="Guest history is stored client-side")

        client = get_elasticsearch_client()
        raw = (request.data.get("ids") or request.data.get("product_ids") or "").strip()
        product_ids = [int(p) for p in raw.split(",") if p.strip().isdigit()][:20]
        for product_id in product_ids:
            try:
                product = product_store.get_product(client, product_id)
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
        return APIResponse.success(data={"saved": len(product_ids)})

    def delete(self, request):
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            RecentlyViewed.objects.filter(user_id=user.pk).delete()
            return APIResponse.success(message="History cleared")
        return APIResponse.success(message="Nothing to clear for guests")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class HealthCheckView(APIView):
    """GET /api/health/ — service health check including database connectivity."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        health = {
            "status": "healthy",
            "service": "search-service",
            "checks": {},
        }

        # Database check
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            health["checks"]["database"] = {"status": "ok"}
        except Exception as e:
            health["status"] = "unhealthy"
            health["checks"]["database"] = {"status": "error", "message": str(e)}

        # Elasticsearch check
        try:
            client = get_elasticsearch_client()
            client.info()
            health["checks"]["elasticsearch"] = {"status": "ok"}
        except Exception as e:
            health["status"] = "degraded"
            health["checks"]["elasticsearch"] = {"status": "error", "message": str(e)}

        status_code = status.HTTP_200_OK if health["status"] == "healthy" else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(health, status=status_code)
