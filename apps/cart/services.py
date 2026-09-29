"""Cart domain helpers.

Product checks go through the catalog module's public selector (an
in-process call), not through the catalog HTTP API.
"""

from datetime import timedelta

from django.utils import timezone

from apps.cart.models import Cart, GuestCart
from apps.catalog.selectors import get_product

GUEST_CART_TTL = timedelta(days=30)


def find_product(product_id):
    """Return the catalog's product document, or None if it doesn't exist."""
    return get_product(product_id)


def get_or_create_cart(user_id):
    """Return the user's existing cart or create a new one."""
    cart, _ = Cart.objects.get_or_create(user_id=user_id)
    return cart


def get_or_create_guest_cart(session_id):
    """Return the guest cart for this session, creating if needed."""
    cart, _ = GuestCart.objects.get_or_create(
        session_id=session_id,
        defaults={"expires_at": timezone.now() + GUEST_CART_TTL},
    )
    return cart
