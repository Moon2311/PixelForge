"""Search use cases (called by views). All filtering runs in PostgreSQL.

* ``ProductSearch``        — /api/search/products/ (keywords, filters, facets)
* ``ProductAutocomplete``  — /api/search/autocomplete/
* ``CategorySearch`` / ``BrandSearch`` — /api/search/categories|brands/
* ``storefront_search`` / ``admin_product_list`` — GET /api/products/
"""

import logging
from typing import Any, Optional

from django.db.models import Case, Count, IntegerField, Q, Value, When

from apps.catalog import product_store
from apps.catalog.models import Brand, Category, Product
from apps.search import filters as search_filters
from apps.search import selectors
from apps.search.documents import (
    build_brand_document,
    build_category_document,
    build_product_document,
    product_documents_queryset,
)

logger = logging.getLogger("catalog.search")

PRICE_RANGES = [(None, 50), (50, 100), (100, 200), (200, 500), (500, None)]
RATING_BUCKETS = [4, 3, 2, 1]


def _range_label(low, high):
    def fmt(value):
        return "*" if value is None else f"{float(value)}"
    return f"{fmt(low)}-{fmt(high)}"


# ---------------------------------------------------------------------------
# /api/search/products/
# ---------------------------------------------------------------------------


class ProductSearch:
    """Product search with filtering, sorting, pagination and facets."""

    def search(
        self,
        query: Optional[str] = None,
        filters: Optional[dict] = None,
        sort: str = "-relevance",
        page: int = 1,
        page_size: int = 20,
        facets: bool = True,
    ) -> dict[str, Any]:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))

        queryset = self.filtered_queryset(query, filters or {})
        total = queryset.count()

        ordered = queryset.order_by(*search_filters.search_ordering(sort, bool(query)))
        offset = (page - 1) * page_size
        page_rows = list(ordered.values_list("pk", "search_score")[offset:offset + page_size])
        page_ids = [pk for pk, _ in page_rows]
        scores = dict(page_rows)

        products = product_documents_queryset(Product.objects.filter(pk__in=page_ids)).in_bulk()
        results = [
            {"id": pk, "score": scores[pk] if query else None, **build_product_document(products[pk])}
            for pk in page_ids
        ]

        return {
            "results": results,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
            "facets": self._build_facets(queryset) if facets else {},
        }

    @staticmethod
    def filtered_queryset(query: Optional[str], criteria: dict):
        queryset = selectors.searchable_products().filter(
            search_filters.search_filter_condition(criteria)
        )
        if query:
            queryset = queryset.filter(search_filters.all_words_condition(query))
        return queryset.annotate(search_score=search_filters.relevance_score(query))

    @staticmethod
    def _build_facets(queryset) -> dict:
        matched = Product.objects.filter(pk__in=queryset.values("pk"))

        def grouped(prefix, limit):
            rows = (
                matched.exclude(**{f"{prefix}_id": None})
                .values(f"{prefix}_id", f"{prefix}__name", f"{prefix}__slug")
                .annotate(count=Count("id"))
                .order_by("-count", f"{prefix}__name")[:limit]
            )
            return [
                {
                    "id": row[f"{prefix}_id"],
                    "name": row[f"{prefix}__name"],
                    "slug": row[f"{prefix}__slug"],
                    "count": row["count"],
                }
                for row in rows
            ]

        buckets = {"total": Count("pk"), "in_stock": Count("pk", filter=Q(search_in_stock=True))}
        for i, (low, high) in enumerate(PRICE_RANGES):
            condition = Q(search_price__isnull=False)
            if low is not None:
                condition &= Q(search_price__gte=low)
            if high is not None:
                condition &= Q(search_price__lt=high)
            buckets[f"price_{i}"] = Count("pk", filter=condition)
        for minimum in RATING_BUCKETS:
            buckets[f"rating_{minimum}"] = Count("pk", filter=Q(search_rating__gte=minimum))
        counts = queryset.aggregate(**buckets)

        return {
            "categories": grouped("category", 50),
            "subcategories": grouped("subcategory", 100),
            "brands": grouped("brand", 50),
            "price_ranges": [
                {"label": _range_label(low, high), "count": counts[f"price_{i}"]}
                for i, (low, high) in enumerate(PRICE_RANGES)
            ],
            "ratings": [
                {"min_rating": minimum, "count": counts[f"rating_{minimum}"]}
                for minimum in RATING_BUCKETS
            ],
            "availability": {
                "in_stock": counts["in_stock"],
                "out_of_stock": counts["total"] - counts["in_stock"],
            },
        }


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------


