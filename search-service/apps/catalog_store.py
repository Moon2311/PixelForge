"""Storage helpers for homepage catalog data (categories, brands, banners).

Categories, brands and banners live in their own Elasticsearch indices so the
homepage can be fully driven by Admin-managed data. Product counts are computed
with an aggregation against the products index.
"""

import re
from datetime import datetime, timezone

from elasticsearch import NotFoundError

from apps.elasticsearch import get_elasticsearch_client

PRODUCT_INDEX = "products"
CATEGORY_INDEX = "categories"
BRAND_INDEX = "brands"
BANNER_INDEX = "banners"

BANNER_TYPES = ("hero", "promotion")

MAPPINGS = {
    CATEGORY_INDEX: {
        "id": {"type": "integer"},
        "name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "slug": {"type": "keyword"},
        "description": {"type": "text"},
        "image": {"type": "keyword"},
        "is_active": {"type": "boolean"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    },
    BRAND_INDEX: {
        "id": {"type": "integer"},
        "name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "slug": {"type": "keyword"},
        "description": {"type": "text"},
        "logo": {"type": "keyword"},
        "is_active": {"type": "boolean"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    },
    BANNER_INDEX: {
        "id": {"type": "integer"},
        "title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "subtitle": {"type": "text"},
        "image": {"type": "keyword"},
        "cta_text": {"type": "keyword"},
        "cta_link": {"type": "keyword"},
        "type": {"type": "keyword"},
        "discount_badge": {"type": "keyword"},
        "sort_order": {"type": "integer"},
        "is_active": {"type": "boolean"},
        "start_at": {"type": "date"},
        "end_at": {"type": "date"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    },
}


class CatalogDoesNotExist(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def now_string():
    return _now()


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "item"


def normalize_hit(hit):
    return {"id": hit["_id"], **hit["_source"]}


def ensure_mapping(client, index):
    """Create the index (if missing) and apply its mapping (idempotent)."""
    client.indices.create(index=index, ignore=400)
    client.indices.put_mapping(index=index, properties=MAPPINGS[index])


def ensure_all_mappings(client):
    for index in (CATEGORY_INDEX, BRAND_INDEX, BANNER_INDEX):
        ensure_mapping(client, index)


def _next_id(client, index):
    resp = client.search(index=index, size=0, aggs={"max_id": {"max": {"field": "id"}}})
    max_id = resp["aggregations"]["max_id"]["value"]
    return int(max_id) + 1 if max_id else 1


def get_document(client, index, doc_id):
    try:
        resp = client.get(index=index, id=doc_id)
    except NotFoundError:
        raise CatalogDoesNotExist(f"{index[:-1].capitalize()} {doc_id} not found") from None
    return normalize_hit(resp)


def create_document(client, index, doc):
    client.index(index=index, id=doc["id"], document=doc, refresh="wait_for")
    return doc


def replace_document(client, index, doc):
    client.index(index=index, id=doc["id"], document=doc, refresh="wait_for")
    return doc


def delete_document(client, index, doc_id):
    client.delete(index=index, id=doc_id, refresh="wait_for")


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def build_category_document(doc_id, data, existing=None):
    existing = existing or {}
    now = _now()
    return {
        "id": doc_id,
        "name": data.get("name", existing.get("name", "")),
        "slug": _slugify(data.get("name", existing.get("name", ""))),
        "description": data.get("description", existing.get("description", "")),
        "image": data.get("image", existing.get("image", "")),
        "is_active": bool(data.get("is_active", existing.get("is_active", True))),
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }


def list_categories(client, active_only=True):
    query = (
        {"term": {"is_active": True}} if active_only else {"match_all": {}}
    )
    resp = client.search(
        index=CATEGORY_INDEX,
        query=query,
        size=500,
        sort=[{"id": {"order": "asc"}}],
    )
    return [normalize_hit(hit) for hit in resp["hits"]["hits"]]


# ---------------------------------------------------------------------------
# Brands
# ---------------------------------------------------------------------------


def build_brand_document(doc_id, data, existing=None):
    existing = existing or {}
    now = _now()
    return {
        "id": doc_id,
        "name": data.get("name", existing.get("name", "")),
        "slug": _slugify(data.get("name", existing.get("name", ""))),
        "description": data.get("description", existing.get("description", "")),
        "logo": data.get("logo", existing.get("logo", "")),
        "is_active": bool(data.get("is_active", existing.get("is_active", True))),
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }


def list_brands(client, active_only=True):
    query = {"term": {"is_active": True}} if active_only else {"match_all": {}}
    resp = client.search(
        index=BRAND_INDEX,
        query=query,
        size=500,
        sort=[{"id": {"order": "asc"}}],
    )
    return [normalize_hit(hit) for hit in resp["hits"]["hits"]]


# ---------------------------------------------------------------------------
# Banners
# ---------------------------------------------------------------------------


def build_banner_document(doc_id, data, existing=None):
    existing = existing or {}
    now = _now()

    def _date(value, fallback):
        value = (value or "").strip() if value is not None else ""
        return value or (fallback or "").strip() or None

    doc = {
        "id": doc_id,
        "title": data.get("title", existing.get("title", "")),
        "subtitle": data.get("subtitle", existing.get("subtitle", "")),
        "image": data.get("image", existing.get("image", "")),
        "cta_text": data.get("cta_text", existing.get("cta_text", "Shop Now")),
        "cta_link": data.get("cta_link", existing.get("cta_link", "/products")),
        "type": data.get("type", existing.get("type", "hero")),
        "discount_badge": data.get("discount_badge", existing.get("discount_badge", "")),
        "sort_order": int(data.get("sort_order", existing.get("sort_order", 0))),
        "is_active": bool(data.get("is_active", existing.get("is_active", True))),
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }
    start_at = _date(data.get("start_at"), existing.get("start_at"))
    end_at = _date(data.get("end_at"), existing.get("end_at"))
    if start_at:
        doc["start_at"] = start_at
    if end_at:
        doc["end_at"] = end_at
    return doc


def _banner_active_filter():
    now = _now()
    return {
        "bool": {
            "must": [{"term": {"is_active": True}}],
            "should": [
                {"bool": {"must_not": [{"exists": {"field": "start_at"}}]}},
                {"range": {"start_at": {"lte": now}}},
            ],
            "minimum_should_match": 1,
            "filter": [
                {"bool": {"should": [
                    {"bool": {"must_not": [{"exists": {"field": "end_at"}}]}},
                    {"range": {"end_at": {"gte": now}}},
                ], "minimum_should_match": 1}}
            ],
        }
    }


def list_banners(client, banner_type=None, active_only=True):
    must = []
    if banner_type:
        must.append({"term": {"type": banner_type}})
    if active_only:
        must.append(_banner_active_filter())
    query = {"bool": {"must": must}} if must else {"match_all": {}}
    resp = client.search(
        index=BANNER_INDEX,
        query=query,
        size=100,
        sort=[{"sort_order": {"order": "asc"}}],
    )
    return [normalize_hit(hit) for hit in resp["hits"]["hits"]]


# ---------------------------------------------------------------------------
# Product counts (aggregated from the products index)
# ---------------------------------------------------------------------------


def get_product_counts(client):
    resp = client.search(
        index=PRODUCT_INDEX,
        size=0,
        aggs={
            "categories": {"terms": {"field": "category_name", "size": 500}},
            "brands": {"terms": {"field": "brand_name", "size": 500}},
        },
    )
    aggs = resp["aggregations"]
    return {
        "categories": {
            b["key"]: b["doc_count"] for b in aggs["categories"]["buckets"]
        },
        "brands": {b["key"]: b["doc_count"] for b in aggs["brands"]["buckets"]},
    }


def with_counts(client, items, kind):
    """Attach product_count to each item based on its name."""
    counts = get_product_counts(client)
    buckets = counts.get(kind, {})
    for item in items:
        item["product_count"] = buckets.get(item.get("name", ""), 0)
    return items


def client():
    return get_elasticsearch_client()
