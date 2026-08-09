"""Management command to verify search index health and consistency.

This command verifies that Elasticsearch indices are healthy, have the
expected document counts, and are consistent with PostgreSQL.

Usage:
    python manage.py verify_search
    python manage.py verify_search --detailed
    python manage.py verify_search --compare
"""

import logging

from django.core.management.base import BaseCommand

from apps.elasticsearch import get_elasticsearch_client
from apps.models import Brand, Category, Product
from apps.search_indices import SearchIndices

logger = logging.getLogger("catalog.search")


class Command(BaseCommand):
    help = "Verify search index health and consistency"

    def add_arguments(self, parser):
        parser.add_argument(
            "--detailed",
            action="store_true",
            help="Show detailed index information",
        )
        parser.add_argument(
            "--compare",
            action="store_true",
            help="Compare ES document counts with PostgreSQL",
        )

    def handle(self, *args, **options):
        client = get_elasticsearch_client()
        indices = SearchIndices(client)

        self.stdout.write("\n=== Search Index Health ===\n")

        # Verify indices
        result = indices.verify()

        for name, info in result.items():
            status = info.get("status", "unknown")
            count = info.get("count", 0)
            cluster_status = info.get("cluster_status", "unknown")

            if status == "ok":
                self.stdout.write(
                    self.style.SUCCESS(f"  {name}: {count} documents (OK)")
                )
            else:
                self.stdout.write(
                    self.style.ERROR(f"  {name}: {count} documents ({status})")
                )

        if options["detailed"]:
            self._show_detailed_info(client)

        if options["compare"]:
            self._compare_counts(client)

        # Cluster health
        try:
            health = client.cluster.health()
            self.stdout.write(f"\n=== Cluster Health ===")
            self.stdout.write(f"  Status: {health['status']}")
            self.stdout.write(f"  Nodes: {health['number_of_nodes']}")
            self.stdout.write(f"  Shards: {health['active_shards']}")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Cluster health check failed: {e}"))

    def _show_detailed_info(self, client):
        """Show detailed index information."""
        self.stdout.write("\n=== Detailed Index Info ===")

        for alias in ["products", "categories", "brands"]:
            try:
                # Get index stats
                stats = client.indices.stats(index=alias)
                index_name = list(stats["indices"].keys())[0]
                index_stats = stats["indices"][index_name]

                docs = index_stats.get("primaries", {}).get("docs", {})
                store = index_stats.get("primaries", {}).get("store", {})

                self.stdout.write(f"\n  {alias}:")
                self.stdout.write(f"    Documents: {docs.get('count', 0)}")
                self.stdout.write(f"    Deleted: {docs.get('deleted', 0)}")
                self.stdout.write(
                    f"    Size: {store.get('size_in_bytes', 0) / 1024:.2f} KB"
                )

                # Get mapping info
                mapping = client.indices.get_mapping(index=alias)
                index_name = list(mapping.keys())[0]
                properties = mapping[index_name]["mappings"].get("properties", {})
                self.stdout.write(f"    Fields: {len(properties)}")

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"  Error getting info for {alias}: {e}")
                )

    def _compare_counts(self, client):
        """Compare ES document counts with PostgreSQL."""
        self.stdout.write("\n=== PostgreSQL vs Elasticsearch Counts ===")

        # Products
        pg_count = Product.objects.filter(is_deleted=False).count()
        try:
            es_count = client.count(index="products")["count"]
            match = "OK" if pg_count == es_count else "MISMATCH"
            style = self.style.SUCCESS if match == "OK" else self.style.WARNING
            self.stdout.write(
                style(f"  Products: PG={pg_count}, ES={es_count} [{match}]")
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Products: PG={pg_count}, ES=error ({e})"))

        # Categories
        pg_count = Category.objects.filter(is_deleted=False).count()
        try:
            es_count = client.count(index="categories")["count"]
            match = "OK" if pg_count == es_count else "MISMATCH"
            style = self.style.SUCCESS if match == "OK" else self.style.WARNING
            self.stdout.write(
                style(f"  Categories: PG={pg_count}, ES={es_count} [{match}]")
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Categories: PG={pg_count}, ES=error ({e})"))

        # Brands
        pg_count = Brand.objects.filter(is_deleted=False).count()
        try:
            es_count = client.count(index="brands")["count"]
            match = "OK" if pg_count == es_count else "MISMATCH"
            style = self.style.SUCCESS if match == "OK" else self.style.WARNING
            self.stdout.write(
                style(f"  Brands: PG={pg_count}, ES={es_count} [{match}]")
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Brands: PG={pg_count}, ES=error ({e})"))
