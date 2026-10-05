import logging

from rest_framework.parsers import JSONParser
from rest_framework.views import APIView

from apps.authentication.authentication import BearerTokenAuthentication
from apps.authentication.permissions import IsAdmin, IsAuthenticated
from apps.common.custom_response import CustomResponse

from . import selectors
from .models import Order
from .serializers import AdminOrderSerializer, OrderSerializer, PlaceOrderSerializer
from .services import CheckoutConflict, checkout_options, place_order

logger = logging.getLogger("orders")


class CheckoutOptionsView(APIView):
    """GET /api/orders/checkout-options/ — shipping and payment methods."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return CustomResponse.successful_response(checkout_options())


class OrderListCreateView(APIView):
    """GET /api/orders/ — the current user's orders, newest first.
    POST /api/orders/ — place an order from the current user's cart."""

    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def get(self, request):
        orders = selectors.user_orders(request.user.id)
        return CustomResponse.successful_response(OrderSerializer(orders, many=True).data)

    def post(self, request):
        serializer = PlaceOrderSerializer(data=request.data)
        if not serializer.is_valid():
            return CustomResponse.failed_response(
                "Please check your checkout details",
                data=serializer.errors,
            )
        try:
            order = place_order(request.user, serializer.validated_data)
        except CheckoutConflict as exc:
            return CustomResponse.failed_response(
                exc.message,
                data={"problems": exc.problems},
                status=409,
            )
        logger.info("User %s placed order %s (total=%s)", request.user.id, order.number, order.total)
        return CustomResponse.successful_response(
            OrderSerializer(order).data,
            "Order placed successfully",
            status=201,
        )


class OrderDetailView(APIView):
    """GET /api/orders/<number>/ — one of the current user's orders."""

    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, number):
        order = (
            Order.objects.prefetch_related("items")
            .filter(number=number, user_id=request.user.id)
            .first()
        )
        if order is None:
            return CustomResponse.failed_response("Order not found", status=404)
        return CustomResponse.successful_response(OrderSerializer(order).data)


def _int_param(request, name):
    value = (request.query_params.get(name) or "").strip()
    return int(value) if value.isdigit() else None


class AdminOrderListView(APIView):
    """GET /api/orders/admin/orders/?user_id=&status=&q= — all orders with
    their buyers. With ``user_id`` the response also has the customer's
    totals."""

    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [IsAdmin]

    def get(self, request):
        user_id = _int_param(request, "user_id")
        status = (request.query_params.get("status") or "").strip() or None
        query = (request.query_params.get("q") or "").strip() or None
        orders = list(selectors.admin_orders(user_id=user_id, status=status, query=query))
        customers = selectors.customers_by_id(
            [o.user_id for o in orders] + ([user_id] if user_id else [])
        )
        data = {
            "orders": AdminOrderSerializer(
                orders, many=True, context={"customers": customers}
            ).data,
            "customer": None,
        }
        if user_id:
            data["customer"] = {
                **(customers.get(user_id) or {"id": user_id, "username": "", "email": "", "name": ""}),
                **selectors.customer_summary(user_id),
            }
        return CustomResponse.successful_response(data)


class AdminProductSalesView(APIView):
    """GET /api/orders/admin/products/<id>/sales/ — who bought a product."""

    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [IsAdmin]

    def get(self, request, product_id):
        return CustomResponse.successful_response(selectors.product_sales(product_id))
