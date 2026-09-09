import re
from datetime import datetime, timezone

from django.conf import settings
from elasticsearch import NotFoundError

from apps.elasticsearch import get_elasticsearch_client

PRODUCT_INDEX = "products"

SORT_FIELDS = {
    "name": "name.keyword",
    "brand": "brand_name.keyword",
    "category": "category_name.keyword",
    "sku": "sku",
    "price": "price",
    "stock_quantity": "stock_quantity",
    "total_sales": "total_sales",
    "rating": "rating",
    "created_at": "created_at",
    "updated_at": "updated_at",
}

DEFAULT_SORT = [{"updated_at": {"order": "desc", "missing": "_last"}}]


class ProductDoesNotExist(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def now_string():
    return _now()


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "product"


def _escape_wildcard(value):
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")


def _wildcard_query(field, value):
    return {
        "wildcard": {
            field: {"value": f"*{_escape_wildcard(value)}*", "case_insensitive": True}
        }
    }


def ensure_mapping(client):
    """Add admin-only fields to the products mapping (idempotent)."""
    client.indices.put_mapping(
        index=PRODUCT_INDEX,
        properties={
            "cost_price": {"type": "float"},
            "min_stock_alert": {"type": "integer"},
            "total_sales": {"type": "integer"},
            "flash_sale": {"type": "boolean"},
            "flash_sale_price": {"type": "float"},
            "flash_sale_ends_at": {"type": "date"},
        },
    )


def normalize_hit(hit):
    doc = {"id": hit["_id"], **hit["_source"]}
    # Flatten nested images to URL list for backward compatibility
    nested_images = doc.get("images")
    if isinstance(nested_images, list) and nested_images and isinstance(nested_images[0], dict):
        doc["images"] = [img.get("url", "") for img in nested_images]
    return doc


def get_product(client, product_id):
    try:
        resp = client.get(index=PRODUCT_INDEX, id=product_id)
    except NotFoundError:
        raise ProductDoesNotExist(f"Product {product_id} not found") from None
    return {"id": resp["_id"], **resp["_source"]}


def product_exists(client, product_id):
    return client.exists(index=PRODUCT_INDEX, id=product_id)


def get_product_by_sku(client, sku, exclude_id=None):
    """Return the first product whose sku matches, or None."""
    query = {"term": {"sku": sku}}
    if exclude_id is not None:
        query = {"bool": {"must": [query], "must_not": [{"term": {"id": exclude_id}}]}}
    resp = client.search(index=PRODUCT_INDEX, query=query, size=1)
    if not resp["hits"]["hits"]:
        return None
    return normalize_hit(resp["hits"]["hits"][0])


def next_product_id(client):
    resp = client.search(
        index=PRODUCT_INDEX, size=0, aggs={"max_id": {"max": {"field": "id"}}}
    )
    max_id = resp["aggregations"]["max_id"]["value"]
    return int(max_id) + 1 if max_id else 1


def build_document(product_id, validated, existing=None, images=None, thumbnail=None):
    """Assemble a full ES document from validated data."""
    now = _now()
    existing = existing or {}
    images = images if images is not None else existing.get("images") or []
    thumbnail = thumbnail or (images[0] if images else "")

    if "tags" in validated:
        tags_raw = validated["tags"]
        tags = (
            [t.strip() for t in tags_raw.split(",") if t.strip()]
            if isinstance(tags_raw, str)
            else list(tags_raw or [])
        )
    else:
        tags = existing.get("tags") or []

    return {
        "id": product_id,
        "name": validated.get("name", existing.get("name", "")),
        "slug": _slugify(validated.get("name", existing.get("name", ""))),
        "description": validated.get("description", existing.get("description", "")),
        "short_description": validated.get(
            "short_description", existing.get("short_description", "")
        ),
        "specifications": validated.get(
            "specifications", existing.get("specifications", "")
        ),
        "category_id": validated.get(
            "category_id", existing.get("category_id", 0)
        ),
        "category_name": validated.get(
            "category_name", existing.get("category_name", "")
        ),
        "brand_id": validated.get("brand_id", existing.get("brand_id", 0)),
        "brand_name": validated.get("brand_name", existing.get("brand_name", "")),
        "sku": validated.get("sku", existing.get("sku", "")),
        "price": float(validated.get("price", existing.get("price", 0))),
        "discount_price": (
            float(validated["discount_price"])
            if "discount_price" in validated
            else existing.get("discount_price")
        ),
        "cost_price": (
            float(validated["cost_price"])
            if "cost_price" in validated
            else existing.get("cost_price")
        ),
        "stock_quantity": int(
            validated.get("stock_quantity", existing.get("stock_quantity", 0))
        ),
        "min_stock_alert": int(
            validated.get("min_stock_alert", existing.get("min_stock_alert", 0))
        ),
        "status": validated.get("status", existing.get("status", "active")),
        "is_featured": bool(
            validated.get("is_featured", existing.get("is_featured", False))
        ),
        "total_sales": int(
            validated.get("total_sales", existing.get("total_sales", 0))
        ),
        "flash_sale": bool(
            validated.get("flash_sale", existing.get("flash_sale", False))
        ),
        "flash_sale_price": (
            float(validated["flash_sale_price"])
            if "flash_sale_price" in validated and validated["flash_sale_price"] is not None
            else existing.get("flash_sale_price")
        ),
        "flash_sale_ends_at": validated.get(
            "flash_sale_ends_at", existing.get("flash_sale_ends_at", "")
        ),
        "color": validated.get("color", existing.get("color", "")),
        "size": validated.get("size", existing.get("size", "")),
        "weight": (
            float(validated["weight"])
            if "weight" in validated
            else existing.get("weight")
        ),
        "tags": tags,
        "images": images,
        "thumbnail": thumbnail,
        "rating": existing.get("rating", 0.0),
        "reviews_count": existing.get("reviews_count", 0),
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }


def create_document(client, doc):
    client.index(index=PRODUCT_INDEX, id=doc["id"], document=doc, refresh="wait_for")
    return doc


def replace_document(client, doc):
    client.index(index=PRODUCT_INDEX, id=doc["id"], document=doc, refresh="wait_for")
    return doc


def delete_product_doc(client, product_id):
    client.delete(index=PRODUCT_INDEX, id=product_id, refresh="wait_for")


def build_admin_query(params):
    must = []

    search = (params.get("search") or "").strip()
    if search:
        should = [
            _wildcard_query("name.keyword", search),
            _wildcard_query("brand_name", search),
            _wildcard_query("sku", search),
        ]
        must.append({"bool": {"should": should, "minimum_should_match": 1}})

    category = (params.get("category") or "").strip()
    if category:
        must.append({"term": {"category_name.keyword": category}})

    brand = (params.get("brand") or "").strip()
    if brand:
        must.append({"term": {"brand_name.keyword": brand}})

    status = (params.get("status") or "").strip()
    if status:
        must.append({"term": {"status": status}})

    stock_status = (params.get("stock_status") or "").strip()
    if stock_status == "in_stock":
        must.append({"range": {"stock_quantity": {"gt": 0}}})
    elif stock_status == "out_of_stock":
        must.append({"range": {"stock_quantity": {"lte": 0}}})
    elif stock_status == "low_stock":
        must.append(
            {
                "script": {
                    "script": {
                        "source": (
                            "def ms = doc['min_stock_alert'];"
                            "if (ms.empty) { return false; }"
                            "return doc['stock_quantity'].value > 0"
                            " && doc['stock_quantity'].value <= ms.value;"
                        )
                    }
                }
            }
        )

    min_price = (params.get("min_price") or "").strip()
    max_price = (params.get("max_price") or "").strip()
    if min_price or max_price:
        range_body = {}
        if min_price:
            try:
                range_body["gte"] = float(min_price)
            except ValueError:
                pass
        if max_price:
            try:
                range_body["lte"] = float(max_price)
            except ValueError:
                pass
        if range_body:
            must.append({"range": {"price": range_body}})

    featured = (params.get("featured") or "").strip().lower()
    if featured in ("1", "true", "yes"):
        must.append({"term": {"is_featured": True}})

    flash_sale = (params.get("flash_sale") or "").strip().lower()
    if flash_sale in ("1", "true", "yes"):
        must.append({"term": {"flash_sale": True}})
        must.append({"range": {"flash_sale_ends_at": {"gte": _now()}}})

    if not must:
        return {"match_all": {}}
    return {"bool": {"must": must}}


def build_sort(params):
    raw = (params.get("sort") or "").strip()
    desc = (params.get("order") or "asc").strip().lower() == "desc"
    if not raw:
        return DEFAULT_SORT
    raw_desc = raw.startswith("-")
    key = raw[1:] if raw_desc else raw
    field = SORT_FIELDS.get(key)
    if field is None:
        return DEFAULT_SORT
    order = "desc" if (raw_desc or desc) else "asc"
    return [{field: {"order": order, "missing": "_last"}}]


def search_products(client, params):
    query = build_admin_query(params)
    sort = build_sort(params)

    page_param = (params.get("page") or "").strip()
    if page_param.isdigit() and int(page_param) > 0:
        page = int(page_param)
        page_size = int((params.get("page_size") or "10").strip() or 10)
        page_size = max(1, min(page_size, 100))
    else:
        page, page_size = None, None

    limit = (params.get("limit") or "").strip()
    size = None
    if page_size:
        size = page_size
    elif limit.isdigit() and int(limit) > 0:
        size = int(limit)
    if size is None:
        size = 1000
    kwargs = {"index": PRODUCT_INDEX, "query": query, "sort": sort, "size": size}
    if page is not None:
        kwargs["from_"] = (page - 1) * page_size

    resp = client.search(**kwargs)
    hits = resp["hits"]["hits"]
    results = [normalize_hit(hit) for hit in hits]

    total = resp["hits"]["total"]["value"]
    if page is None:
        return total, results, None

    total_pages = (total + page_size - 1) // page_size if total else 0
    pagination = {
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1,
    }
    return total, results, pagination


def get_filter_options(client):
    """Return distinct categories and brands for filter dropdowns."""
    resp = client.search(
        index=PRODUCT_INDEX,
        size=0,
        aggs={
            "categories": {"terms": {"field": "category_name.keyword", "size": 100}},
            "brands": {"terms": {"field": "brand_name.keyword", "size": 100}},
        },
    )
    aggs = resp["aggregations"]
    categories = [b["key"] for b in aggs["categories"]["buckets"]]
    brands = [b["key"] for b in aggs["brands"]["buckets"]]
    return {"categories": categories, "brands": brands}


def client():
    return get_elasticsearch_client()
