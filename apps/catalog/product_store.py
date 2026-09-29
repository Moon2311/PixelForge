"""Flat product store backing the legacy /api/products/ endpoints.

Each legacy product maps onto the normalized catalog: one ``Product`` row
plus a single default ``ProductVariant`` (same SKU) carrying the price and
inventory, and ``ProductImage`` rows for the image URLs. ``serialize``
turns that back into the flat dict the frontend expects.
"""

from datetime import timezone as dt_timezone

from django.db import transaction
from django.db.models import F, OuterRef, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from apps.catalog.inventory import InventoryManager
from apps.catalog.models import (
    Brand,
    Category,
    Inventory,
    InventoryLog,
    Product,
    ProductImage,
    ProductVariant,
    VariantPrice,
)

class ProductDoesNotExist(Exception):
    pass


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _primary_variant():
    return ProductVariant.objects.filter(
        product=OuterRef("pk"), is_deleted=False
    ).order_by("pk")


def products_queryset():
    """Active products annotated with everything ``serialize`` needs."""
    primary = _primary_variant()
    return (
        Product.objects.filter(is_deleted=False)
        .select_related("brand", "category")
        .prefetch_related("images")
        .annotate(
            flat_price=Subquery(primary.values("price__regular_price")[:1]),
            flat_discount_price=Subquery(primary.values("price__sale_price")[:1]),
            flat_stock=Subquery(primary.values("inventory__stock_quantity")[:1]),
            flat_min_stock=Subquery(primary.values("inventory__low_stock_threshold")[:1]),
            flat_rating=F("rating_avg"),
            flat_reviews=F("review_count"),
        )
    )


def _float(value):
    return float(value) if value is not None else None


def _iso(value):
    return value.isoformat() if value else ""


def serialize(product):
    """Flatten an annotated Product into the legacy product document."""
    images = [img.image_url for img in sorted(
        product.images.all(), key=lambda img: (not img.is_primary, img.display_order, img.pk)
    )]
    return {
        "id": product.pk,
        "name": product.name,
        "slug": product.slug,
        "description": product.description,
        "short_description": product.short_description,
        "specifications": product.specifications_text,
        "category_id": product.category_id or 0,
        "category_name": product.category.name if product.category else "",
        "brand_id": product.brand_id or 0,
        "brand_name": product.brand.name if product.brand else "",
        "sku": product.sku,
        "price": _float(product.flat_price) or 0.0,
        "discount_price": _float(product.flat_discount_price),
        "cost_price": _float(product.cost_price),
        "stock_quantity": product.flat_stock or 0,
        "min_stock_alert": product.flat_min_stock or 0,
        "status": product.status,
        "is_featured": product.is_featured,
        "total_sales": product.total_sales,
        "flash_sale": product.flash_sale,
        "flash_sale_price": _float(product.flash_sale_price),
        "flash_sale_ends_at": _iso(product.flash_sale_ends_at),
        "color": product.color,
        "size": product.size,
        "weight": product.weight,
        "tags": list(product.tags or []),
        "images": images,
        "thumbnail": images[0] if images else "",
        "rating": round(float(product.flat_rating or 0), 2),
        "reviews_count": product.flat_reviews or 0,
        "created_at": _iso(product.created_at),
        "updated_at": _iso(product.updated_at),
    }


def get_product(product_id):
    try:
        return serialize(products_queryset().get(pk=product_id))
    except Product.DoesNotExist:
        raise ProductDoesNotExist(f"Product {product_id} not found") from None


def get_products(product_ids):
    """Serialize products by id, preserving the given order and skipping missing ones."""
    found = products_queryset().in_bulk(product_ids)
    return [serialize(found[pk]) for pk in product_ids if pk in found]


def sku_in_use(sku, exclude_id=None):
    """True if another product (including archived ones) or variant owns this SKU."""
    products = Product.objects.filter(sku=sku)
    variants = ProductVariant.objects.filter(sku=sku)
    if exclude_id is not None:
        products = products.exclude(pk=exclude_id)
        variants = variants.exclude(product_id=exclude_id)
    return products.exists() or variants.exists()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _unique_slug(model, name, exclude_id=None, fallback="item"):
    base = slugify(name)[:240] or fallback
    slug, n = base, 2
    qs = model.objects.all()
    if exclude_id is not None:
        qs = qs.exclude(pk=exclude_id)
    while qs.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _resolve(model, object_id, name):
    """Find a Category/Brand by id, else by name (creating it if needed)."""
    if object_id:
        obj = model.objects.filter(pk=object_id).first()
        if obj is not None:
            return obj
    name = (name or "").strip()
    if not name:
        return None
    obj = model.objects.filter(name__iexact=name).first()
    if obj is None:
        obj = model.objects.create(name=name, slug=_unique_slug(model, name))
    elif obj.is_deleted:
        obj.is_deleted = False
        obj.deleted_at = None
        obj.save(update_fields=["is_deleted", "deleted_at", "updated_at"])
    return obj


def _parse_tags(value):
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return list(value or [])


