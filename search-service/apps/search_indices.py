"""Elasticsearch index definitions with versioning and aliases.

Provides versioned index mappings for products, categories, and brands.
Each index version has an alias that points to the current version,
enabling zero-downtime index migrations.

Usage:
    from apps.search_indices import SearchIndices

    indices = SearchIndices(client)
    indices.create_all()       # Create versioned indices + aliases
    indices.switch_alias()     # Switch alias to new version
    indices.reindex_all()      # Reindex from PG into new index
"""

import logging
from typing import Any

from django.conf import settings
from elasticsearch import Elasticsearch

logger = logging.getLogger("catalog.search")


# ---------------------------------------------------------------------------
# Index version definitions
# ---------------------------------------------------------------------------

PRODUCT_INDEX_V1 = "products-v1"
CATEGORY_INDEX_V1 = "categories-v1"
BRAND_INDEX_V1 = "brands-v1"

PRODUCT_ALIAS = "products"
CATEGORY_ALIAS = "categories"
BRAND_ALIAS = "brands"


# ---------------------------------------------------------------------------
# Product mapping (denormalized for search)
# ---------------------------------------------------------------------------

PRODUCT_MAPPING: dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "product_analyzer": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": [
                        "lowercase",
                        "asciifolding",
                        "product_synonym",
                    ],
                },
                "autocomplete_analyzer": {
                    "type": "custom",
                    "tokenizer": "edge_ngram_tokenizer",
                    "filter": [
                        "lowercase",
                        "asciifolding",
                    ],
                },
                "search_analyzer": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": [
                        "lowercase",
                        "asciifolding",
                    ],
                },
            },
            "tokenizer": {
                "edge_ngram_tokenizer": {
                    "type": "edge_ngram",
                    "min_gram": 2,
                    "max_gram": 20,
                    "token_chars": ["letter", "digit"],
                },
            },
            "filter": {
                "product_synonym": {
                    "type": "synonym",
                    "synonyms": [],
                },
            },
        },
    },
    "mappings": {
        "properties": {
            "product_id": {"type": "long"},
            "sku": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "name": {
                "type": "text",
                "analyzer": "product_analyzer",
                "fields": {
                    "keyword": {"type": "keyword"},
                    "autocomplete": {
                        "type": "text",
                        "analyzer": "autocomplete_analyzer",
                        "search_analyzer": "search_analyzer",
                    },
                },
            },
            "slug": {"type": "keyword"},
            "description": {
                "type": "text",
                "analyzer": "product_analyzer",
            },
            "short_description": {
                "type": "text",
                "analyzer": "product_analyzer",
            },
            "status": {
                "type": "keyword",
            },
            "is_featured": {"type": "boolean"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "brand": {
                "type": "object",
                "properties": {
                    "id": {"type": "long"},
                    "name": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "slug": {"type": "keyword"},
                    "logo": {"type": "keyword", "index": False},
                },
            },
            "category": {
                "type": "object",
                "properties": {
                    "id": {"type": "long"},
                    "name": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "slug": {"type": "keyword"},
                },
            },
            "subcategory": {
                "type": "object",
                "properties": {
                    "id": {"type": "long"},
                    "name": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "slug": {"type": "keyword"},
                },
            },
            "variants": {
                "type": "nested",
                "properties": {
                    "variant_id": {"type": "long"},
                    "sku": {"type": "keyword"},
                    "barcode": {"type": "keyword"},
                    "name": {"type": "text"},
                    "status": {"type": "keyword"},
                    "price": {
                        "type": "object",
                        "properties": {
                            "regular": {"type": "scaled_float", "scaling_factor": 100},
                            "sale": {"type": "scaled_float", "scaling_factor": 100},
                            "currency": {"type": "keyword"},
                        },
                    },
                    "inventory": {
                        "type": "object",
                        "properties": {
                            "stock_quantity": {"type": "integer"},
                            "reserved_quantity": {"type": "integer"},
                            "available_quantity": {"type": "integer"},
                            "in_stock": {"type": "boolean"},
                        },
                    },
                    "attribute_values": {
                        "type": "nested",
                        "properties": {
                            "attribute_name": {"type": "keyword"},
                            "attribute_slug": {"type": "keyword"},
                            "value": {"type": "keyword"},
                        },
                    },
                },
            },
            "attributes": {
                "type": "nested",
                "properties": {
                    "name": {"type": "keyword"},
                    "slug": {"type": "keyword"},
                    "values": {"type": "keyword"},
                },
            },
            "specifications": {
                "type": "nested",
                "properties": {
                    "name": {"type": "keyword"},
                    "value": {"type": "text"},
                    "unit": {"type": "keyword"},
                },
            },
            "images": {
                "type": "nested",
                "properties": {
                    "image_id": {"type": "long"},
                    "url": {"type": "keyword", "index": False},
                    "alt_text": {"type": "text"},
                    "is_primary": {"type": "boolean"},
                    "display_order": {"type": "integer"},
                },
            },
            "primary_image": {
                "type": "keyword",
                "index": False,
            },
            "price": {
                "type": "object",
                "properties": {
                    "regular": {"type": "scaled_float", "scaling_factor": 100},
                    "min": {"type": "scaled_float", "scaling_factor": 100},
                    "max": {"type": "scaled_float", "scaling_factor": 100},
                    "sale": {"type": "scaled_float", "scaling_factor": 100},
                    "currency": {"type": "keyword"},
                },
            },
            "rating": {
                "type": "float",
            },
            "review_count": {
                "type": "integer",
            },
            "availability": {
                "type": "object",
                "properties": {
                    "in_stock": {"type": "boolean"},
                    "quantity": {"type": "integer"},
                },
            },
            "tags": {
                "type": "keyword",
            },
            "color": {
                "type": "keyword",
            },
            "size": {
                "type": "keyword",
            },
            "weight": {
                "type": "scaled_float",
                "scaling_factor": 100,
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------

CATEGORY_MAPPING: dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "category_analyzer": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["lowercase", "asciifolding"],
                },
            },
        },
    },
    "mappings": {
        "properties": {
            "category_id": {"type": "long"},
            "name": {
                "type": "text",
                "analyzer": "category_analyzer",
                "fields": {
                    "keyword": {"type": "keyword"},
                    "autocomplete": {
                        "type": "text",
                        "analyzer": "autocomplete_analyzer",
                        "search_analyzer": "search_analyzer",
                    },
                },
            },
            "slug": {"type": "keyword"},
            "description": {"type": "text"},
            "image": {"type": "keyword", "index": False},
            "is_active": {"type": "boolean"},
            "display_order": {"type": "integer"},
            "product_count": {"type": "integer"},
            "subcategories": {
                "type": "nested",
                "properties": {
                    "subcategory_id": {"type": "long"},
                    "name": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "slug": {"type": "keyword"},
                    "is_active": {"type": "boolean"},
                    "display_order": {"type": "integer"},
                    "product_count": {"type": "integer"},
                },
            },
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        },
    },
}


