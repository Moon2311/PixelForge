from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.cart.models import GuestCart


class Command(BaseCommand):
    help = "Remove expired guest carts"

    def handle(self, *args, **options):
        now = timezone.now()
        count, _ = GuestCart.objects.filter(expires_at__lt=now).delete()
        self.stdout.write(f"Deleted {count} expired guest cart(s)")
