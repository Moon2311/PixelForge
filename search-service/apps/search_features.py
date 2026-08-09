"""Elasticsearch search features for the product catalog.

Provides full-text search, fuzzy matching, phrase matching, autocomplete,
filtering, sorting, pagination, and aggregations/facets.
"""

import logging
from typing import Any, Optional

from elasticsearch import Elasticsearch
from elasticsearch_dsl import Q, Search

from apps.search_indices import PRODUCT_ALIAS, CATEGORY_ALIAS, BRAND_ALIAS

logger = logging.getLogger("catalog.search")


# ---------------------------------------------------------------------------
# Sort field mapping
# ---------------------------------------------------------------------------

SORT_FIELDS = {
    "name": "name.keyword",
    "-name": "-name.keyword",
    "price": "price.regular",
    "-price": "-price.regular",
    "rating": "rating",
    "-rating": "-rating",
    "review_count": "review_count",
    "-review_count": "-review_count",
    "created_at": "created_at",
    "-created_at": "-created_at",
    "relevance": "_score",
    "-relevance": "-_score",
}


# ---------------------------------------------------------------------------
# Product search
# ---------------------------------------------------------------------------


class ProductSearch:
    """Full-featured product search with filtering, sorting, and facets."""

    def __init__(self, client: Elasticsearch):
        self.client = client

    def search(
        self,
        query: Optional[str] = None,
        filters: Optional[dict] = None,
        sort: str = "-relevance",
        page: int = 1,
        page_size: int = 20,
        facets: bool = True,
        fuzzy: bool = True,
    ) -> dict[str, Any]:
        """Execute a product search.

        Args:
            query: Search query string
            filters: Filter criteria (category, brand, price, etc.)
            sort: Sort field (default: relevance)
            page: Page number (1-indexed)
            page_size: Results per page
            facets: Whether to include aggregations
            fuzzy: Whether to enable fuzzy matching

        Returns:
            Dict with hits, total, facets, and pagination info
        """
        filters = filters or {}
        must_clauses = []
        filter_clauses = []

        # Build query
        if query:
            must_clauses.append(self._build_query_clause(query, fuzzy))
        else:
            must_clauses.append({"match_all": {}})

        # Build filters
        filter_clauses.extend(self._build_filter_clauses(filters))

        # Build the bool query
        bool_query = {
            "bool": {
                "must": must_clauses,
                "filter": filter_clauses,
            }
        }

        # Calculate pagination
        from_offset = (page - 1) * page_size

        # Build search body
        body = {
            "query": bool_query,
            "from": from_offset,
            "size": page_size,
            "sort": self._build_sort_clause(sort),
        }

        # Add aggregations
        if facets:
            body["aggs"] = self._build_aggregations()

        # Execute search
        response = self.client.search(index=PRODUCT_ALIAS, body=body)

        # Process results
        hits = response["hits"]
        total = hits["total"]["value"]
        results = [self._format_hit(hit) for hit in hits["hits"]]

        # Process aggregations
        facets_result = {}
        if facets and "aggregations" in response:
            facets_result = self._process_aggregations(response["aggregations"])

        return {
            "results": results,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
            "facets": facets_result,
        }

    def _build_query_clause(self, query: str, fuzzy: bool = True) -> dict:
        """Build the main query clause with full-text, fuzzy, and phrase matching."""
        # Multi-match across key fields
        must = [
            {
                "multi_match": {
                    "query": query,
                    "fields": [
                        "name^3",
                        "name.autocomplete^2",
                        "sku^2",
                        "description",
                        "short_description",
                        "brand.name",
                        "category.name",
                        "subcategory.name",
                        "specifications.value",
                        "tags",
                    ],
                    "type": "best_fields",
                    "fuzzy_transpositions": fuzzy,
                    "prefix_length": 0,
                    "max_expansions": 50,
                }
            }
        ]

        # Add phrase match for exact phrases
        should = [
            {
                "match_phrase": {
                    "name": {
                        "query": query,
                        "boost": 2.0,
                    }
                }
            },
            {
                "match_phrase": {
                    "sku": {
                        "query": query,
                        "boost": 1.5,
                    }
                }
            },
        ]

        return {
            "bool": {
                "must": must,
                "should": should,
                "minimum_should_match": 0,
            }
        }

    def _build_filter_clauses(self, filters: dict) -> list:
        """Build filter clauses from filter criteria."""
        clauses = []

        # Status filter - only show active products by default
        status = filters.get("status", "active")
        clauses.append({"term": {"status": status}})

        # Category filter
        category_id = filters.get("category_id")
        if category_id:
            clauses.append({"term": {"category.id": category_id}})

        category_slug = filters.get("category_slug")
        if category_slug:
            clauses.append({"term": {"category.slug": category_slug}})

        # Subcategory filter
        subcategory_id = filters.get("subcategory_id")
        if subcategory_id:
            clauses.append({"term": {"subcategory.id": subcategory_id}})

        subcategory_slug = filters.get("subcategory_slug")
        if subcategory_slug:
            clauses.append({"term": {"subcategory.slug": subcategory_slug}})

        # Brand filter
        brand_id = filters.get("brand_id")
        if brand_id:
            clauses.append({"term": {"brand.id": brand_id}})

        brand_slug = filters.get("brand_slug")
        if brand_slug:
            clauses.append({"term": {"brand.slug": brand_slug}})

        # Price range filter
        min_price = filters.get("min_price")
        max_price = filters.get("max_price")
        if min_price is not None or max_price is not None:
            price_range = {}
            if min_price is not None:
                price_range["gte"] = float(min_price)
            if max_price is not None:
                price_range["lte"] = float(max_price)
            clauses.append({"range": {"price.regular": price_range}})

        # Availability filter
        in_stock = filters.get("in_stock")
        if in_stock is not None:
            clauses.append({"term": {"availability.in_stock": in_stock}})

        # Featured filter
        is_featured = filters.get("is_featured")
        if is_featured is not None:
            clauses.append({"term": {"is_featured": is_featured}})

        # Rating filter
        min_rating = filters.get("min_rating")
        if min_rating is not None:
            clauses.append({"range": {"rating": {"gte": float(min_rating)}}})

        # Attribute filters (nested)
        attributes = filters.get("attributes", {})
        for attr_slug, values in attributes.items():
            if isinstance(values, str):
                values = [values]
            clauses.append({
                "nested": {
                    "path": "variants.attribute_values",
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"variants.attribute_values.attribute_slug": attr_slug}},
                                {"terms": {"variants.attribute_values.value": values}},
                            ]
                        }
                    }
                }
            })

        # Tag filter
        tags = filters.get("tags")
        if tags:
            if isinstance(tags, str):
                tags = [tags]
            clauses.append({"terms": {"tags": tags}})

        return clauses

    def _build_sort_clause(self, sort: str) -> list:
        """Build sort clause from sort parameter."""
        if sort == "-relevance" or sort == "relevance":
            return ["_score"]

        field = SORT_FIELDS.get(sort, sort.lstrip("-"))
        order = "desc" if sort.startswith("-") else "asc"

        if field.startswith("_"):
            return [{"_score": {"order": order}}]

        return [{field: {"order": order, "missing": "_last"}}]

    def _build_aggregations(self) -> dict:
        """Build aggregation definitions for facets."""
        return {
            "categories": {
                "terms": {
                    "field": "category.id",
                    "size": 50,
                },
                "aggs": {
                    "category_names": {
                        "terms": {
                            "field": "category.name.keyword",
                            "size": 1,
                        }
                    },
                    "category_slugs": {
                        "terms": {
                            "field": "category.slug",
                            "size": 1,
                        }
                    },
                },
            },
            "subcategories": {
                "terms": {
                    "field": "subcategory.id",
                    "size": 100,
                },
                "aggs": {
                    "subcategory_names": {
                        "terms": {
                            "field": "subcategory.name.keyword",
                            "size": 1,
                        }
                    },
                    "subcategory_slugs": {
                        "terms": {
                            "field": "subcategory.slug",
                            "size": 1,
                        }
                    },
                },
            },
            "brands": {
                "terms": {
                    "field": "brand.id",
                    "size": 50,
                },
                "aggs": {
                    "brand_names": {
                        "terms": {
                            "field": "brand.name.keyword",
                            "size": 1,
                        }
                    },
                    "brand_slugs": {
                        "terms": {
                            "field": "brand.slug",
                            "size": 1,
                        }
                    },
                },
            },
            "price_ranges": {
                "range": {
                    "field": "price.regular",
                    "ranges": [
                        {"to": 50},
                        {"from": 50, "to": 100},
                        {"from": 100, "to": 200},
                        {"from": 200, "to": 500},
                        {"from": 500},
                    ],
                },
            },
            "ratings": {
                "range": {
                    "field": "rating",
                    "ranges": [
                        {"from": 4},
                        {"from": 3},
                        {"from": 2},
                        {"from": 1},
                    ],
                },
            },
            "availability": {
                "filters": {
                    "filters": {
                        "in_stock": {"term": {"availability.in_stock": True}},
                        "out_of_stock": {"term": {"availability.in_stock": False}},
                    }
                },
            },
        }

    def _process_aggregations(self, aggs: dict) -> dict:
        """Process aggregation results into facets format."""
        facets = {}

        # Process category facet
        if "categories" in aggs:
            categories = []
            for bucket in aggs["categories"]["buckets"]:
                cat_id = bucket["key"]
                cat_name = ""
                cat_slug = ""
                if "category_names" in bucket and bucket["category_names"]["buckets"]:
                    cat_name = bucket["category_names"]["buckets"][0]["key"]
                if "category_slugs" in bucket and bucket["category_slugs"]["buckets"]:
                    cat_slug = bucket["category_slugs"]["buckets"][0]["key"]
                categories.append({
                    "id": cat_id,
                    "name": cat_name,
                    "slug": cat_slug,
                    "count": bucket["doc_count"],
                })
            facets["categories"] = categories

        # Process subcategory facet
        if "subcategories" in aggs:
            subcategories = []
            for bucket in aggs["subcategories"]["buckets"]:
                sub_id = bucket["key"]
                sub_name = ""
                sub_slug = ""
                if "subcategory_names" in bucket and bucket["subcategory_names"]["buckets"]:
                    sub_name = bucket["subcategory_names"]["buckets"][0]["key"]
                if "subcategory_slugs" in bucket and bucket["subcategory_slugs"]["buckets"]:
                    sub_slug = bucket["subcategory_slugs"]["buckets"][0]["key"]
                subcategories.append({
                    "id": sub_id,
                    "name": sub_name,
                    "slug": sub_slug,
                    "count": bucket["doc_count"],
                })
            facets["subcategories"] = subcategories

        # Process brand facet
        if "brands" in aggs:
            brands = []
            for bucket in aggs["brands"]["buckets"]:
                brand_id = bucket["key"]
                brand_name = ""
                brand_slug = ""
                if "brand_names" in bucket and bucket["brand_names"]["buckets"]:
                    brand_name = bucket["brand_names"]["buckets"][0]["key"]
                if "brand_slugs" in bucket and bucket["brand_slugs"]["buckets"]:
                    brand_slug = bucket["brand_slugs"]["buckets"][0]["key"]
                brands.append({
                    "id": brand_id,
                    "name": brand_name,
                    "slug": brand_slug,
                    "count": bucket["doc_count"],
                })
            facets["brands"] = brands

        # Process price ranges
        if "price_ranges" in aggs:
            price_ranges = []
            for bucket in aggs["price_ranges"]["buckets"]:
                label = bucket.get("key", "")
                price_ranges.append({
                    "label": label,
                    "count": bucket["doc_count"],
                })
            facets["price_ranges"] = price_ranges

        # Process ratings
        if "ratings" in aggs:
            ratings = []
            for bucket in aggs["ratings"]["buckets"]:
                ratings.append({
                    "min_rating": bucket.get("from", 0),
                    "count": bucket["doc_count"],
                })
            facets["ratings"] = ratings

        # Process availability
        if "availability" in aggs:
            avail_filters = aggs["availability"]["filters"]["buckets"]
            facets["availability"] = {
                "in_stock": avail_filters.get("in_stock", {}).get("doc_count", 0),
                "out_of_stock": avail_filters.get("out_of_stock", {}).get("doc_count", 0),
            }

        return facets

    def _format_hit(self, hit: dict) -> dict:
        """Format an ES hit into a response document."""
        source = hit.get("_source", {})
        return {
            "id": source.get("product_id"),
            "score": hit.get("_score"),
            **source,
        }


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------


