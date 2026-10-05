"""Endpoints the payment providers (or the customer's browser, sent back by
a provider) call. They carry no user session: nothing they receive is
trusted until it has been verified with the provider."""

import re
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponseRedirect

from ..models import Payment

PAYMENT_ID_RE = re.compile(r"^PY[0-9A-F]{16}$")


def find_payment(reference, method):
    reference = str(reference or "").strip()
    if not PAYMENT_ID_RE.match(reference):
        return None
    return Payment.objects.filter(payment_id=reference, payment_method=method).first()


def redirect_to_result(payment):
    """Send the browser to the storefront, which polls the payment status."""
    query = urlencode({"payment_id": payment.payment_id}) if payment else "error=unknown_payment"
    return HttpResponseRedirect(f"{settings.PAYMENT_RESULT_URL}?{query}")
