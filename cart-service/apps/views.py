import logging

import requests
from django.conf import settings
from rest_framework.views import APIView

from .authentication import SharedTokenAuthentication
from .models import Cart, CartItem
from .permissions import IsAuthenticated
from .responses import APIResponse
from .serializers import CartItemSerializer, CartItemUpdateSerializer

logger = logging.getLogger("cart")


def get_or_create_cart(user_id):
    """Return the user's existing cart or create a new one."""
    cart, _ = Cart.objects.get_or_create(user_id=user_id)
    return cart


def serialize_cart(cart):
    """Build the standard cart response dict."""
    items = [
        {
            "id": item.id,
            "product_id": item.product_id,
            "quantity": item.quantity,
        }
        for item in cart.items.all()
    ]
    return {
        "id": cart.id,
        "user_id": cart.user_id,
        "items": items,
        "created_at": cart.created_at.isoformat(),
        "updated_at": cart.updated_at.isoformat(),
    }


def validate_product(product_id):
    """Check that a product exists via the search-service API.

    Returns (product_data, error_response). If error_response is not None,
    the product is invalid.
    """
    try:
        resp = requests.get(
            f"{settings.SEARCH_SERVICE_URL}/api/products/{product_id}/",
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("data"), None
        elif resp.status_code == 404:
            return None, APIResponse.not_found("Product not found")
        else:
            return None, APIResponse.error(
                f"Product service returned {resp.status_code}",
                status_code=resp.status_code,
            )
    except requests.RequestException as e:
        logger.warning("Product validation request failed: %s", e)
        # ponytail: skip product validation if search-service is unreachable,
        # add circuit breaker if this becomes unreliable
        return None, None


class CartView(APIView):
    """GET /api/cart/ — Retrieve current user's cart.
    DELETE /api/cart/ — Clear current user's cart."""

    authentication_classes = [SharedTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cart = get_or_create_cart(request.user.id)
        return APIResponse.success(data=serialize_cart(cart))

    def delete(self, request):
        cart = get_or_create_cart(request.user.id)
        count = cart.items.count()
        cart.items.all().delete()
        logger.info("Cleared cart for user %s (%d items removed)", request.user.id, count)
        return APIResponse.success(data=serialize_cart(cart), message="Cart cleared successfully")


class CartItemAddView(APIView):
    """POST /api/cart/items/ — Add item to cart (or increase quantity)."""

    authentication_classes = [SharedTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CartItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        product_id = serializer.validated_data["product_id"]
        quantity = serializer.validated_data["quantity"]

        # Validate product exists
        product_data, error = validate_product(product_id)
        if error:
            return error
        # product_data may be None if validation was skipped (service unreachable)

        cart = get_or_create_cart(request.user.id)

        item, created = CartItem.objects.get_or_create(
            cart=cart, product_id=product_id, defaults={"quantity": quantity}
        )
        if not created:
            item.quantity += quantity
            item.save(update_fields=["quantity", "updated_at"])

        logger.info(
            "User %s: %s product %s (qty=%d, created=%s)",
            request.user.id,
            "added" if created else "updated",
            product_id,
            item.quantity,
            created,
        )

        return APIResponse.success(
            data={"id": item.id, "product_id": item.product_id, "quantity": item.quantity},
            message="Product added to cart successfully" if created else "Cart item quantity updated",
            status_code=201 if created else 200,
        )


class CartItemDetailView(APIView):
    """PATCH /api/cart/items/<id>/ — Update quantity.
    DELETE /api/cart/items/<id>/ — Remove item."""

    authentication_classes = [SharedTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def _get_owned_item(self, request, item_id):
        """Return (item, None) or (None, error_response). Ensures ownership."""
        try:
            item = CartItem.objects.select_related("cart").get(id=item_id)
        except CartItem.DoesNotExist:
            return None, APIResponse.not_found("Cart item not found")

        if item.cart.user_id != request.user.id:
            return None, APIResponse.forbidden("Access denied")

        return item, None

    def patch(self, request, item_id):
        serializer = CartItemUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        item, error = self._get_owned_item(request, item_id)
        if error:
            return error

        item.quantity = serializer.validated_data["quantity"]
        item.save(update_fields=["quantity", "updated_at"])

        logger.info("User %s: updated cart item %s to qty %d", request.user.id, item_id, item.quantity)

        return APIResponse.success(
            data={"id": item.id, "product_id": item.product_id, "quantity": item.quantity},
            message="Cart item updated successfully",
        )

    def delete(self, request, item_id):
        item, error = self._get_owned_item(request, item_id)
        if error:
            return error

        item.delete()
        logger.info("User %s: removed cart item %s", request.user.id, item_id)

        return APIResponse.success(message="Cart item removed successfully")
