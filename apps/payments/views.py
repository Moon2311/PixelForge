import logging

from rest_framework.parsers import JSONParser
from rest_framework.views import APIView

from apps.authentication.authentication import BearerTokenAuthentication
from apps.authentication.permissions import IsAuthenticated
from apps.common.custom_response import CustomResponse

from . import services
from .models import Payment
from .serializers import CreatePaymentSerializer, PaymentSerializer
from .services import PaymentError, create_payment

logger = logging.getLogger("apps.payments")


class PaymentCreateView(APIView):
    """POST /api/payments/create/ — start paying one of the current user's
    orders with JazzCash or Easypaisa.

    The amount is the order total from the database. The response's
    ``checkout`` tells the browser where to go: submit ``fields`` to ``url``
    with ``method``. Repeating the request while the attempt is still open
    returns the same payment (200 instead of 201).
    """

    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def post(self, request):
        serializer = CreatePaymentSerializer(data=request.data)
        if not serializer.is_valid():
            return CustomResponse.failed_response(
                "Please check your payment details",
                data=serializer.errors,
            )
        try:
            payment, checkout, created = create_payment(
                request.user,
                serializer.validated_data["order_id"],
                serializer.validated_data["payment_method"],
            )
        except PaymentError as exc:
            return CustomResponse.failed_response(exc.message, status=exc.status_code)
        except services.ProviderError as exc:
            logger.error("Could not start %s checkout: %s", serializer.validated_data["payment_method"], exc)
            return CustomResponse.failed_response(
                "The payment provider is unavailable. Please try again later.",
                status=502,
            )
        data = {**PaymentSerializer(payment).data, "checkout_url": checkout["url"], "checkout": checkout}
        if created:
            return CustomResponse.successful_response(data, "Payment started", status=201)
        return CustomResponse.successful_response(data, "Payment already in progress")


class PaymentDetailView(APIView):
    """GET /api/payments/<payment_id>/ — one of the current user's payments.

    While the payment is open the provider is asked again (at most every
    few seconds), so polling after returning from the provider, or after a
    missed callback, settles the payment.
    """

    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, payment_id):
        payment = (
            Payment.objects.select_related("order")
            .filter(payment_id=payment_id, order__user_id=request.user.id)
            .first()
        )
        if payment is None:
            return CustomResponse.failed_response("Payment not found", status=404)
        payment = services.refresh(payment)
        payment = Payment.objects.select_related("order").get(pk=payment.pk)
        return CustomResponse.successful_response(PaymentSerializer(payment).data)