class ProductAutocomplete:
    """Autocomplete suggestions for products."""

    def __init__(self, client: Elasticsearch):
        self.client = client

    def suggest(self, prefix: str, size: int = 10) -> list[dict]:
        """Get autocomplete suggestions for a prefix.

        Args:
            prefix: The search prefix
            size: Maximum number of suggestions

        Returns:
            List of suggestion dicts with name, slug, and score
        """
        body = {
            "suggest": {
                "product_suggest": {
                    "prefix": prefix,
                    "completion": {
                        "field": "name.autocomplete",
                        "size": size,
                        "fuzzy": {
                            "fuzziness": "AUTO",
                        },
                    },
                },
            }
        }

        # Also do a prefix query for name
        prefix_query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"status": "active"}},
                    ],
                    "should": [
                        {
                            "match_phrase_prefix": {
                                "name": {
                                    "query": prefix,
                                    "max_expansions": 10,
                                }
                            }
                        },
                        {
                            "wildcard": {
                                "name.keyword": {
                                    "value": f"*{prefix}*",
                                    "case_insensitive": True,
                                }
                            }
                        },
                    ],
                }
            },
            "size": size,
            "_source": ["product_id", "name", "slug", "primary_image"],
        }

        response = self.client.search(index=PRODUCT_ALIAS, body=prefix_query)

        suggestions = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            suggestions.append({
                "id": source.get("product_id"),
                "name": source.get("name"),
                "slug": source.get("slug"),
                "image": source.get("primary_image"),
                "score": hit.get("_score"),
            })

        return suggestions


