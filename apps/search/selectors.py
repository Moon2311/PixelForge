"""Base querysets used by search.

Price, stock and rating come from the denormalized, indexed columns on
``Product`` (maintained by ``apps.catalog.stats``), so filtering, sorting
and faceting never evaluates per-row subqueries or loads rows into Python.
"""

from django.db.models import Count, F, Q

from apps.catalog.models import Product, product_search_text


def with_search_text(queryset):
    """Alias the indexed ``search_text`` expression (see ``filters``)."""
    return queryset.alias(search_text=product_search_text())


def searchable_products():
    """Non-deleted products annotated for /api/search/products/."""
    return with_search_text(Product.objects.filter(is_deleted=False)).annotate(
        search_price=F("price_max"),
        search_in_stock=F("in_stock"),
        search_rating=F("rating_avg"),
        search_review_count=F("review_count"),
    )


def with_product_count(queryset):
    """Annotate categories/brands with their number of non-deleted products."""
    return queryset.annotate(
        product_count=Count("products", filter=Q(products__is_deleted=False))
    )
