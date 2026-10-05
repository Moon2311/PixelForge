"""Query building blocks for product search (pure ``Q``/expression builders).

Keyword matching is case-insensitive substring matching, the same
semantics the old wildcard queries had. It is designed around PostgreSQL
indexes rather than per-row joins:

* Text that lives on the product row is matched through one expression,
  ``catalog.models.product_search_text()``, which has a trigram GIN index
  (``LIKE '%WORD%'`` becomes an index scan).
* Matches on related rows (brand / category / subcategory names,
  specification values) are resolved to small id lists first, so the final
  ``OR`` only contains indexable predicates on the product table
  (``brand_id IN (...)`` etc.) and PostgreSQL can combine them with a
  BitmapOr instead of scanning every product.
"""

from decimal import Decimal

from django.db.models import Case, Exists, F, IntegerField, OuterRef, Q, Value, When
from django.db.models.functions import NullIf
from django.utils import timezone

from apps.catalog.models import (
    Brand,
    Category,
    ProductSpecification,
    Subcategory,
    VariantAttributeValue,
)

SEARCH_TEXT = "search_text"


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------


def _ids(queryset):
    return list(queryset.values_list("pk", flat=True))


def keyword_condition(word: str) -> Q:
    """Products where ``word`` appears in any searchable field.

    Requires the queryset to carry the ``search_text`` alias
    (see ``selectors.with_search_text``).
    """
    condition = Q(**{f"{SEARCH_TEXT}__contains": word.upper()})

    brand_ids = _ids(Brand.objects.filter(name__icontains=word))
    if brand_ids:
        condition |= Q(brand_id__in=brand_ids)
    category_ids = _ids(Category.objects.filter(name__icontains=word))
    if category_ids:
        condition |= Q(category_id__in=category_ids)
    subcategory_ids = _ids(Subcategory.objects.filter(name__icontains=word))
    if subcategory_ids:
        condition |= Q(subcategory_id__in=subcategory_ids)
    spec_product_ids = list(
        ProductSpecification.objects.filter(value__icontains=word)
        .values_list("product_id", flat=True)
        .distinct()
    )
    if spec_product_ids:
        condition |= Q(pk__in=spec_product_ids)
    return condition


def all_words_condition(query: str) -> Q:
    """Every whitespace-separated word must match (AND of ``keyword_condition``)."""
    condition = Q()
    for word in query.split():
        condition &= keyword_condition(word)
    return condition


def any_term_condition(terms) -> Q:
    """At least one of ``terms`` must match (OR of ``keyword_condition``)."""
    condition = Q()
    for term in terms:
        condition |= keyword_condition(term)
    return condition


