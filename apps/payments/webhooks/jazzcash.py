"""JazzCash: the browser return (``pp_ReturnURL``) and the IPN listener.

Both receive ``pp_*`` fields signed with ``pp_SecureHash``. A bad hash or
merchant id is rejected without touching any payment; a good one is
confirmed with the Payment Inquiry API before the payment is updated.
"""

import dataclasses
import logging

from django.conf import settings
from rest_framework.parsers import FormParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import services
from ..models import Payment
from ..services import jazzcash
from ..services.base import ProviderError, Verification
from . import find_payment, redirect_to_result

logger = logging.getLogger("apps.payments")


def _fields(request):
    return {str(k): str(v) for k, v in request.data.items()}


def _authentic(fields):
    return jazzcash.has_valid_hash(fields) and fields.get("pp_MerchantID") == settings.JAZZCASH_MERCHANT_ID


def process(payment, fields, source):
    """Apply a signed JazzCash result, using the inquiry API as the
    authority for the status and the signed fields for the amount."""
    signed = jazzcash.from_callback(fields)
    verified = True
    try:
        result = jazzcash.verify(payment)
    except ProviderError as exc:
        logger.warning("JazzCash inquiry for %s failed: %s", payment.payment_id, exc)
        verified = False
        # Without the inquiry a signed failure can be recorded, but a
        # success waits until the inquiry confirms it (status polling).
        result = signed if signed.status != Payment.STATUS_PAID else Verification(
            status=Payment.STATUS_PROCESSING, raw=signed.raw
        )
    else:
        result = dataclasses.replace(
            result,
            amount=result.amount if result.amount is not None else signed.amount,
            currency=result.currency or signed.currency,
            provider_transaction_id=result.provider_transaction_id or signed.provider_transaction_id,
        )
    return services.apply_result(payment.pk, result, source=source, verified=verified)


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
            logger.warning("Rejected JazzCash return with invalid hash/merchant (ref=%s)",
                           fields.get("pp_TxnRefNo", "")[:20])
        elif payment is None:
            logger.warning("JazzCash return for unknown payment %s", fields.get("pp_TxnRefNo", "")[:20])
        else:
            process(payment, fields, source="jazzcash_return")
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
            logger.warning("Rejected JazzCash IPN with invalid hash/merchant (ref=%s)",
                           fields.get("pp_TxnRefNo", "")[:20])
            return self._reply("115", "Invalid hash received", status=400)
        payment = find_payment(fields.get("pp_TxnRefNo"), Payment.METHOD_JAZZCASH)
        if payment is None:
            return self._reply("109", "Transaction does not exist", status=404)
        process(payment, fields, source="jazzcash_ipn")
        return self._reply("000", "IPN received")

    def _reply(self, code, message, status=200):
        body = {"pp_ResponseCode": code, "pp_ResponseMessage": message}
        body["pp_SecureHash"] = jazzcash.secure_hash(body)
        return Response(body, status=status)