class ProductAutocomplete:
    """Product name suggestions for a typed prefix (name prefix matches first)."""

    def suggest(self, prefix: str, size: int = 10) -> list[dict]:
        products = (
            Product.objects.filter(is_deleted=False, status="active", name__icontains=prefix)
            .prefetch_related("images")
            .annotate(
                search_score=Case(
                    When(name__istartswith=prefix, then=Value(2)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
            .order_by("-search_score", "name")[:size]
        )
        suggestions = []
        for product in products:
            images = sorted(product.images.all(), key=lambda img: (img.display_order, img.pk))
            primary = next((img.image_url for img in images if img.is_primary), "")
            suggestions.append({
                "id": product.pk,
                "name": product.name,
                "slug": product.slug,
                "image": primary or (images[0].image_url if images else ""),
                "score": product.search_score,
            })
        return suggestions


# ---------------------------------------------------------------------------
# Category / brand search
# ---------------------------------------------------------------------------


class CategorySearch:
    """Search active categories by name/description."""

    def search(self, query: str = None, size: int = 50) -> list[dict]:
        queryset = Category.objects.filter(is_deleted=False, is_active=True)
        if query:
            queryset = queryset.filter(Q(name__icontains=query) | Q(description__icontains=query))
        queryset = selectors.with_product_count(queryset).order_by("display_order", "name")[:size]
        return [{"id": category.pk, **build_category_document(category)} for category in queryset]


class BrandSearch:
    """Search active brands by name/description."""

    def search(self, query: str = None, size: int = 50) -> list[dict]:
        queryset = Brand.objects.filter(is_deleted=False, is_active=True)
        if query:
            queryset = queryset.filter(Q(name__icontains=query) | Q(description__icontains=query))
        queryset = selectors.with_product_count(queryset).order_by("name")[:size]
        return [{"id": brand.pk, **build_brand_document(brand)} for brand in queryset]


# ---------------------------------------------------------------------------
# GET /api/products/ (flat storefront search and admin list)
# ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _split_csv(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _positive_int(value, default):
    value = str(value or "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else default


def page_params(params):
    """``(page, page_size)`` from ``page`` and ``page_size`` (or ``limit``)."""
    page = _positive_int(params.get("page"), 1)
    page_size = _positive_int(params.get("page_size") or params.get("limit"), DEFAULT_PAGE_SIZE)
    return page, min(page_size, MAX_PAGE_SIZE)


def _paginate(queryset, page, page_size):
    """Fetch one page of ``queryset`` from the database (LIMIT/OFFSET).

    Returns ``(total, results, pagination)``.
    """
    total = queryset.count()
    offset = (page - 1) * page_size
    results = [product_store.serialize(p) for p in queryset[offset:offset + page_size]]
    total_pages = (total + page_size - 1) // page_size
    return total, results, {
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1,
    }


def storefront_search(name="", brand="", specification="", page=1, page_size=DEFAULT_PAGE_SIZE):
    """Storefront search: a product matches if ANY term matches.

    Every comma-separated term from ``name``, ``brand`` and ``specification``
    is matched (case-insensitive substring) against the product's name, SKU,
    descriptions, specifications, color, size, tags, brand, category and
    subcategory. Products whose name contains a ``name`` term come first.
    Returns ``(total, results, pagination)`` for one page.
    """
    name_terms = _split_csv(name)
    terms = list(dict.fromkeys(name_terms + _split_csv(brand) + _split_csv(specification)))

    queryset = selectors.with_search_text(product_store.products_queryset())
    if terms:
        queryset = queryset.filter(search_filters.any_term_condition(terms))

    if name_terms:
        name_match = Q()
        for term in name_terms:
            name_match |= Q(name__icontains=term)
        queryset = queryset.alias(
            name_hit=Case(When(name_match, then=Value(1)), default=Value(0), output_field=IntegerField())
        ).order_by("-name_hit", *search_filters.FLAT_DEFAULT_SORT)
    else:
        queryset = queryset.order_by(*search_filters.FLAT_DEFAULT_SORT)
    return _paginate(queryset, page, page_size)


def admin_product_list(params):
    """Admin list: filtered, sorted and paginated.

    Returns ``(total, results, pagination)``.
    """
    queryset = (
        product_store.products_queryset()
        .filter(search_filters.flat_admin_condition(params))
        .order_by(*search_filters.flat_admin_ordering(params))
    )
    return _paginate(queryset, *page_params(params))
