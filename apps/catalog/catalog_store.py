"""Storage helpers for homepage catalog data (categories, brands, banners).

Backs the legacy /api/categories/, /api/brands/ and /api/banners/
endpoints with the PostgreSQL ``Category``, ``Brand`` and ``Banner``
models, returning the flat documents those endpoints have always served.
"""

from datetime import timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from apps.catalog.models import Banner, Brand, Category

CATEGORY = "categories"
BRAND = "brands"
BANNER = "banners"

BANNER_TYPES = (Banner.TYPE_HERO, Banner.TYPE_PROMOTION)


class CatalogDoesNotExist(Exception):
    pass


class CatalogConflict(Exception):
    pass


def _iso(value):
    return value.isoformat() if value else None


def _parse_datetime(value):
    value = (value or "").strip() if isinstance(value, str) else value
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _unique_slug(model, name, exclude_id=None):
    base = slugify(name)[:240] or "item"
    slug, n = base, 2
    qs = model.objects.exclude(pk=exclude_id) if exclude_id else model.objects.all()
    while qs.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize_category(obj):
    doc = {
        "id": obj.pk,
        "name": obj.name,
        "slug": obj.slug,
        "description": obj.description,
        "image": obj.image,
        "is_active": obj.is_active,
        "created_at": _iso(obj.created_at),
        "updated_at": _iso(obj.updated_at),
    }
    if hasattr(obj, "product_count"):
        doc["product_count"] = obj.product_count
    return doc


def _serialize_brand(obj):
    doc = {
        "id": obj.pk,
        "name": obj.name,
        "slug": obj.slug,
        "description": obj.description,
        "logo": obj.logo,
        "is_active": obj.is_active,
        "created_at": _iso(obj.created_at),
        "updated_at": _iso(obj.updated_at),
    }
    if hasattr(obj, "product_count"):
        doc["product_count"] = obj.product_count
    return doc


def _serialize_banner(obj):
    doc = {
        "id": obj.pk,
        "title": obj.title,
        "subtitle": obj.subtitle,
        "image": obj.image,
        "cta_text": obj.cta_text,
        "cta_link": obj.cta_link,
        "type": obj.type,
        "discount_badge": obj.discount_badge,
        "sort_order": obj.sort_order,
        "is_active": obj.is_active,
        "created_at": _iso(obj.created_at),
        "updated_at": _iso(obj.updated_at),
    }
    if obj.start_at:
        doc["start_at"] = _iso(obj.start_at)
    if obj.end_at:
        doc["end_at"] = _iso(obj.end_at)
    return doc


# ---------------------------------------------------------------------------
# Field application
# ---------------------------------------------------------------------------


def _apply_named(obj, data, image_field):
    """Shared by categories (``image``) and brands (``logo``)."""
    if "name" in data:
        obj.name = (data["name"] or "").strip()
        obj.slug = _unique_slug(type(obj), obj.name, exclude_id=obj.pk)
    for field in ("description", image_field):
        if field in data:
            setattr(obj, field, data[field] or "")
    if "is_active" in data:
        obj.is_active = bool(data["is_active"])


def _apply_category(obj, data):
    _apply_named(obj, data, "image")


def _apply_brand(obj, data):
    _apply_named(obj, data, "logo")


def _apply_banner(obj, data):
    for field in ("title", "subtitle", "image", "cta_text", "cta_link", "discount_badge"):
        if field in data:
            setattr(obj, field, data[field] or "")
    if data.get("type"):
        obj.type = data["type"]
    if "sort_order" in data:
        obj.sort_order = int(data["sort_order"] or 0)
    if "is_active" in data:
        obj.is_active = bool(data["is_active"])
    # Blank dates keep the stored value, matching the old document semantics.
    for field in ("start_at", "end_at"):
        parsed = _parse_datetime(data.get(field))
        if parsed is not None:
            setattr(obj, field, parsed)


KINDS = {
    CATEGORY: (Category, _serialize_category, _apply_category),
    BRAND: (Brand, _serialize_brand, _apply_brand),
    BANNER: (Banner, _serialize_banner, _apply_banner),
}


def _soft_deletable(model):
    return hasattr(model, "is_deleted")


def _live(model):
    qs = model.objects.all()
    return qs.filter(is_deleted=False) if _soft_deletable(model) else qs


def _label(kind):
    return kind[:-1].capitalize()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def get_document(kind, doc_id):
    model, serialize, _ = KINDS[kind]
    obj = _live(model).filter(pk=doc_id).first()
    if obj is None:
        raise CatalogDoesNotExist(f"{_label(kind)} {doc_id} not found")
    return serialize(obj)


def create_document(kind, data):
    model, serialize, apply = KINDS[kind]
    obj = None
    name = (data.get("name") or "").strip()
    if _soft_deletable(model) and name:
        existing = model.objects.filter(name__iexact=name).first()
        if existing is not None and not existing.is_deleted:
            raise CatalogConflict(f"{_label(kind)} '{name}' already exists")
        if existing is not None:
            # Re-creating an archived category/brand restores it.
            obj = existing
            obj.is_deleted = False
            obj.deleted_at = None
    obj = obj or model()
    apply(obj, data)
    _save(kind, obj)
    return serialize(obj)


def update_document(kind, doc_id, data):
    model, serialize, apply = KINDS[kind]
    obj = _live(model).filter(pk=doc_id).first()
    if obj is None:
        raise CatalogDoesNotExist(f"{_label(kind)} {doc_id} not found")
    apply(obj, data)
    _save(kind, obj)
    return serialize(obj)


def delete_document(kind, doc_id):
    model, _, _ = KINDS[kind]
    obj = _live(model).filter(pk=doc_id).first()
    if obj is None:
        raise CatalogDoesNotExist(f"{_label(kind)} {doc_id} not found")
    if _soft_deletable(model):
        obj.soft_delete()
    else:
        obj.delete()


def _save(kind, obj):
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError:
        raise CatalogConflict(f"{_label(kind)} with this name already exists") from None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _with_counts(queryset):
    return queryset.annotate(
        product_count=Count("products", filter=Q(products__is_deleted=False))
    )


def list_categories(active_only=True):
    qs = _live(Category)
    if active_only:
        qs = qs.filter(is_active=True)
    return [_serialize_category(c) for c in _with_counts(qs).order_by("id")[:500]]


def list_brands(active_only=True):
    qs = _live(Brand)
    if active_only:
        qs = qs.filter(is_active=True)
    return [_serialize_brand(b) for b in _with_counts(qs).order_by("id")[:500]]


def list_banners(banner_type=None, active_only=True):
    qs = Banner.objects.all()
    if banner_type:
        qs = qs.filter(type=banner_type)
    if active_only:
        now = timezone.now()
        qs = qs.filter(is_active=True).filter(
            Q(start_at__isnull=True) | Q(start_at__lte=now),
            Q(end_at__isnull=True) | Q(end_at__gte=now),
        )
    return [_serialize_banner(b) for b in qs.order_by("sort_order", "id")[:100]]
