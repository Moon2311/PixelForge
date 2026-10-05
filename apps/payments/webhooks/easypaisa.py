"""Easypaisa: the hosted-checkout ``postBackURL`` and the IPN listener.

Nothing Easypay sends through the browser is signed, so these endpoints
only tell us *which* payment to look at; its status always comes from the
``inquire-transaction`` API.
"""

import logging
import re

from django.http import HttpResponse, HttpResponseBadRequest
from django.utils.html import format_html, format_html_join
from rest_framework.parsers import FormParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import services
from ..models import Payment
from ..services import easypaisa
from . import find_payment, redirect_to_result

logger = logging.getLogger("apps.payments")

AUTH_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,512}$")


def auto_submit_form(form):
    """An HTML page that POSTs ``form`` (``{method, url, fields}``) at once."""
    inputs = format_html_join(
        "", '<input type="hidden" name="{}" value="{}">', form["fields"].items()
    )
    return HttpResponse(format_html(
        '<!doctype html><html><head><meta charset="utf-8"><title>Redirecting to Easypaisa…</title></head>'
        '<body onload="document.forms[0].submit()">'
        '<form method="{}" action="{}">{}<noscript><button type="submit">Continue to Easypaisa</button>'
        "</noscript></form></body></html>",
        form["method"].lower(), form["url"], inputs,
    ))


class EasypaisaReturnView(APIView):
    """GET /api/payments/easypaisa/callback/ — the hosted checkout's
    ``postBackURL``. Called twice by the browser:

    1. with ``auth_token``: forward it to Easypay's ``Confirm.jsf``;
    2. with ``orderRefNumber`` (and ``status``/``desc``) once the customer
       is done: verify with Easypaisa and redirect to the storefront.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        auth_token = request.query_params.get("auth_token")
        if auth_token is not None:
            if not AUTH_TOKEN_RE.match(auth_token):
                return HttpResponseBadRequest("Invalid auth_token")
            return auto_submit_form(easypaisa.confirm_form(auth_token))

        reference = request.query_params.get("orderRefNumber") or request.query_params.get("orderRefNum")
        payment = find_payment(reference, Payment.METHOD_EASYPAISA)
        if payment is None:
            logger.warning("Easypaisa return for unknown payment %s", str(reference)[:20])
        else:
            services.refresh(payment, include_closed=True)
        return redirect_to_result(payment)


class EasypaisaIPNView(APIView):
    """GET or POST /api/payments/easypaisa/ipn/ — Easypaisa's Instant
    Payment Notification (configure this URL in the Easypay merchant
    portal). The notification only names the order; we never fetch URLs
    from it and instead ask ``inquire-transaction``."""

    authentication_classes = []
    permission_classes = []
    parser_classes = [JSONParser, FormParser]

    def get(self, request):
        return self._handle({**request.query_params.dict()})

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        return self._handle({**request.query_params.dict(), **{str(k): v for k, v in data.items()}})

    def _handle(self, data):
        reference = data.get("orderRefNumber") or data.get("orderRefNum") or data.get("orderId")
        if not reference and data.get("url"):
            # The IPN can carry an order-status URL ending in the order ref.
            reference = str(data["url"]).rstrip("/").rsplit("/", 1)[-1]
        payment = find_payment(reference, Payment.METHOD_EASYPAISA)
        if payment is None:
            return Response({"status": "UNKNOWN_ORDER"}, status=404)
        payment = services.refresh(payment, include_closed=True)
        return Response({"status": "OK", "orderRefNumber": payment.payment_id})
