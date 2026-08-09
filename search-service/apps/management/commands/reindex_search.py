"""Management command to reindex data from PostgreSQL to Elasticsearch.

This command creates versioned indices and reindexes all data from
PostgreSQL into Elasticsearch. It supports full reindex and incremental
reindex for specific entity types.

Usage:
    python manage.py reindex_search --full
    python manage.py reindex_search --type products
    python manage.py reindex_search --type categories
    python manage.py reindex_search --type brands
    python manage.py reindex_search --switch-alias
    python manage.py reindex_search --delete-old
"""

import logging
import time

from django.core.management.base import BaseCommand

from apps.elasticsearch import get_elasticsearch_client
from apps.search_indices import SearchIndices

logger = logging.getLogger("catalog.search")


class Command(BaseCommand):
    help = "Reindex data from PostgreSQL to Elasticsearch"

    def add_arguments(self, parser):
        parser.add_argument(
            "--full",
            action="store_true",
            help="Perform full reindex (create indices + reindex all data)",
        )
        parser.add_argument(
            "--type",
            type=str,
            choices=["products", "categories", "brands"],
            help="Reindex specific entity type",
        )
        parser.add_argument(
            "--create-indices",
            action="store_true",
            help="Create versioned indices with aliases",
        )
        parser.add_argument(
            "--switch-alias",
            action="store_true",
            help="Switch aliases to point to latest versioned indices",
        )
        parser.add_argument(
            "--delete-old",
            action="store_true",
            help="Delete old versioned indices (keeps latest)",
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Verify index health and document counts after reindex",
        )

    def handle(self, *args, **options):
        client = get_elasticsearch_client()
        indices = SearchIndices(client)

        if options["full"]:
            self._full_reindex(indices, options)
        elif options["create_indices"]:
            self._create_indices(indices)
        elif options["type"]:
            self._reindex_type(indices, options["type"])
        elif options["switch_alias"]:
            self._switch_alias(indices)
        elif options["delete_old"]:
            self._delete_old(indices)
        else:
            self.stdout.write(
                self.style.WARNING(
                    "Please specify an action: --full, --create-indices, "
                    "--type, --switch-alias, --delete-old"
                )
            )

    def _full_reindex(self, indices: SearchIndices, options: dict):
        """Perform full reindex."""
        self.stdout.write(self.style.SUCCESS("Starting full reindex..."))
        start_time = time.time()

        # Create indices
        self.stdout.write("Creating versioned indices...")
        indices.create_all()

        # Reindex data
        self.stdout.write("Reindexing data...")
        indices.reindex_all()

        # Switch aliases
        if options.get("switch_alias"):
            self.stdout.write("Switching aliases...")
            indices.switch_alias()

        elapsed = time.time() - start_time
        self.stdout.write(
            self.style.SUCCESS(f"Full reindex completed in {elapsed:.2f} seconds")
        )

        # Verify
        if options.get("verify"):
            self._verify(indices)

    def _create_indices(self, indices: SearchIndices):
        """Create versioned indices."""
        self.stdout.write("Creating versioned indices...")
        indices.create_all()
        self.stdout.write(self.style.SUCCESS("Indices created successfully"))

    def _reindex_type(self, indices: SearchIndices, entity_type: str):
        """Reindex specific entity type."""
        from apps.search_document_builder import SearchDocumentBuilder

        builder = SearchDocumentBuilder(indices.client)

        self.stdout.write(f"Reindexing {entity_type}...")

        if entity_type == "products":
            count = builder.reindex_products()
        elif entity_type == "categories":
            count = builder.reindex_categories()
        elif entity_type == "brands":
            count = builder.reindex_brands()
        else:
            self.stdout.write(self.style.ERROR(f"Unknown entity type: {entity_type}"))
            return

        self.stdout.write(
            self.style.SUCCESS(f"Reindexed {count} {entity_type}")
        )

    def _switch_alias(self, indices: SearchIndices):
        """Switch aliases."""
        self.stdout.write("Switching aliases...")
        indices.switch_alias()
        self.stdout.write(self.style.SUCCESS("Aliases switched successfully"))

    def _delete_old(self, indices: SearchIndices):
        """Delete old indices."""
        self.stdout.write("Deleting old indices...")
        indices.delete_old_indices(keep_versions=1)
        self.stdout.write(self.style.SUCCESS("Old indices deleted"))

    def _verify(self, indices: SearchIndices):
        """Verify index health."""
        self.stdout.write("Verifying indices...")
        result = indices.verify()

        for name, info in result.items():
            status = info.get("status", "unknown")
            count = info.get("count", 0)
            if status == "ok":
                self.stdout.write(
                    self.style.SUCCESS(f"  {name}: {count} documents (OK)")
                )
            else:
                self.stdout.write(
                    self.style.ERROR(f"  {name}: {count} documents ({status})")
                )
