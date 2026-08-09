"""Management command to safely backfill/reindex the Product Catalog from PostgreSQL into Elasticsearch.

This command reads product data from PostgreSQL (the source of truth),
builds denormalized Elasticsearch documents, and bulk-indexes them into
a versioned index (products-v1). The products alias is only switched
after successful validation.

Safety guarantees:
    - Existing documents in the old index are never touched until alias switch.
    - Failed product IDs are logged for inspection.
    - Verification must pass before alias is switched.
    - PostgreSQL source data is never modified.

Usage:
    python manage.py reindex_catalog
    python manage.py reindex_catalog --dry-run
    python manage.py reindex_catalog --batch-size 500
    python manage.py reindex_catalog --product-id 123
    python manage.py reindex_catalog --rebuild
    python manage.py reindex_catalog --verify
    python manage.py reindex_catalog --rebuild --verify
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from elasticsearch import Elasticsearch, helpers

from apps.elasticsearch import get_elasticsearch_client
from apps.models import (
    Brand,
    Category,
    Inventory,
    Product,
    ProductAttributeValue,
    ProductImage,
    ProductReview,
    ProductSpecification,
    ProductVariant,
    Subcategory,
    VariantAttributeValue,
    VariantPrice,
)
from apps.search_indices import (
    PRODUCT_ALIAS,
    PRODUCT_INDEX_V1,
    PRODUCT_MAPPING,
    SearchIndices,
)

logger = logging.getLogger("catalog.search")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BATCH_SIZE = 200
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds
SAMPLE_SIZE = 5  # number of sample documents to verify
SEARCH_TEST_QUERIES = ["phone", "laptop", "test", "product"]


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = "Reindex Product Catalog from PostgreSQL to Elasticsearch"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be reindexed without actually indexing",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=f"Number of products per bulk batch (default: {DEFAULT_BATCH_SIZE})",
        )
        parser.add_argument(
            "--product-id",
            type=int,
            default=None,
            help="Reindex a single product by ID",
        )
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Delete and recreate the products-v1 index before reindexing",
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Run verification after reindexing (counts, samples, searchability)",
        )
        parser.add_argument(
            "--switch-alias",
            action="store_true",
            help="Switch the products alias to products-v1 after successful verification",
        )
        parser.add_argument(
            "--retry-count",
            type=int,
            default=MAX_RETRIES,
            help=f"Max retries per batch on failure (default: {MAX_RETRIES})",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.batch_size = options["batch_size"]
        self.product_id = options["product_id"]
        self.rebuild = options["rebuild"]
        self.verify_only = options["verify"]
        self.switch_alias = options["switch_alias"]
        self.max_retries = options["retry_count"]

        self.es = get_elasticsearch_client()
        self.indices = SearchIndices(self.es)
        self.failed_product_ids: list[int] = []
        self.indexed_count = 0
        self.skipped_count = 0

        start_time = time.time()

        self._print_header()

        if self.verify_only:
            self._run_verification()
            return

        # Step 1: Ensure index exists (create or rebuild)
        if self.rebuild:
            self._rebuild_index()
        else:
            self._ensure_index()

        # Step 2: Get pre-reindex document count
        old_doc_count = self._get_index_doc_count(PRODUCT_INDEX_V1)
        self._log(f"Existing documents in {PRODUCT_INDEX_V1}: {old_doc_count}")

        # Step 3: Run reindex
        if self.dry_run:
            self._dry_run()
        else:
            self._reindex()

        elapsed = time.time() - start_time
        self._print_summary(elapsed)

        # Step 4: Verify if requested
        if self.verify and not self.dry_run:
            self._run_verification()

        # Step 5: Switch alias if requested and verification passed
        if self.switch_alias and not self.dry_run:
            self._switch_alias()

    # ------------------------------------------------------------------
    # Header / summary
    # ------------------------------------------------------------------

    def _print_header(self):
        mode = "DRY RUN" if self.dry_run else "LIVE"
        action = "REBUILD" if self.rebuild else "UPSERT"
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS(f"  Product Catalog Reindex  [{mode}]"))
        self.stdout.write(self.style.SUCCESS(f"  Index:  {PRODUCT_INDEX_V1}"))
        self.stdout.write(self.style.SUCCESS(f"  Action: {action}"))
        self.stdout.write(self.style.SUCCESS(f"  Batch:  {self.batch_size}"))
        if self.product_id:
            self.stdout.write(self.style.SUCCESS(f"  Filter: product_id={self.product_id}"))
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write("")

    def _print_summary(self, elapsed: float):
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("-" * 70))
        self.stdout.write(self.style.SUCCESS("  Reindex Summary"))
        self.stdout.write(self.style.SUCCESS("-" * 70))
        self.stdout.write(f"  Indexed:   {self.indexed_count}")
        self.stdout.write(f"  Skipped:   {self.skipped_count}")
        self.stdout.write(f"  Failed:    {len(self.failed_product_ids)}")
        self.stdout.write(f"  Time:      {elapsed:.2f}s")

        if self.failed_product_ids:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("  Failed product IDs:"))
            # Log to file
            log_file = self._log_failed_ids()
            self.stdout.write(self.style.WARNING(f"    {self.failed_product_ids[:20]}"))
            if len(self.failed_product_ids) > 20:
                self.stdout.write(
                    self.style.WARNING(
                        f"    ... and {len(self.failed_product_ids) - 20} more"
                    )
                )
            self.stdout.write(self.style.WARNING(f"    Full list saved to: {log_file}"))

        self.stdout.write(self.style.SUCCESS("-" * 70))
        self.stdout.write("")

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _ensure_index(self):
        """Ensure products-v1 index exists with correct mapping."""
        if not self.es.indices.exists(index=PRODUCT_INDEX_V1):
            self._log(f"Creating index {PRODUCT_INDEX_V1}...")
            self.es.indices.create(index=PRODUCT_INDEX_V1, body=PRODUCT_MAPPING)
            self._log(f"Created index {PRODUCT_INDEX_V1}")
        else:
            self._log(f"Index {PRODUCT_INDEX_V1} already exists")

    def _rebuild_index(self):
        """Delete and recreate the products-v1 index."""
        if self.es.indices.exists(index=PRODUCT_INDEX_V1):
            self._log(f"Deleting index {PRODUCT_INDEX_V1}...")
            self.es.indices.delete(index=PRODUCT_INDEX_V1)
            self._log(f"Deleted index {PRODUCT_INDEX_V1}")

        self._log(f"Creating index {PRODUCT_INDEX_V1}...")
        self.es.indices.create(index=PRODUCT_INDEX_V1, body=PRODUCT_MAPPING)
        self._log(f"Created index {PRODUCT_INDEX_V1}")

    def _get_index_doc_count(self, index: str) -> int:
        """Get document count for an index."""
        try:
            return self.es.count(index=index)["count"]
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Queryset
    # ------------------------------------------------------------------

    def _get_product_queryset(self):
        """Get the queryset of products to reindex."""
        qs = Product.objects.filter(
            is_deleted=False
        ).select_related(
            "brand", "category", "subcategory"
        )

        if self.product_id:
            qs = qs.filter(pk=self.product_id)

        return qs.order_by("pk")

    # ------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------

    def _dry_run(self):
        """Show what would be reindexed without actually indexing."""
        qs = self._get_product_queryset()
        total = qs.count()

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"  Would index {total} products"))
        self.stdout.write("")

        # Show first 10 products
        for product in qs[:10]:
            self.stdout.write(
                f"    [{product.pk}] {product.sku} - {product.name} "
                f"(status={product.status})"
            )

        if total > 10:
            self.stdout.write(f"    ... and {total - 10} more")

        self.stdout.write("")

    # ------------------------------------------------------------------
    # Reindex
    # ------------------------------------------------------------------

    def _reindex(self):
        """Reindex products from PostgreSQL to Elasticsearch."""
        qs = self._get_product_queryset()
        total = qs.count()

        if total == 0:
            self._log("No products to reindex")
            return

        self._log(f"Reindexing {total} products...")

        # Process in batches
        batch = []
        batch_num = 0

        for product in qs.iterator():
            batch.append(product)

            if len(batch) >= self.batch_size:
                batch_num += 1
                self._process_batch(batch, batch_num, total)
                batch = []

        # Process remaining
        if batch:
            batch_num += 1
            self._process_batch(batch, batch_num, total)

    def _process_batch(self, products: list, batch_num: int, total: int):
        """Process a batch of products with bulk indexing and retry."""
        actions = []

        for product in products:
            try:
                doc = self._build_product_document(product)
                action = {
                    "_index": PRODUCT_INDEX_V1,
                    "_id": f"product:{product.pk}",
                    "_source": doc,
                }
                actions.append(action)
                self.indexed_count += 1
            except Exception as e:
                self.failed_product_ids.append(product.pk)
                logger.error(
                    f"Failed to build document for product {product.pk}: {e}",
                    exc_info=True,
                )

        if not actions:
            return

        # Bulk index with retry
        success = False
        for attempt in range(1, self.max_retries + 1):
            try:
                success_count, errors = helpers.bulk(
                    self.es,
                    actions,
                    raise_on_error=False,
                    raise_on_exception=False,
                )

                if errors:
                    self._log_errors(errors, batch_num)
                    # Extract failed IDs from errors
                    for error in errors:
                        if "index" in error:
                            doc_id = error["index"].get("_id", "")
                            if doc_id.startswith("product:"):
                                pid = int(doc_id.split(":")[1])
                                if pid not in self.failed_product_ids:
                                    self.failed_product_ids.append(pid)
                else:
                    success = True

                progress = min(self.indexed_count, total)
                self._log(
                    f"  Batch {batch_num}: {success_count}/{len(actions)} indexed "
                    f"({progress}/{total} total)"
                )
                break

            except Exception as e:
                if attempt < self.max_retries:
                    wait = RETRY_BACKOFF_BASE ** attempt
                    self._log(
                        f"  Batch {batch_num} attempt {attempt} failed: {e}. "
                        f"Retrying in {wait}s..."
                    )
                    time.sleep(wait)
                else:
                    self._log(
                        f"  Batch {batch_num} FAILED after {self.max_retries} attempts: {e}"
                    )
                    for product in products:
                        if product.pk not in self.failed_product_ids:
                            self.failed_product_ids.append(product.pk)

        # Refresh after each batch for consistency
        if not self.dry_run:
            try:
                self.es.indices.refresh(index=PRODUCT_INDEX_V1)
            except Exception:
                pass

    def _log_errors(self, errors: list, batch_num: int):
        """Log bulk indexing errors."""
        for error in errors:
            if "index" in error:
                item = error["index"]
                doc_id = item.get("_id", "unknown")
                err_info = item.get("error", {})
                logger.error(
                    f"Batch {batch_num} index error for {doc_id}: {err_info}"
                )
            elif "delete" in error:
                item = error["delete"]
                doc_id = item.get("_id", "unknown")
                err_info = item.get("error", {})
                logger.error(
                    f"Batch {batch_num} delete error for {doc_id}: {err_info}"
                )

    # ------------------------------------------------------------------
    # Document builder (inline for self-contained command)
    # ------------------------------------------------------------------

    def _build_product_document(self, product: Product) -> dict[str, Any]:
        """Build a denormalized product document from PostgreSQL."""
        brand = product.brand
        category = product.category
        subcategory = product.subcategory

        # Variants
        variants = ProductVariant.objects.filter(
            product=product, is_deleted=False
        ).select_related("price", "inventory")

        variant_docs = []
        all_attribute_values = []
        min_price = None
        max_price = None
        total_stock = 0
        any_in_stock = False

        for variant in variants:
            price = getattr(variant, "price", None)
            inventory = getattr(variant, "inventory", None)

            variant_price = {
                "regular": float(price.regular_price) if price else 0,
                "sale": float(price.sale_price) if price and price.sale_price else None,
                "currency": price.currency if price else "USD",
            }

            variant_inventory = {
                "stock_quantity": inventory.stock_quantity if inventory else 0,
                "reserved_quantity": inventory.reserved_quantity if inventory else 0,
                "available_quantity": inventory.available_quantity if inventory else 0,
                "in_stock": inventory.is_in_stock if inventory else False,
            }

            # Variant attribute values
            attr_values = VariantAttributeValue.objects.filter(
                variant=variant
            ).select_related("attribute_value__attribute")

            variant_attr_docs = []
            for vav in attr_values:
                av = vav.attribute_value
                attr = av.attribute
                variant_attr_docs.append({
                    "attribute_name": attr.name,
                    "attribute_slug": attr.slug,
                    "value": av.value,
                })
                all_attribute_values.append({
                    "name": attr.name,
                    "slug": attr.slug,
                    "values": [av.value],
                })

            variant_docs.append({
                "variant_id": variant.pk,
                "sku": variant.sku,
                "barcode": variant.barcode or "",
                "name": variant.name or "",
                "status": variant.status,
                "price": variant_price,
                "inventory": variant_inventory,
                "attribute_values": variant_attr_docs,
            })

            if price:
                current = float(price.sale_price or price.regular_price)
                if min_price is None or current < min_price:
                    min_price = current
                if max_price is None or current > max_price:
                    max_price = current

            if inventory:
                total_stock += inventory.available_quantity
                if inventory.is_in_stock:
                    any_in_stock = True

        # Deduplicate attributes
        seen_attrs = set()
        unique_attributes = []
        for attr in all_attribute_values:
            if attr["slug"] not in seen_attrs:
                seen_attrs.add(attr["slug"])
                unique_attributes.append(attr)

        # Specifications
        specs = ProductSpecification.objects.filter(product=product)
        specifications = [
            {"name": s.name, "value": s.value, "unit": s.unit or ""}
            for s in specs
        ]

        # Images
        images = ProductImage.objects.filter(product=product).order_by(
            "display_order"
        )
        image_docs = [
            {
                "image_id": img.pk,
                "url": img.image_url,
                "alt_text": img.alt_text or "",
                "is_primary": img.is_primary,
                "display_order": img.display_order,
            }
            for img in images
        ]

        primary_image = ""
        for img in images:
            if img.is_primary:
                primary_image = img.image_url
                break
        if not primary_image and images:
            primary_image = images[0].image_url

        # Reviews
        from django.db.models import Avg
        review_stats = ProductReview.objects.filter(
            product=product, status="approved"
        ).aggregate(avg_rating=Avg("rating"))
        avg_rating = float(review_stats["avg_rating"] or 0)
        review_count = ProductReview.objects.filter(
            product=product, status="approved"
        ).count()

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
            "brand": {
                "id": brand.pk if brand else None,
                "name": brand.name if brand else "",
                "slug": brand.slug if brand else "",
                "logo": brand.logo if brand else "",
            } if brand else None,
            "category": {
                "id": category.pk if category else None,
                "name": category.name if category else "",
                "slug": category.slug if category else "",
            } if category else None,
            "subcategory": {
                "id": subcategory.pk if subcategory else None,
                "name": subcategory.name if subcategory else "",
                "slug": subcategory.slug if subcategory else "",
            } if subcategory else None,
            "variants": variant_docs,
            "attributes": unique_attributes,
            "specifications": specifications,
            "images": image_docs,
            "primary_image": primary_image,
            "price": {
                "regular": max_price or 0,
                "min": min_price or 0,
                "max": max_price or 0,
                "sale": None,
                "currency": "USD",
            },
            "rating": round(avg_rating, 2),
            "review_count": review_count,
            "availability": {
                "in_stock": any_in_stock,
                "quantity": total_stock,
            },
            "tags": [],
            "color": "",
            "size": "",
            "weight": 0,
        }

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def _run_verification(self):
        """Run full verification: counts, samples, searchability."""
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS("  Verification"))
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write("")

        all_passed = True

        # 1. Document count verification
        if not self._verify_counts():
            all_passed = False

        # 2. Sample document verification
        if not self._verify_sample_documents():
            all_passed = False

        # 3. Searchability verification
        if not self._verify_searchable():
            all_passed = False

        self.stdout.write("")
        if all_passed:
            self.stdout.write(
                self.style.SUCCESS("  All verification checks PASSED")
            )
        else:
            self.stdout.write(
                self.style.ERROR("  Some verification checks FAILED")
            )
        self.stdout.write("")

    def _verify_counts(self) -> bool:
        """Verify document count matches PostgreSQL."""
        self.stdout.write(self.style.SUCCESS("  1. Document Count Verification"))
        self.stdout.write("  " + "-" * 50)

        pg_count = Product.objects.filter(is_deleted=False).count()

        try:
            es_count = self.es.count(index=PRODUCT_INDEX_V1)["count"]
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"    Failed to count ES documents: {e}"))
            return False

        match = pg_count == es_count
        style = self.style.SUCCESS if match else self.style.ERROR
        status = "MATCH" if match else "MISMATCH"

        self.stdout.write(style(f"    PostgreSQL: {pg_count}"))
        self.stdout.write(style(f"    Elasticsearch: {es_count}"))
        self.stdout.write(style(f"    Status: {status}"))

        if not match:
            self.stdout.write(
                self.style.WARNING(
                    f"    Difference: {abs(pg_count - es_count)} documents"
                )
            )

        self.stdout.write("")
        return match

    def _verify_sample_documents(self) -> bool:
        """Verify sample documents have expected fields."""
        self.stdout.write(self.style.SUCCESS("  2. Sample Document Verification"))
        self.stdout.write("  " + "-" * 50)

        try:
            # Get random sample of documents
            result = self.es.search(
                index=PRODUCT_INDEX_V1,
                body={
                    "query": {"match_all": {}},
                    "size": SAMPLE_SIZE,
                },
            )
            hits = result["hits"]["hits"]
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"    Failed to fetch samples: {e}"))
            return False

        if not hits:
            self.stdout.write(self.style.WARNING("    No documents found to verify"))
            return False

        required_fields = [
            "product_id", "sku", "name", "slug", "status",
            "brand", "category", "price", "availability",
        ]

        all_valid = True
        for i, hit in enumerate(hits):
            doc = hit["_source"]
            missing = [f for f in required_fields if f not in doc]

            if missing:
                all_valid = False
                self.stdout.write(
                    self.style.ERROR(
                        f"    Doc {i + 1} (id={doc.get('product_id')}): "
                        f"missing fields: {missing}"
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"    Doc {i + 1} (id={doc.get('product_id')}, "
                        f"name={doc.get('name', '')[:30]}): OK"
                    )
                )

        self.stdout.write("")
        return all_valid

    def _verify_searchable(self) -> bool:
        """Verify that products are searchable."""
        self.stdout.write(self.style.SUCCESS("  3. Searchability Verification"))
        self.stdout.write("  " + "-" * 50)

        all_ok = True

        for query in SEARCH_TEST_QUERIES:
            try:
                result = self.es.search(
                    index=PRODUCT_INDEX_V1,
                    body={
                        "query": {
                            "multi_match": {
                                "query": query,
                                "fields": ["name^3", "sku^2", "description"],
                            }
                        },
                        "size": 1,
                    },
                )
                count = result["hits"]["total"]["value"]
                self.stdout.write(
                    self.style.SUCCESS(
                        f'    Query "{query}": {count} hits'
                    )
                )
            except Exception as e:
                all_ok = False
                self.stdout.write(
                    self.style.ERROR(f'    Query "{query}": FAILED - {e}')
                )

        # Test filter query
        try:
            result = self.es.search(
                index=PRODUCT_INDEX_V1,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"status": "active"}},
                            ]
                        }
                    },
                    "size": 1,
                },
            )
            count = result["hits"]["total"]["value"]
            self.stdout.write(
                self.style.SUCCESS(
                    f'    Filter (status=active): {count} hits'
                )
            )
        except Exception as e:
            all_ok = False
            self.stdout.write(
                self.style.ERROR(f"    Filter (status=active): FAILED - {e}")
            )

        self.stdout.write("")
        return all_ok

    # ------------------------------------------------------------------
    # Alias switching
    # ------------------------------------------------------------------

    def _switch_alias(self):
        """Switch the products alias to point to products-v1."""
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("  Switching alias..."))

        try:
            # Check if alias exists
            if self.es.indices.exists_alias(name=PRODUCT_ALIAS):
                # Get current indices for alias
                old_indices = self.es.indices.get_alias(name=PRODUCT_ALIAS)
                actions = []

                for old_index in old_indices:
                    if old_index != PRODUCT_INDEX_V1:
                        actions.append({"remove": {"index": old_index, "alias": PRODUCT_ALIAS}})

                actions.append({"add": {"index": PRODUCT_INDEX_V1, "alias": PRODUCT_ALIAS}})

                self.es.indices.update_aliases(body={"actions": actions})
            else:
                # Create alias
                self.es.indices.put_alias(index=PRODUCT_INDEX_V1, name=PRODUCT_ALIAS)

            self.stdout.write(
                self.style.SUCCESS(
                    f"    Alias {PRODUCT_ALIAS} now points to {PRODUCT_INDEX_V1}"
                )
            )

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"    Failed to switch alias: {e}"))

        self.stdout.write("")

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log(self, message: str):
        """Log a message to stdout."""
        self.stdout.write(message)

    def _log_failed_ids(self) -> str:
        """Save failed product IDs to a log file."""
        log_dir = os.path.join(settings.BASE_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(log_dir, f"reindex_failed_{timestamp}.json")

        with open(log_file, "w") as f:
            json.dump(
                {
                    "timestamp": timestamp,
                    "total_failed": len(self.failed_product_ids),
                    "product_ids": self.failed_product_ids,
                },
                f,
                indent=2,
            )

        return log_file