def _parse_datetime(value):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


PRODUCT_FIELDS = {
    # legacy key -> (model field, converter)
    "name": ("name", str),
    "description": ("description", str),
    "short_description": ("short_description", str),
    "specifications": ("specifications_text", str),
    "cost_price": ("cost_price", lambda v: v),
    "status": ("status", str),
    "is_featured": ("is_featured", bool),
    "total_sales": ("total_sales", int),
    "flash_sale": ("flash_sale", bool),
    "flash_sale_price": ("flash_sale_price", lambda v: v),
    "flash_sale_ends_at": ("flash_sale_ends_at", _parse_datetime),
    "color": ("color", str),
    "size": ("size", str),
    "weight": ("weight", lambda v: v),
    "tags": ("tags", _parse_tags),
}


def _set_images(product, images):
    product.images.all().delete()
    ProductImage.objects.bulk_create([
        ProductImage(product=product, image_url=url, is_primary=(i == 0), display_order=i)
        for i, url in enumerate(images)
    ])


def save_product(validated, images, product_id=None, actor_user_id=None):
    """Create (``product_id=None``) or update a product from legacy data.

    Returns ``(document, previous_stock)``. Raises ProductDoesNotExist.
    """
    with transaction.atomic():
        if product_id is None:
            product = Product(status="active", created_by=actor_user_id)
            previous_stock = 0
        else:
            product = Product.objects.select_for_update().filter(
                pk=product_id, is_deleted=False
            ).first()
            if product is None:
                raise ProductDoesNotExist(f"Product {product_id} not found")
            previous_stock = None

        for key, (field, convert) in PRODUCT_FIELDS.items():
            if key in validated:
                setattr(product, field, convert(validated[key]))

        product.sku = validated["sku"].strip()
        product.slug = _unique_slug(Product, product.name, exclude_id=product.pk, fallback="product")
        product.category = _resolve(Category, validated.get("category_id"), validated.get("category_name"))
        product.brand = _resolve(Brand, validated.get("brand_id"), validated.get("brand_name"))
        product.updated_by = actor_user_id
        product.save()

        variant = product.variants.filter(is_deleted=False).order_by("pk").first()
        if variant is None:
            variant = ProductVariant.objects.create(product=product, sku=product.sku)
        elif variant.sku != product.sku:
            variant.sku = product.sku
            variant.save(update_fields=["sku", "updated_at"])

        price, _ = VariantPrice.objects.get_or_create(
            variant=variant, defaults={"regular_price": validated["price"]}
        )
        price.regular_price = validated["price"]
        if "discount_price" in validated:
            price.sale_price = validated["discount_price"]
        price.save()

        inventory, created = Inventory.objects.select_for_update().get_or_create(
            variant=variant,
            defaults={"stock_quantity": 0, "low_stock_threshold": 0},
        )
        if previous_stock is None:
            previous_stock = inventory.stock_quantity
        if "stock_quantity" in validated:
            inventory.stock_quantity = validated["stock_quantity"]
        if "min_stock_alert" in validated:
            inventory.low_stock_threshold = validated["min_stock_alert"]
        inventory.save()

        _set_images(product, images)

        is_new = product_id is None
        InventoryLog.objects.create(
            variant=variant,
            previous_quantity=previous_stock,
            new_quantity=inventory.stock_quantity,
            quantity_change=inventory.stock_quantity - previous_stock,
            action=InventoryLog.ACTION_CREATE if is_new else InventoryLog.ACTION_UPDATE,
            actor_user_id=actor_user_id,
        )
        InventoryManager._handle_low_stock_alert(inventory)


    return get_product(product.pk), previous_stock


def delete_product(product_id, actor_user_id=None):
    """Archive (soft delete) a product and close its open low-stock alerts."""
    with transaction.atomic():
        product = Product.objects.select_for_update().filter(pk=product_id, is_deleted=False).first()
        if product is None:
            raise ProductDoesNotExist(f"Product {product_id} not found")

        for variant in product.variants.filter(is_deleted=False).select_related("inventory"):
            inventory = getattr(variant, "inventory", None)
            if inventory is not None:
                InventoryLog.objects.create(
                    variant=variant,
                    previous_quantity=inventory.stock_quantity,
                    new_quantity=0,
                    quantity_change=-inventory.stock_quantity,
                    action=InventoryLog.ACTION_DELETE,
                    actor_user_id=actor_user_id,
                )
            variant.low_stock_alerts.filter(status="open").delete()

        product.soft_delete()


# ---------------------------------------------------------------------------
# Filter options
# ---------------------------------------------------------------------------


def get_filter_options():
    """Distinct category and brand names used by active products."""
    products = Product.objects.filter(is_deleted=False)
    categories = (
        products.exclude(category=None).values_list("category__name", flat=True)
        .distinct().order_by("category__name")[:100]
    )
    brands = (
        products.exclude(brand=None).values_list("brand__name", flat=True)
        .distinct().order_by("brand__name")[:100]
    )
    return {"categories": list(categories), "brands": list(brands)}