def relevance_score(query):
    """Rough relevance: exact name > name prefix > name contains > SKU match."""
    if not query:
        return Value(0, output_field=IntegerField())
    return Case(
        When(name__iexact=query, then=Value(4)),
        When(name__istartswith=query, then=Value(3)),
        When(name__icontains=query, then=Value(2)),
        When(sku__icontains=query, then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )


# ---------------------------------------------------------------------------
# /api/search/products/ filters and ordering
# ---------------------------------------------------------------------------

SEARCH_SORT_FIELDS = {
    "name": "name",
    "price": "search_price",
    "rating": "search_rating",
    "review_count": "search_review_count",
    "created_at": "created_at",
    "relevance": "search_score",
}


def search_filter_condition(filters: dict) -> Q:
    """Structured filters for /api/search/products/ (needs search annotations)."""
    condition = Q(status=filters.get("status", "active"))

    lookups = {
        "category_id": "category_id",
        "category_slug": "category__slug",
        "subcategory_id": "subcategory_id",
        "subcategory_slug": "subcategory__slug",
        "brand_id": "brand_id",
        "brand_slug": "brand__slug",
    }
    for key, lookup in lookups.items():
        if filters.get(key):
            condition &= Q(**{lookup: filters[key]})

    if filters.get("min_price") is not None:
        condition &= Q(search_price__gte=Decimal(str(filters["min_price"])))
    if filters.get("max_price") is not None:
        condition &= Q(search_price__lte=Decimal(str(filters["max_price"])))

    if filters.get("in_stock") is not None:
        condition &= Q(search_in_stock=filters["in_stock"])

    if filters.get("is_featured") is not None:
        condition &= Q(is_featured=filters["is_featured"])

    if filters.get("min_rating") is not None:
        condition &= Q(search_rating__gte=float(filters["min_rating"]))

    for attr_slug, values in (filters.get("attributes") or {}).items():
        if isinstance(values, str):
            values = [values]
        condition &= Q(Exists(
            VariantAttributeValue.objects.filter(
                variant__product=OuterRef("pk"),
                variant__is_deleted=False,
                attribute_value__attribute__slug=attr_slug,
                attribute_value__value__in=values,
            )
        ))

    tags = filters.get("tags")
    if tags:
        if isinstance(tags, str):
            tags = [tags]
        tag_condition = Q()
        for tag in tags:
            tag_condition |= Q(tags__contains=[tag])
        condition &= tag_condition

    return condition


def search_ordering(sort: str, has_query: bool) -> list:
    key = sort.lstrip("-")
    field = SEARCH_SORT_FIELDS.get(key)
    if field is None or (key == "relevance" and not has_query):
        return ["-created_at", "-pk"]
    expression = F(field).desc(nulls_last=True) if sort.startswith("-") else F(field).asc(nulls_last=True)
    return [expression, "-created_at", "-pk"]


# ---------------------------------------------------------------------------
# Flat /api/products/ admin list filters and ordering
# ---------------------------------------------------------------------------

FLAT_SORT_FIELDS = {
    "name": "name",
    "brand": "brand__name",
    "category": "category__name",
    "sku": "sku",
    "price": "flat_price",
    "stock_quantity": "flat_stock",
    "total_sales": "total_sales",
    "rating": "flat_rating",
    "created_at": "created_at",
    "updated_at": "updated_at",
    # Share of the price taken off by the sale price.
    "discount": (F("flat_price") - F("flat_discount_price")) / NullIf(F("flat_price"), 0),
}

FLAT_DEFAULT_SORT = [F("updated_at").desc(nulls_last=True), "-pk"]


def _truthy(value):
    return (value or "").strip().lower() in ("1", "true", "yes")


def flat_admin_condition(params) -> Q:
    """Admin list filters (needs ``catalog.product_store.products_queryset`` annotations)."""
    condition = Q()

    search = (params.get("search") or "").strip()
    if search:
        brand_ids = _ids(Brand.objects.filter(name__icontains=search))
        condition &= (
            Q(name__icontains=search)
            | Q(sku__icontains=search)
            | Q(brand_id__in=brand_ids)
        )

    category = (params.get("category") or "").strip()
    if category:
        condition &= Q(category__name=category)

    brand = (params.get("brand") or "").strip()
    if brand:
        condition &= Q(brand__name=brand)

    status = (params.get("status") or "").strip()
    if status:
        condition &= Q(status=status)

    stock_status = (params.get("stock_status") or "").strip()
    if stock_status == "in_stock":
        condition &= Q(flat_stock__gt=0)
    elif stock_status == "out_of_stock":
        condition &= Q(flat_stock__lte=0) | Q(flat_stock__isnull=True)
    elif stock_status == "low_stock":
        condition &= Q(flat_stock__gt=0, flat_stock__lte=F("flat_min_stock"))

    for key, lookup in (("min_price", "flat_price__gte"), ("max_price", "flat_price__lte")):
        raw = (params.get(key) or "").strip()
        if raw:
            try:
                condition &= Q(**{lookup: float(raw)})
            except ValueError:
                pass

    if _truthy(params.get("featured")):
        condition &= Q(is_featured=True)

    if _truthy(params.get("on_sale")):
        condition &= Q(flat_discount_price__isnull=False, flat_discount_price__lt=F("flat_price"))

    if _truthy(params.get("flash_sale")):
        condition &= Q(flash_sale=True, flash_sale_ends_at__gte=timezone.now())

    return condition


def flat_admin_ordering(params) -> list:
    raw = (params.get("sort") or "").strip()
    if not raw:
        return FLAT_DEFAULT_SORT
    field = FLAT_SORT_FIELDS.get(raw.lstrip("-"))
    if field is None:
        return FLAT_DEFAULT_SORT
    desc = raw.startswith("-") or (params.get("order") or "asc").strip().lower() == "desc"
    expression = F(field) if isinstance(field, str) else field
    expression = expression.desc(nulls_last=True) if desc else expression.asc(nulls_last=True)
    return [expression, "-pk"]