# ---------------------------------------------------------------------------
# Brand mapping
# ---------------------------------------------------------------------------

BRAND_MAPPING: dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "brand_analyzer": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["lowercase", "asciifolding"],
                },
            },
        },
    },
    "mappings": {
        "properties": {
            "brand_id": {"type": "long"},
            "name": {
                "type": "text",
                "analyzer": "brand_analyzer",
                "fields": {
                    "keyword": {"type": "keyword"},
                    "autocomplete": {
                        "type": "text",
                        "analyzer": "autocomplete_analyzer",
                        "search_analyzer": "search_analyzer",
                    },
                },
            },
            "slug": {"type": "keyword"},
            "description": {"type": "text"},
            "logo": {"type": "keyword", "index": False},
            "website": {"type": "keyword", "index": False},
            "is_active": {"type": "boolean"},
            "product_count": {"type": "integer"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        },
    },
}


# ---------------------------------------------------------------------------
# Index management class
# ---------------------------------------------------------------------------


class SearchIndices:
    """Manages versioned Elasticsearch indices with aliases."""

    def __init__(self, client: Elasticsearch):
        self.client = client

    # ------------------------------------------------------------------
    # Index creation
    # ------------------------------------------------------------------

    def create_all(self):
        """Create all versioned indices with aliases."""
        self._create_index(PRODUCT_INDEX_V1, PRODUCT_MAPPING, PRODUCT_ALIAS)
        self._create_index(CATEGORY_INDEX_V1, CATEGORY_MAPPING, CATEGORY_ALIAS)
        self._create_index(BRAND_INDEX_V1, BRAND_MAPPING, BRAND_ALIAS)

    def _create_index(self, index_name: str, mapping: dict, alias: str):
        """Create a versioned index with alias if it doesn't exist."""
        if not self.client.indices.exists(index=index_name):
            self.client.indices.create(index=index_name, body=mapping)
            logger.info(f"Created index: {index_name}")

            # Create alias
            self.client.indices.put_alias(index=index_name, name=alias)
            logger.info(f"Created alias: {alias} -> {index_name}")
        else:
            logger.info(f"Index already exists: {index_name}")

    # ------------------------------------------------------------------
    # Alias management
    # ------------------------------------------------------------------

    def switch_alias(self):
        """Switch aliases to point to the latest versioned indices."""
        self._switch_alias(PRODUCT_INDEX_V1, PRODUCT_ALIAS)
        self._switch_alias(CATEGORY_INDEX_V1, CATEGORY_ALIAS)
        self._switch_alias(BRAND_INDEX_V1, BRAND_ALIAS)

    def _switch_alias(self, new_index: str, alias: str):
        """Switch alias to point to new_index, removing old index from alias."""
        try:
            old_indices = self.client.indices.get_alias(name=alias)
            actions = []

            # Remove old indices from alias
            for old_index in old_indices:
                if old_index != new_index:
                    actions.append({"remove": {"index": old_index, "alias": alias}})

            # Add new index to alias
            actions.append({"add": {"index": new_index, "alias": alias}})

            if actions:
                self.client.indices.update_aliases(body={"actions": actions})
                logger.info(f"Switched alias {alias} -> {new_index}")
        except Exception:
            # Alias doesn't exist yet, just create it
            self.client.indices.put_alias(index=new_index, name=alias)
            logger.info(f"Created alias: {alias} -> {new_index}")

    # ------------------------------------------------------------------
    # Reindexing
    # ------------------------------------------------------------------

    def reindex_all(self):
        """Reindex all data from PostgreSQL into Elasticsearch."""
        from apps.search_document_builder import SearchDocumentBuilder

        builder = SearchDocumentBuilder(self.client)

        logger.info("Starting full reindex...")

        # Reindex categories
        count = builder.reindex_categories()
        logger.info(f"Reindexed {count} categories")

        # Reindex brands
        count = builder.reindex_brands()
        logger.info(f"Reindexed {count} brands")

        # Reindex products
        count = builder.reindex_products()
        logger.info(f"Reindexed {count} products")

        logger.info("Full reindex complete")

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        """Verify index health and document counts."""
        result = {}

        for name, alias in [
            ("products", PRODUCT_ALIAS),
            ("categories", CATEGORY_ALIAS),
            ("brands", BRAND_ALIAS),
        ]:
            try:
                count = self.client.count(index=alias)["count"]
                health = self.client.cluster.health()
                result[name] = {
                    "count": count,
                    "status": "ok",
                    "cluster_status": health["status"],
                }
            except Exception as e:
                result[name] = {
                    "count": 0,
                    "status": "error",
                    "error": str(e),
                }

        return result

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def delete_old_indices(self, keep_versions: int = 1):
        """Delete old versioned indices, keeping the latest N versions."""
        index_groups = {
            "products": PRODUCT_INDEX_V1,
            "categories": CATEGORY_INDEX_V1,
            "brands": BRAND_INDEX_V1,
        }

        for prefix, latest in index_groups.items():
            # Find all indices matching the prefix pattern
            all_indices = list(self.client.indices.get(index=f"{prefix}-v*").keys())
            all_indices.sort()

            # Keep the latest N
            to_delete = all_indices[:-keep_versions] if keep_versions > 0 else all_indices

            for index in to_delete:
                self.client.indices.delete(index=index)
                logger.info(f"Deleted old index: {index}")
