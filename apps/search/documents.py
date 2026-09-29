"""Search document builder.

Builds denormalized product/category/brand documents straight from
PostgreSQL for the /api/search/ endpoints. ``product_documents_queryset``
prefetches everything a product document needs so a page of results is
built with a fixed number of queries.
"""

from typing import Any

from django.db.models import Count, Prefetch, Q

from apps.catalog.models import (
    Brand,
    Category,
    Product,
    ProductImage,
    ProductVariant,
    Subcategory,
    VariantAttributeValue,
)

def product_documents_queryset(queryset=None):
    """Return ``queryset`` with the relations and stats a document needs."""
    queryset = queryset if queryset is not None else Product.objects.filter(is_deleted=False)
    variants = (
        ProductVariant.objects.filter(is_deleted=False)
        .select_related("price", "inventory")
        .prefetch_related(
            Prefetch(
                "attribute_values",
                queryset=VariantAttributeValue.objects.select_related(
                    "attribute_value__attribute"
                ),
            )
        )
        .order_by("pk")
    )
    return queryset.select_related("brand", "category", "subcategory").prefetch_related(
        Prefetch("variants", queryset=variants, to_attr="active_variants"),
        Prefetch(
            "images",
            queryset=ProductImage.objects.order_by("display_order", "id"),
            to_attr="ordered_images",
        ),
        "specifications",
    )


def _ref(obj, **extra):
    if obj is None:
        return None
    return {"id": obj.pk, "name": obj.name, "slug": obj.slug, **extra}


def build_product_document(product: Product) -> dict[str, Any]:
    """Build a denormalized product document.

    Works on any Product, but is query-efficient only for instances loaded
    through ``product_documents_queryset``.
    """
    if not hasattr(product, "active_variants"):
        product = product_documents_queryset(Product.objects.filter(pk=product.pk)).get()
    variants = product.active_variants
    images = product.ordered_images

    variant_docs = []
    attributes = {}
    min_price = max_price = None
    total_stock = 0
    any_in_stock = False

    for variant in variants:
        price = getattr(variant, "price", None)
        inventory = getattr(variant, "inventory", None)

        variant_attrs = []
        for vav in variant.attribute_values.all():
            av = vav.attribute_value
            variant_attrs.append({
                "attribute_name": av.attribute.name,
                "attribute_slug": av.attribute.slug,
                "value": av.value,
            })
            attributes.setdefault(av.attribute.slug, {
                "name": av.attribute.name,
                "slug": av.attribute.slug,
                "values": [av.value],
            })

        variant_docs.append({
            "variant_id": variant.pk,
            "sku": variant.sku,
            "barcode": variant.barcode or "",
            "name": variant.name or "",
            "status": variant.status,
            "price": {
                "regular": float(price.regular_price) if price else 0,
                "sale": float(price.sale_price) if price and price.sale_price else None,
                "currency": price.currency if price else "USD",
            },
            "inventory": {
                "stock_quantity": inventory.stock_quantity if inventory else 0,
                "reserved_quantity": inventory.reserved_quantity if inventory else 0,
                "available_quantity": inventory.available_quantity if inventory else 0,
                "in_stock": inventory.is_in_stock if inventory else False,
            },
            "attribute_values": variant_attrs,
        })

        if price:
            current = float(price.sale_price or price.regular_price)
            min_price = current if min_price is None else min(min_price, current)
            max_price = current if max_price is None else max(max_price, current)

        if inventory:
            total_stock += inventory.available_quantity
            any_in_stock = any_in_stock or inventory.is_in_stock

    primary_image = next((img.image_url for img in images if img.is_primary), "")
    if not primary_image and images:
        primary_image = images[0].image_url

    brand = product.brand
    return {
        "product_id": product.pk,
        "sku": product.sku,
        "name": product.name,
        "slug": product.slug,
        "description": product.description or "",
        "short_description": product.short_description or "",
        "status": product.status,
        "is_featured": product.is_featured,
        "created_at": product.created_at.isoformat() if product.created_at else None,
        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
        "brand": _ref(brand, logo=brand.logo if brand else ""),
        "category": _ref(product.category),
        "subcategory": _ref(product.subcategory),
        "variants": variant_docs,
        "attributes": list(attributes.values()),
        "specifications": [
            {"name": s.name, "value": s.value, "unit": s.unit or ""}
            for s in product.specifications.all()
        ],
        "images": [
            {
                "image_id": img.pk,
                "url": img.image_url,
                "alt_text": img.alt_text or "",
                "is_primary": img.is_primary,
                "display_order": img.display_order,
            }
            for img in images
        ],
        "primary_image": primary_image,
        "price": {
            "regular": max_price or 0,
            "min": min_price or 0,
            "max": max_price or 0,
            "sale": None,
            "currency": "USD",
        },
        "rating": round(float(product.rating_avg or 0), 2),
        "review_count": product.review_count,
        "availability": {
            "in_stock": any_in_stock,
            "quantity": total_stock,
        },
        "tags": list(product.tags or []),
        "color": product.color,
        "size": product.size,
        "weight": product.weight or 0,
    }


def build_category_document(category: Category) -> dict[str, Any]:
    """Build a denormalized category document with product counts."""
    subcategories = (
        Subcategory.objects.filter(category=category, is_deleted=False)
        .annotate(product_count=Count("products", filter=Q(products__is_deleted=False)))
        .order_by("display_order", "name")
    )
    product_count = getattr(category, "product_count", None)
    if product_count is None:
        product_count = Product.objects.filter(category=category, is_deleted=False).count()

    return {
        "category_id": category.pk,
        "name": category.name,
        "slug": category.slug,
        "description": category.description or "",
        "image": category.image or "",
        "is_active": category.is_active,
        "display_order": category.display_order,
        "product_count": product_count,
        "subcategories": [
            {
                "subcategory_id": sub.pk,
                "name": sub.name,
                "slug": sub.slug,
                "is_active": sub.is_active,
                "display_order": sub.display_order,
                "product_count": sub.product_count,
            }
            for sub in subcategories
        ],
        "created_at": category.created_at.isoformat() if category.created_at else None,
        "updated_at": category.updated_at.isoformat() if category.updated_at else None,
    }


def build_brand_document(brand: Brand) -> dict[str, Any]:
    """Build a denormalized brand document with its product count."""
    product_count = getattr(brand, "product_count", None)
    if product_count is None:
        product_count = Product.objects.filter(brand=brand, is_deleted=False).count()

    return {
        "brand_id": brand.pk,
        "name": brand.name,
        "slug": brand.slug,
        "description": brand.description or "",
        "logo": brand.logo or "",
        "website": brand.website or "",
        "is_active": brand.is_active,
        "product_count": product_count,
        "created_at": brand.created_at.isoformat() if brand.created_at else None,
        "updated_at": brand.updated_at.isoformat() if brand.updated_at else None,
    }