# ---------------------------------------------------------------------------
# Category search
# ---------------------------------------------------------------------------


class CategorySearch:
    """Search and autocomplete for categories."""

    def __init__(self, client: Elasticsearch):
        self.client = client

    def search(self, query: str = None, size: int = 50) -> list[dict]:
        """Search categories."""
        if query:
            body = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"is_active": True}},
                        ],
                        "should": [
                            {
                                "multi_match": {
                                    "query": query,
                                    "fields": ["name^2", "description"],
                                    "fuzzy_transpositions": True,
                                }
                            }
                        ],
                    }
                },
                "size": size,
                "sort": [{"display_order": "asc"}, {"name.keyword": "asc"}],
            }
        else:
            body = {
                "query": {"term": {"is_active": True}},
                "size": size,
                "sort": [{"display_order": "asc"}, {"name.keyword": "asc"}],
            }

        response = self.client.search(index=CATEGORY_ALIAS, body=body)

        return [
            {
                "id": hit["_source"].get("category_id"),
                **hit["_source"],
            }
            for hit in response["hits"]["hits"]
        ]


# ---------------------------------------------------------------------------
# Brand search
# ---------------------------------------------------------------------------


class BrandSearch:
    """Search and autocomplete for brands."""

    def __init__(self, client: Elasticsearch):
        self.client = client

    def search(self, query: str = None, size: int = 50) -> list[dict]:
        """Search brands."""
        if query:
            body = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"is_active": True}},
                        ],
                        "should": [
                            {
                                "multi_match": {
                                    "query": query,
                                    "fields": ["name^2", "description"],
                                    "fuzzy_transpositions": True,
                                }
                            }
                        ],
                    }
                },
                "size": size,
                "sort": [{"name.keyword": "asc"}],
            }
        else:
            body = {
                "query": {"term": {"is_active": True}},
                "size": size,
                "sort": [{"name.keyword": "asc"}],
            }

        response = self.client.search(index=BRAND_ALIAS, body=body)

        return [
            {
                "id": hit["_source"].get("brand_id"),
                **hit["_source"],
            }
            for hit in response["hits"]["hits"]
        ]
