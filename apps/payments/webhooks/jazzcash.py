"""JazzCash: the browser return (``pp_ReturnURL``) and the IPN listener.

Both receive ``pp_*`` fields signed with ``pp_SecureHash``. A bad hash or
merchant id is rejected without touching any payment. A good one is
recorded (duplicates detected), and the Payment Inquiry API is asked by the
payment worker before the payment can become PAID; these endpoints never
wait on JazzCash, so they stay fast while JazzCash's API is down.
"""

import logging

from django.conf import settings
from rest_framework.parsers import FormParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import metrics, services
from ..models import Payment, PaymentNotification
from ..services import jazzcash
from . import find_payment, redirect_to_result

logger = logging.getLogger("apps.payments")


def _fields(request):
    return {str(k): str(v) for k, v in request.data.items()}


def _authentic(fields):
    return jazzcash.has_valid_hash(fields) and fields.get("pp_MerchantID") == settings.JAZZCASH_MERCHANT_ID


def _rejected(fields, source):
    metrics.incr("payment_invalid_notifications_total")
    metrics.event("warning", "payment_notification_rejected", provider="JAZZCASH", source=source,
                  reference=fields.get("pp_TxnRefNo", "")[:20], reason="invalid hash or merchant id")


def process(payment, fields, source, kind):
    """A signed JazzCash result: a non-success is applied at once (it is
    authentic), a success waits for the inquiry API (amount and currency
    are then checked against the signed fields too)."""
    return services.handle_notification(
        payment, provider_source=source, kind=kind, fields=fields, signature_valid=True,
        reported=jazzcash.from_callback(fields),
    )


class JazzCashReturnView(APIView):
    """POST /api/payments/jazzcash/callback/ — JazzCash sends the customer
    back here with the signed result; we verify it and redirect to the
    storefront's payment result page."""

    authentication_classes = []
    permission_classes = []
    parser_classes = [FormParser, JSONParser]

    def post(self, request):
        fields = _fields(request)
        payment = find_payment(fields.get("pp_TxnRefNo"), Payment.METHOD_JAZZCASH)
        if not _authentic(fields):
            _rejected(fields, "jazzcash_return")
        elif payment is None:
            logger.warning("JazzCash return for unknown payment %s", fields.get("pp_TxnRefNo", "")[:20])
        else:
            process(payment, fields, "jazzcash_return", PaymentNotification.SOURCE_RETURN)
        return redirect_to_result(payment)


class JazzCashIPNView(APIView):
    """POST /api/payments/jazzcash/ipn/ — JazzCash's server-to-server status
    notification (configure this URL as the IPN URL in the merchant portal).
    Needed for voucher (OTC) payments completed later at a JazzCash shop."""

    authentication_classes = []
    permission_classes = []
    parser_classes = [JSONParser, FormParser]

    def post(self, request):
        fields = _fields(request)
        if not _authentic(fields):
            _rejected(fields, "jazzcash_ipn")
            return self._reply("115", "Invalid hash received", status=400)
        payment = find_payment(fields.get("pp_TxnRefNo"), Payment.METHOD_JAZZCASH)
        if payment is None:
            return self._reply("109", "Transaction does not exist", status=404)
        process(payment, fields, "jazzcash_ipn", PaymentNotification.SOURCE_IPN)
        return self._reply("000", "IPN received")

    def _reply(self, code, message, status=200):
        body = {"pp_ResponseCode": code, "pp_ResponseMessage": message}
        body["pp_SecureHash"] = jazzcash.secure_hash(body)
        return Response(body, status=status)
