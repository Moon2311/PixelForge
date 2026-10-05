"""Pieces shared by the provider modules (``jazzcash``, ``easypaisa``).

Each provider module exposes the same functions:

``is_configured()``
    True when every credential the provider needs is set.
``prepare(payment, order)``
    Fill ``payment.metadata`` with what the checkout must repeat exactly
    (timestamps), before the payment is saved.
``checkout(payment, order)``
    The browser hand-off as ``{"method", "url", "fields"}``: React submits
    ``fields`` to ``url`` with ``method``. Built from the payment alone, so a
    repeated "Pay Now" gets the very same checkout.
``verify(payment)``
    Ask the provider, server to server, what happened to the payment.
    Returns a ``Verification``; raises ``ProviderError`` when it can't tell.
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger("apps.payments")


class ProviderError(Exception):
    """The provider couldn't be reached or gave an unusable answer."""


@dataclass(frozen=True)
class Verification:
    """What the provider says about a payment.

    ``status`` is one of ``Payment.STATUS_*``. ``amount``/``currency`` are
    what the provider says was charged, when it tells us.
    """

    status: str
    provider_transaction_id: str = ""
    amount: Decimal | None = None
    currency: str | None = None
    reason: str = ""
    raw: dict = field(default_factory=dict)


# Response keys never written to ``Payment.metadata`` or the logs.
SECRET_KEYS = {
    "pp_password", "pp_securehash", "merchanthashedreq", "encryptedhashrequest",
    "credentials", "pp_customercardno", "pp_customercardcvv", "pp_customercardexpiry",
    "customercardno", "cardnumber", "cvv", "pin",
}


def redact(data):
    """``data`` without credentials, hashes or card details."""
    return {k: v for k, v in (data or {}).items() if str(k).lower() not in SECRET_KEYS}


def post_json(url, payload, headers=None):
    """POST ``payload`` as JSON and return the decoded JSON object."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.PAYMENT_PROVIDER_TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"HTTP {exc.code} from {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"Could not reach {url}: {exc}") from None
    try:
        data = json.loads(body)
    except ValueError:
        raise ProviderError(f"Invalid JSON from {url}") from None
    if not isinstance(data, dict):
        raise ProviderError(f"Unexpected response from {url}")
    return data
