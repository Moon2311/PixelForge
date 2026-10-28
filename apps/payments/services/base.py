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

from apps.events.retry import NonRetryableError, RetryableError

logger = logging.getLogger("apps.payments")


class ProviderError(Exception):
    """The provider couldn't be reached or gave an unusable answer.

    Subclasses classify the failure for retries and the circuit breaker
    (``category`` for logs and metrics). ``retry_after``: seconds before a
    retry makes sense, when known.
    """

    category = "provider_error"
    # Counts as the gateway being unavailable (circuit breaker).
    trips_circuit = False
    retry_after = None


class ProviderUnavailable(ProviderError, RetryableError):
    """Connection refused/reset, DNS failure, HTTP 5xx: retry later."""

    category = "unavailable"
    trips_circuit = True


class ProviderTimeout(ProviderUnavailable):
    """No answer in PAYMENT_PROVIDER_TIMEOUT seconds. For an inquiry this is
    only "ask again"; nothing was charged by asking."""

    category = "timeout"


class ProviderRateLimited(ProviderUnavailable):
    """HTTP 429."""

    category = "rate_limited"

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class CircuitOpen(ProviderUnavailable):
    """The circuit breaker refused the call (the provider failed repeatedly);
    nothing was sent."""

    category = "circuit_open"
    trips_circuit = False

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class ProviderResponseError(ProviderError, RetryableError):
    """The provider answered, but not usably (invalid JSON or signature, an
    error code, an unknown status): retried, without tripping the circuit."""

    category = "bad_response"


class ProviderAuthError(ProviderError, NonRetryableError):
    """HTTP 401/403: our credentials or IP allow-listing are wrong.
    Retrying can't fix it; an operator must."""

    category = "auth"


class ProviderConfigError(ProviderError, NonRetryableError):
    """Our configuration is unusable (e.g. a hash key of the wrong length)."""

    category = "config"


class ProviderRequestError(ProviderError, NonRetryableError):
    """HTTP 4xx other than 401/403/429: the provider rejected our request."""

    category = "invalid_request"


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
        raise _http_error(exc, url) from None
    except TimeoutError:
        raise ProviderTimeout(f"No answer from {url} in {settings.PAYMENT_PROVIDER_TIMEOUT}s") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ProviderTimeout(f"No answer from {url} in {settings.PAYMENT_PROVIDER_TIMEOUT}s") from None
        raise ProviderUnavailable(f"Could not reach {url}: {exc.reason}") from None
    except OSError as exc:
        raise ProviderUnavailable(f"Could not reach {url}: {exc}") from None
    try:
        data = json.loads(body)
    except ValueError:
        raise ProviderResponseError(f"Invalid JSON from {url}") from None
    if not isinstance(data, dict):
        raise ProviderResponseError(f"Unexpected response from {url}")
    return data


def _http_error(exc, url):
    code = exc.code
    if code == 429:
        try:
            retry_after = float(exc.headers.get("Retry-After")) if exc.headers else None
        except (TypeError, ValueError):
            retry_after = None
        return ProviderRateLimited(f"HTTP 429 from {url}", retry_after=retry_after)
    if code in (401, 403):
        return ProviderAuthError(f"HTTP {code} from {url}")
    if code >= 500 or code in (408, 425):
        return ProviderUnavailable(f"HTTP {code} from {url}")
    return ProviderRequestError(f"HTTP {code} from {url}")
