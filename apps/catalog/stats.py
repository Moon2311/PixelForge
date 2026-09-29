"""Denormalized per-product search stats (price range, stock, rating).

``refresh_product_stats`` recomputes them with one set-based UPDATE. Signal
handlers call it whenever a price, inventory row, variant or review is
saved or deleted, inside the same transaction, so the columns are always
consistent with the source rows. ``manage.py refresh_product_stats``
rebuilds everything (e.g. after raw SQL imports).
"""

from django.db.models import Avg, Count, Exists, F, Max, Min, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.catalog.models import Inventory, Product, ProductReview, ProductVariant, VariantPrice


def _price_stat(aggregate):
    current = Coalesce("sale_price", "regular_price")
    return Subquery(
        VariantPrice.objects.filter(variant__product=OuterRef("pk"), variant__is_deleted=False)
        .values("variant__product")
        .annotate(value=aggregate(current))
        .values("value")[:1]
    )


def _review_stat(aggregate):
    return Subquery(
        ProductReview.objects.filter(product=OuterRef("pk"), status=ProductReview.STATUS_APPROVED)
        .values("product")
        .annotate(value=aggregate)
        .values("value")[:1]
    )


def refresh_product_stats(product_ids=None):
    """Recompute stats for the given products (all products when None)."""
    queryset = Product.objects.all()
    if product_ids is not None:
        product_ids = [pk for pk in set(product_ids) if pk is not None]
        if not product_ids:
            return 0
        queryset = queryset.filter(pk__in=product_ids)
    return queryset.update(
        price_min=_price_stat(Min),
        price_max=_price_stat(Max),
        in_stock=Exists(
            Inventory.objects.filter(
                variant__product=OuterRef("pk"),
                variant__is_deleted=False,
                stock_quantity__gt=F("reserved_quantity"),
            )
        ),
        rating_avg=_review_stat(Avg("rating")),
        review_count=Coalesce(_review_stat(Count("pk")), Value(0)),
    )


def _product_id_of_variant(variant_id):
    return ProductVariant.objects.filter(pk=variant_id).values_list("product_id", flat=True).first()


@receiver([post_save, post_delete], sender=VariantPrice)
@receiver([post_save, post_delete], sender=Inventory)
def _variant_child_changed(sender, instance, **kwargs):
    refresh_product_stats([_product_id_of_variant(instance.variant_id)])


@receiver([post_save, post_delete], sender=ProductVariant)
@receiver([post_save, post_delete], sender=ProductReview)
def _product_child_changed(sender, instance, **kwargs):
    refresh_product_stats([instance.product_id])
