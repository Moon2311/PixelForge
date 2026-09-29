import logging

from rest_framework.parsers import JSONParser
from rest_framework.views import APIView

from apps.authentication.authentication import BearerTokenAuthentication
from apps.authentication.permissions import IsAuthenticated
from apps.common.responses import APIResponse

from .models import CartItem, GuestCart, GuestCartItem
from .serializers import (
    CartItemSerializer,
    CartItemUpdateSerializer,
    GuestCartItemCreateSerializer,
    GuestCartItemUpdateSerializer,
)
from .services import find_product, get_or_create_cart, get_or_create_guest_cart

logger = logging.getLogger("cart")


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
    """Return an error response if the product doesn't exist, else None."""
    if find_product(product_id) is None:
        return APIResponse.not_found("Product not found")
    return None


class CartAPIView(APIView):
    """Cart endpoints accept JSON bodies only (as before)."""

    parser_classes = [JSONParser]


class CartView(CartAPIView):
    """GET /api/cart/ — Retrieve current user's cart.
    DELETE /api/cart/ — Clear current user's cart."""

    authentication_classes = [BearerTokenAuthentication]
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


class CartItemAddView(CartAPIView):
    """POST /api/cart/items/ — Add item to cart (or increase quantity)."""

    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CartItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        product_id = serializer.validated_data["product_id"]
        quantity = serializer.validated_data["quantity"]

        error = validate_product(product_id)
        if error:
            return error

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


class CartItemDetailView(CartAPIView):
    """PATCH /api/cart/items/<id>/ — Update quantity.
    DELETE /api/cart/items/<id>/ — Remove item."""

    authentication_classes = [BearerTokenAuthentication]
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


def _get_guest_session(request):
    """Extract and validate X-Guest-Session header. Returns (session_id, None) or (None, error)."""
    session_id = request.headers.get("X-Guest-Session", "").strip()
    if not session_id:
        return None, APIResponse.error("X-Guest-Session header required")
    return session_id, None


def serialize_guest_cart(cart):
    """Build the guest cart response dict."""
    items = []
    total = 0
    for item in cart.items.all():
        line_total = item.unit_price * item.quantity
        total += line_total
        items.append({
            "id": item.id,
            "product_id": item.product_id,
            "variant_id": item.variant_id,
            "product_name": item.product_name,
            "product_image_url": item.product_image_url,
            "unit_price": str(item.unit_price),
            "quantity": item.quantity,
            "line_total": str(line_total),
        })
    return {
        "id": cart.id,
        "session_id": cart.session_id,
        "items": items,
        "total": str(total),
        "item_count": len(items),
        "expires_at": cart.expires_at.isoformat(),
        "updated_at": cart.updated_at.isoformat(),
    }


class GuestCartView(CartAPIView):
    """GET /api/cart/guest/ — Retrieve or create guest cart.
    DELETE /api/cart/guest/ — Clear all items from guest cart."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        session_id, error = _get_guest_session(request)
        if error:
            return error

        cart = get_or_create_guest_cart(session_id)
        return APIResponse.success(data=serialize_guest_cart(cart))

    def delete(self, request):
        session_id, error = _get_guest_session(request)
        if error:
            return error

        try:
            cart = GuestCart.objects.get(session_id=session_id)
        except GuestCart.DoesNotExist:
            return APIResponse.not_found("Guest cart not found")

        count = cart.items.count()
        cart.items.all().delete()
        logger.info("Cleared guest cart %s (%d items removed)", session_id, count)
        return APIResponse.success(
            data=serialize_guest_cart(cart), message="Guest cart cleared successfully"
        )


class GuestCartItemView(CartAPIView):
    """POST /api/cart/guest/items/ — Add item to guest cart.
    PATCH /api/cart/guest/items/<int:pk>/ — Update quantity.
    DELETE /api/cart/guest/items/<int:pk>/ — Remove item."""

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        session_id, error = _get_guest_session(request)
        if error:
            return error

        serializer = GuestCartItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        error = validate_product(data["product_id"])
        if error:
            return error

        cart = get_or_create_guest_cart(session_id)

        item, created = GuestCartItem.objects.get_or_create(
            cart=cart,
            product_id=data["product_id"],
            variant_id=data["variant_id"],
            defaults={
                "product_name": data["product_name"],
                "product_image_url": data.get("product_image_url", ""),
                "unit_price": data["unit_price"],
                "quantity": data["quantity"],
            },
        )
        if not created:
            item.quantity += data["quantity"]
            item.save(update_fields=["quantity"])

        logger.info(
            "Guest %s: %s product %s (qty=%d, created=%s)",
            session_id,
            "added" if created else "updated",
            data["product_id"],
            item.quantity,
            created,
        )

        return APIResponse.success(
            data={
                "id": item.id,
                "product_id": item.product_id,
                "variant_id": item.variant_id,
                "quantity": item.quantity,
            },
            message="Item added to guest cart" if created else "Guest cart item quantity updated",
            status_code=201 if created else 200,
        )

    def patch(self, request, pk):
        session_id, error = _get_guest_session(request)
        if error:
            return error

        serializer = GuestCartItemUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            item = GuestCartItem.objects.select_related("cart").get(
                id=pk, cart__session_id=session_id
            )
        except GuestCartItem.DoesNotExist:
            return APIResponse.not_found("Guest cart item not found")

        quantity = serializer.validated_data["quantity"]
        if quantity == 0:
            item.delete()
            return APIResponse.success(message="Guest cart item removed")

        item.quantity = quantity
        item.save(update_fields=["quantity"])
        return APIResponse.success(
            data={
                "id": item.id,
                "product_id": item.product_id,
                "variant_id": item.variant_id,
                "quantity": item.quantity,
            },
            message="Guest cart item updated",
        )

    def delete(self, request, pk):
        session_id, error = _get_guest_session(request)
        if error:
            return error

        try:
            item = GuestCartItem.objects.select_related("cart").get(
                id=pk, cart__session_id=session_id
            )
        except GuestCartItem.DoesNotExist:
            return APIResponse.not_found("Guest cart item not found")

        item.delete()
        logger.info("Guest %s: removed cart item %s", session_id, pk)
        return APIResponse.success(message="Guest cart item removed")


class MergeCartView(CartAPIView):
    """POST /api/cart/merge/ — Merge guest cart into authenticated cart."""

    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        session_id = request.data.get("session_id", "").strip()
        if not session_id:
            return APIResponse.error("session_id is required")

        # Get authenticated cart
        auth_cart = get_or_create_cart(request.user.id)

        # Look up guest cart
        try:
            guest_cart = GuestCart.objects.get(session_id=session_id)
        except GuestCart.DoesNotExist:
            return APIResponse.success(
                data=serialize_cart(auth_cart),
                message="No guest cart found — nothing to merge",
            )

        # Merge items
        for guest_item in guest_cart.items.all():
            existing = CartItem.objects.filter(
                cart=auth_cart,
                product_id=guest_item.product_id,
            ).first()

            if existing:
                new_qty = min(existing.quantity + guest_item.quantity, 99)
                existing.quantity = new_qty
                existing.save(update_fields=["quantity", "updated_at"])
            else:
                CartItem.objects.create(
                    cart=auth_cart,
                    product_id=guest_item.product_id,
                    quantity=guest_item.quantity,
                )

        # Delete guest cart (cascade deletes items)
        guest_cart.delete()
        logger.info(
            "Merged guest cart %s into user %s cart", session_id, request.user.id
        )

        return APIResponse.success(
            data=serialize_cart(auth_cart), message="Guest cart merged successfully"
        )
