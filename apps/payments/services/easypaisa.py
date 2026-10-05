"""Easypaisa (Easypay) online payments: hosted checkout and the
``inquire-transaction`` REST API for verification.

Follows the Easypaisa Merchant Integration Guide (v4.x):

* Hosted checkout: the browser POSTs ``storeId``, ``amount``,
  ``postBackURL``, ``orderRefNum``, ... plus ``merchantHashedReq`` to
  ``Index.jsf``. ``merchantHashedReq`` is the ``key=value`` pairs of the
  non-empty fields, sorted by key and joined with ``&``, encrypted with
  AES/ECB/PKCS5Padding under the store's hash key and Base64-encoded.
* Easypay sends the browser back to ``postBackURL`` with ``auth_token``;
  the merchant POSTs ``auth_token`` and ``postBackURL`` to ``Confirm.jsf``,
  the customer pays, and Easypay sends the browser to ``postBackURL`` again
  with ``status``, ``desc`` and ``orderRefNumber``. Those query values are
  not signed, so the result is always read from ``inquire-transaction``.
* REST calls authenticate with a ``Credentials`` header holding
  Base64(``username:password``) of the store's partner account.
"""

import base64
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings

from .base import ProviderError, Verification, post_json, redact

PKT = ZoneInfo("Asia/Karachi")

URLS = {
    "sandbox": {
        "checkout_url": "https://easypaystg.easypaisa.com.pk/easypay/Index.jsf",
        "confirm_url": "https://easypaystg.easypaisa.com.pk/easypay/Confirm.jsf",
        "inquiry_url": "https://easypaystg.easypaisa.com.pk/easypay-service/rest/v4/inquire-transaction",
    },
    "production": {
        "checkout_url": "https://easypay.easypaisa.com.pk/easypay/Index.jsf",
        "confirm_url": "https://easypay.easypaisa.com.pk/easypay/Confirm.jsf",
        "inquiry_url": "https://easypay.easypaisa.com.pk/easypay-service/rest/v4/inquire-transaction",
    },
}

# inquire-transaction ``transactionStatus`` -> Payment status.
STATUSES = {
    "PAID": "PAID",
    "PENDING": "PROCESSING",
    "INITIATED": "PROCESSING",
    "FAILED": "FAILED",
    "DECLINED": "FAILED",
    "BLOCKED": "FAILED",
    "EXPIRED": "EXPIRED",
    "REVERSED": "REFUNDED",
    "REFUNDED": "REFUNDED",
}


def _config():
    defaults = URLS["production" if settings.EASYPAISA_ENVIRONMENT == "production" else "sandbox"]
    return {
        "store_id": settings.EASYPAISA_STORE_ID,
        "hash_key": settings.EASYPAISA_HASH_KEY,
        "username": settings.EASYPAISA_USERNAME,
        "password": settings.EASYPAISA_PASSWORD,
        "account_num": settings.EASYPAISA_ACCOUNT_NUM,
        "return_url": settings.EASYPAISA_RETURN_URL,
        "payment_method": settings.EASYPAISA_PAYMENT_METHOD,
        "checkout_url": settings.EASYPAISA_CHECKOUT_URL or defaults["checkout_url"],
        "confirm_url": settings.EASYPAISA_CONFIRM_URL or defaults["confirm_url"],
        "inquiry_url": settings.EASYPAISA_INQUIRY_URL or defaults["inquiry_url"],
    }


def is_configured():
    return all(_config().values())


def hashed_request(fields, hash_key=None):
    """``merchantHashedReq`` for the checkout ``fields``."""
    key = (_config()["hash_key"] if hash_key is None else hash_key).encode("utf-8")
    if len(key) not in (16, 24, 32):
        raise ProviderError("EASYPAISA_HASH_KEY must be 16, 24 or 32 characters")
    message = "&".join(
        f"{name}={fields[name]}" for name in sorted(fields)
        if name != "merchantHashedReq" and str(fields[name]) != ""
    ).encode("utf-8")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(message) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()  # noqa: S305 - required by Easypay
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def format_amount(amount):
    """Easypay amounts are decimals with at least one place: ``15000.0``."""
    text = f"{Decimal(amount):.2f}"
    return text[:-1] if text.endswith("0") else text


def mobile_number(order):
    """The customer's mobile as ``03XXXXXXXXX`` when we have one."""
    for value in (order.contact, (order.billing_address or {}).get("phone", "")):
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        if digits.startswith("92") and len(digits) == 12:
            digits = "0" + digits[2:]
        if len(digits) == 11 and digits.startswith("03"):
            return digits
    return ""


def prepare(payment, order):
    payment.metadata["easypaisa"] = {
        "expiry_date": payment.expires_at.astimezone(PKT).strftime("%Y%m%d %H%M%S"),
    }


def checkout(payment, order):
    config = _config()
    fields = {
        "storeId": config["store_id"],
        "amount": format_amount(payment.amount),
        "postBackURL": config["return_url"],
        "orderRefNum": payment.payment_id,
        "expiryDate": payment.metadata["easypaisa"]["expiry_date"],
        "autoRedirect": "1",
        "paymentMethod": config["payment_method"],
        "emailAddr": order.contact if "@" in order.contact else "",
        "mobileNum": mobile_number(order),
    }
    fields["merchantHashedReq"] = hashed_request(fields, config["hash_key"])
    return {"method": "POST", "url": config["checkout_url"], "fields": fields}


def confirm_form(auth_token):
    """Second hosted-checkout step: what to POST to ``Confirm.jsf``."""
    config = _config()
    return {
        "method": "POST",
        "url": config["confirm_url"],
        "fields": {"auth_token": auth_token, "postBackURL": config["return_url"]},
    }


def _credentials(config):
    return base64.b64encode(f"{config['username']}:{config['password']}".encode("utf-8")).decode("ascii")


def verify(payment):
    """inquire-transaction: the order's status at Easypaisa."""
    config = _config()
    data = post_json(
        config["inquiry_url"],
        {"orderId": payment.payment_id, "storeId": config["store_id"], "accountNum": config["account_num"]},
        headers={"Credentials": _credentials(config)},
    )
    code = str(data.get("responseCode") or "")
    if code != "0000":
        raise ProviderError(f"Easypaisa inquiry failed: {code} {data.get('responseDesc', '')}".strip())
    if str(data.get("orderId") or payment.payment_id) != payment.payment_id:
        raise ProviderError("Easypaisa inquiry answered for a different order")

    transaction_status = str(data.get("transactionStatus") or "").upper()
    status = STATUSES.get(transaction_status)
    if status is None:
        raise ProviderError(f"Unrecognised Easypaisa transaction status: {transaction_status or 'missing'}")
    try:
        amount = Decimal(str(data["transactionAmount"])).quantize(Decimal("0.01"))
    except (KeyError, InvalidOperation, ValueError):
        amount = None
    return Verification(
        status=status,
        provider_transaction_id=str(data.get("transactionId") or ""),
        amount=amount,
        # Easypay only settles in PKR and doesn't echo a currency.
        currency="PKR",
        reason="" if status == "PAID" else f"{transaction_status} {data.get('responseDesc', '')}".strip(),
        raw=redact(data),
    )
