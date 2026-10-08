"""Cached catalog reads and their invalidation.

Cached (public data, identical for every user):

* product documents, GET /api/products/<id>/   pixelforge:product:<id>
  5 min + up to 30 s jitter; unknown ids cached as "not found" for 60-75 s
* active categories, GET /api/categories/      pixelforge:catalog:categories
* active brands, GET /api/brands/              pixelforge:catalog:brands
  10 min + up to 60 s jitter

Entries are deleted after the transaction that changes their source rows
commits (signal handlers below), so readers never re-cache data that is about
to be rolled back. TTLs only bound staleness if an invalidation is lost
(e.g. Redis was unreachable at that moment).

Checkout never reads from here: orders and cart validate products and stock
in PostgreSQL (``catalog.selectors``, ``catalog.inventory``).
"""

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.common import cache
from apps.catalog import catalog_store, product_store
from apps.catalog.models import (
    Brand,
    Category,
    Inventory,
    Product,
    ProductImage,
    ProductReview,
    ProductVariant,
    VariantPrice,
)

# None = settings defaults: CACHE_DEFAULT_TTL (300) + CACHE_TTL_JITTER (30),
# NEGATIVE_CACHE_TTL (60) + NEGATIVE_CACHE_TTL_JITTER (15).
PRODUCT_POLICY = cache.CachePolicy()
CATALOG_LIST_POLICY = cache.CachePolicy(ttl=600, jitter=60, negative_ttl=0)


def product_key(product_id):
    return cache.build_key("product", product_id)


def categories_key():
    return cache.build_key("catalog", "categories")


def brands_key():
    return cache.build_key("catalog", "brands")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_product(product_id):
    """``product_store.get_product`` through the cache. Raises ProductDoesNotExist."""
    return cache.get_or_set(
        product_key(product_id),
        lambda: product_store.get_product(product_id),
        policy=PRODUCT_POLICY,
        not_found=product_store.ProductDoesNotExist,
    )


def list_categories():
    """Active categories (with product counts) through the cache."""
    return cache.get_or_set(
        categories_key(),
        lambda: catalog_store.list_categories(active_only=True),
        policy=CATALOG_LIST_POLICY,
    )


def list_brands():
    """Active brands (with product counts) through the cache."""
    return cache.get_or_set(
        brands_key(),
        lambda: catalog_store.list_brands(active_only=True),
        policy=CATALOG_LIST_POLICY,
    )


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


def invalidate(*keys):
    """Delete ``keys`` once the current transaction commits (now, outside one)."""
    keys = [key for key in keys if key]
    if keys and settings.CACHE_ENABLED:
        transaction.on_commit(lambda: cache.delete(*keys))


def invalidate_products(product_ids):
    invalidate(*(product_key(pk) for pk in set(product_ids) if pk is not None))


def invalidate_all_products():
    """After bulk changes that bypass signals (e.g. a full stats rebuild)."""
    if settings.CACHE_ENABLED:
        transaction.on_commit(lambda: cache.delete_prefix("product"))


@receiver([post_save, post_delete], sender=Product)
def _product_changed(sender, instance, **kwargs):
    # Category and brand lists carry product counts.
    invalidate(product_key(instance.pk), categories_key(), brands_key())


@receiver([post_save, post_delete], sender=ProductVariant)
@receiver([post_save, post_delete], sender=ProductImage)
@receiver([post_save, post_delete], sender=ProductReview)
def _product_child_changed(sender, instance, **kwargs):
    invalidate_products([instance.product_id])


@receiver([post_save, post_delete], sender=VariantPrice)
@receiver([post_save, post_delete], sender=Inventory)
def _variant_child_changed(sender, instance, **kwargs):
    if not settings.CACHE_ENABLED:
        return
    product_id = (
        ProductVariant.objects.filter(pk=instance.variant_id)
        .values_list("product_id", flat=True)
        .first()
    )
    invalidate_products([product_id])


@receiver([post_save, post_delete], sender=Category)
def _category_changed(sender, instance, **kwargs):
    if not settings.CACHE_ENABLED:
        return
    # Product documents carry the category name.
    invalidate(categories_key())
    invalidate_products(Product.objects.filter(category_id=instance.pk).values_list("pk", flat=True))


@receiver([post_save, post_delete], sender=Brand)
def _brand_changed(sender, instance, **kwargs):
    if not settings.CACHE_ENABLED:
        return
    # Product documents carry the brand name.
    invalidate(brands_key())
    invalidate_products(Product.objects.filter(brand_id=instance.pk).values_list("pk", flat=True))
