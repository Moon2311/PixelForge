from django.core.management.base import BaseCommand

from apps.catalog.stats import refresh_product_stats


class Command(BaseCommand):
    help = "Recompute denormalized product search stats (price range, stock, rating)"

    def handle(self, *args, **options):
        count = refresh_product_stats()
        self.stdout.write(self.style.SUCCESS(f"Refreshed stats for {count} product(s)"))
