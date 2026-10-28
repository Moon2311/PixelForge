import logging
import re

from rest_framework.parsers import JSONParser
from rest_framework.views import APIView

from apps.authentication.authentication import BearerTokenAuthentication
from apps.authentication.permissions import IsAuthenticated
from apps.common.custom_response import CustomResponse

from . import health, services
from .models import Payment
from .serializers import CreatePaymentSerializer, PaymentSerializer
from .services import PaymentError, create_payment, retry_payment

logger = logging.getLogger("apps.payments")

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _idempotency_key(request):
    """The optional ``Idempotency-Key`` header; raises PaymentError if malformed."""
    key = request.headers.get("Idempotency-Key", "").strip()
    if key and not IDEMPOTENCY_KEY_RE.match(key):
        raise PaymentError("Idempotency-Key must be 1-64 letters, digits or . _ : -", 400)
    return key


def _started(payment, checkout, created):
    data = {**PaymentSerializer(payment).data, "checkout_url": checkout["url"] if checkout else None,
            "checkout": checkout}
    if created:
        return CustomResponse.successful_response(data, "Payment started", status=201)
    if payment.status == Payment.STATUS_PENDING:
        return CustomResponse.successful_response(data, "Payment already in progress")
    return CustomResponse.successful_response(data, PaymentSerializer().get_status_message(payment))


def _provider_unavailable(method, exc):
    logger.error("Could not start %s checkout: %s", method, exc)
    return CustomResponse.failed_response(
        "The payment provider is unavailable. Please try again later.", status=502,
    )


class PaymentCreateView(APIView):
    """POST /api/payments/create/ — start paying one of the current user's
    orders with JazzCash or Easypaisa.

    The amount is the order total from the database. The response's
    ``checkout`` tells the browser where to go: submit ``fields`` to ``url``
    with ``method``. Repeating the request while the attempt is still open
    returns the same payment (200 instead of 201); so does repeating an
    optional ``Idempotency-Key`` header, even after the attempt finished.
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
        method = serializer.validated_data["payment_method"]
        try:
            payment, checkout, created = create_payment(
                request.user, serializer.validated_data["order_id"], method, _idempotency_key(request),
            )
        except PaymentError as exc:
            return CustomResponse.failed_response(exc.message, status=exc.status_code)
        except services.ProviderError as exc:
            return _provider_unavailable(method, exc)
        return _started(payment, checkout, created)


class PaymentRetryView(APIView):
    """POST /api/payments/<payment_id>/retry/ — pay again after this payment
    definitively failed (FAILED, CANCELLED or EXPIRED). Opens a new attempt
    with a new reference (201), or returns the order's attempt that is
    already open (200). Refused (409) while the payment is still open,
    pending confirmation or paid."""

    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def post(self, request, payment_id):
        try:
            payment, checkout, created = retry_payment(request.user, payment_id, _idempotency_key(request))
        except PaymentError as exc:
            return CustomResponse.failed_response(exc.message, status=exc.status_code)
        except services.ProviderError as exc:
            return _provider_unavailable("payment", exc)
        return _started(payment, checkout, created)


class PaymentDetailView(APIView):
    """GET /api/payments/<payment_id>/ — one of the current user's payments.

    While the payment is open a provider check is queued (at most every
    PAYMENT_RECHECK_SECONDS; the payment worker asks the provider), so
    polling after returning from the provider, or after a missed callback,
    settles the payment. The request itself never waits on the provider.

    200 when the status is final; 202 while it isn't (accepted, still being
    processed or confirmed): poll again.
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
        if payment.status in Payment.ACTIVE_STATUSES:
            services.request_verification(payment, reason="status_poll")
            payment = Payment.objects.select_related("order").get(pk=payment.pk)
        data = PaymentSerializer(payment).data
        return CustomResponse.successful_response(
            data, data["status_message"], status=200 if payment.is_final else 202,
        )


class PaymentsHealthView(APIView):
    """GET /api/health/payments/ — payment pipeline health: open payments
    by status, UNKNOWN payments awaiting reconciliation, circuit breakers,
    duplicate notifications, payment worker and reconciler counters.

    Always 200 with ``status`` healthy/degraded and a ``problems`` list.
    Counts and states only: no amounts, references or customer data.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        data = health.snapshot()
        return CustomResponse.successful_response(data, f"Payments {data['status']}")
