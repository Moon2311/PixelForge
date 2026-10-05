"""JazzCash Online Payment Gateway: HTTP POST (Page Redirection) checkout,
``pp_SecureHash`` signing, and the Payment Inquiry API for verification.

Follows the JazzCash "Payment Gateway Integration Guide for Merchants"
(v4.2, sandbox.jazzcash.com.pk/SandboxDocumentation):

* The browser POSTs the ``pp_*`` fields to the merchant form URL. The
  customer pays on JazzCash (mobile account, card or voucher) and JazzCash
  POSTs the result, signed with ``pp_SecureHash``, to ``pp_ReturnURL``.
* ``pp_SecureHash`` is the upper-case hex HMAC-SHA256, keyed with the
  integrity salt, of the salt followed by the non-empty values of every
  ``pp*`` field (except the hash) in ASCII order of the field names, all
  joined with ``&`` (guide §14.2).
* Amounts are sent in paisa (PKR 100.00 -> ``10000``); date-times are
  ``yyyyMMddHHmmss`` Pakistan time.
"""

import hashlib
import hmac
import re
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from .base import ProviderError, Verification, post_json, redact

PKT = ZoneInfo("Asia/Karachi")
DATETIME_FORMAT = "%Y%m%d%H%M%S"

SANDBOX_CHECKOUT_URL = "https://sandbox.jazzcash.com.pk/CustomerPortal/transactionmanagement/merchantform/"
SANDBOX_INQUIRY_URL = "https://sandbox.jazzcash.com.pk/ApplicationAPI/API/PaymentInquiry/Inquire"

# Payment response codes (guide, Appendix I) -> Payment status.
SUCCESS_CODES = {"000", "121", "200"}  # paid / confirmed by merchant / approved
PENDING_CODES = {"124", "157", "210"}  # voucher awaiting cash / pending / authorization pending
CANCELLED_CODES = {"112", "144", "410", "412"}  # cancelled or aborted by the customer
EXPIRED_CODES = {"116", "134"}  # transaction expired / timed out
REFUNDED_CODES = {"122", "131"}  # reversed / refunded
NOT_FOUND_CODE = "109"  # "Transaction does not exist." (customer never submitted)


def _config():
    sandbox = settings.JAZZCASH_ENVIRONMENT != "production"
    return {
        "merchant_id": settings.JAZZCASH_MERCHANT_ID,
        "password": settings.JAZZCASH_PASSWORD,
        "salt": settings.JAZZCASH_INTEGRITY_SALT,
        "return_url": settings.JAZZCASH_RETURN_URL,
        "version": settings.JAZZCASH_VERSION,
        "txn_type": settings.JAZZCASH_TXN_TYPE,
        # Production URLs are issued by JazzCash after business approval.
        "checkout_url": settings.JAZZCASH_CHECKOUT_URL or (SANDBOX_CHECKOUT_URL if sandbox else ""),
        "inquiry_url": settings.JAZZCASH_INQUIRY_URL or (SANDBOX_INQUIRY_URL if sandbox else ""),
    }


def is_configured():
    config = _config()
    return all(value for key, value in config.items() if key != "txn_type")


def secure_hash(fields, salt=None):
    """``pp_SecureHash`` for ``fields`` (guide §14.2)."""
    salt = _config()["salt"] if salt is None else salt
    values = [
        str(fields[name]) for name in sorted(fields)
        if name.lower().startswith("pp") and name != "pp_SecureHash" and str(fields[name]) != ""
    ]
    message = "&".join([salt, *values])
    return hmac.new(salt.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest().upper()


def has_valid_hash(fields):
    received = str(fields.get("pp_SecureHash") or "")
    return bool(received) and hmac.compare_digest(received.upper(), secure_hash(fields))


def to_paisa(amount):
    return str(int((Decimal(amount) * 100).to_integral_value()))


def from_paisa(value):
    try:
        return (Decimal(str(value)) / 100).quantize(Decimal("0.01"))
    except (ArithmeticError, ValueError):
        return None


def status_for_code(code):
    if code in SUCCESS_CODES:
        return "PAID"
    if code in PENDING_CODES:
        return "PROCESSING"
    if code in CANCELLED_CODES:
        return "CANCELLED"
    if code in EXPIRED_CODES:
        return "EXPIRED"
    if code in REFUNDED_CODES:
        return "REFUNDED"
    return "FAILED"


def prepare(payment, order):
    created = timezone.now().astimezone(PKT)
    payment.metadata["jazzcash"] = {
        "txn_datetime": created.strftime(DATETIME_FORMAT),
        "expiry_datetime": payment.expires_at.astimezone(PKT).strftime(DATETIME_FORMAT),
    }


def checkout(payment, order):
    config = _config()
    times = payment.metadata["jazzcash"]
    fields = {
        "pp_Version": config["version"],
        "pp_TxnType": config["txn_type"],  # empty: the customer picks on JazzCash
        "pp_Language": "EN",
        "pp_MerchantID": config["merchant_id"],
        "pp_SubMerchantID": "",
        "pp_Password": config["password"],
        "pp_BankID": "",
        "pp_ProductID": "",
        "pp_TxnRefNo": payment.payment_id,
        "pp_Amount": to_paisa(payment.amount),
        "pp_TxnCurrency": payment.currency,
        "pp_TxnDateTime": times["txn_datetime"],
        # Bill reference allows letters, digits and "." only.
        "pp_BillReference": re.sub(r"[^A-Za-z0-9.]", "", order.number)[:20],
        "pp_Description": f"Payment for order {order.number}",
        "pp_TxnExpiryDateTime": times["expiry_datetime"],
        "pp_ReturnURL": config["return_url"],
        "ppmpf_1": order.number,
    }
    if config["version"] != "1.1":
        fields["pp_IsRegisteredCustomer"] = "No"
    fields["pp_SecureHash"] = secure_hash(fields, config["salt"])
    return {"method": "POST", "url": config["checkout_url"], "fields": fields}


def from_callback(fields):
    """The result JazzCash POSTed to the return URL (hash already checked).

    Used for the charged amount/currency; the status itself is taken from
    the Payment Inquiry API (``verify``).
    """
    code = str(fields.get("pp_ResponseCode") or "")
    return Verification(
        status=status_for_code(code),
        provider_transaction_id=str(fields.get("pp_RetreivalReferenceNo") or ""),
        amount=from_paisa(fields["pp_Amount"]) if fields.get("pp_Amount") else None,
        currency=fields.get("pp_TxnCurrency") or None,
        reason="" if code in SUCCESS_CODES else f"{code} {fields.get('pp_ResponseMessage', '')}".strip(),
        raw=redact(fields),
    )


def verify(payment):
    """Payment Inquiry API: the transaction's status at JazzCash."""
    config = _config()
    payload = {
        "pp_TxnRefNo": payment.payment_id,
        "pp_MerchantID": config["merchant_id"],
        "pp_Password": config["password"],
        "pp_Version": config["version"],
    }
    payload["pp_SecureHash"] = secure_hash(payload, config["salt"])
    data = post_json(config["inquiry_url"], payload)

    if data.get("pp_SecureHash") and not has_valid_hash(data):
        raise ProviderError("JazzCash inquiry response has an invalid pp_SecureHash")
    code = str(data.get("pp_ResponseCode") or "")
    message = str(data.get("pp_ResponseMessage") or "")
    if code == NOT_FOUND_CODE:
        return Verification(status="PENDING", reason=message, raw=redact(data))
    if code and code != "000":
        raise ProviderError(f"JazzCash inquiry failed: {code} {message}".strip())

    payment_code = str(data.get("pp_PaymentResponseCode") or "")
    if payment_code:
        status = status_for_code(payment_code)
        reason = "" if status == "PAID" else (
            f"{payment_code} {data.get('pp_PaymentResponseMessage', '')}".strip()
        )
    else:
        text = str(data.get("pp_Status") or data.get("status") or "").lower()
        status = {"completed": "PAID", "success": "PAID", "failed": "FAILED",
                  "pending": "PROCESSING"}.get(text)
        if status is None:
            raise ProviderError(f"Unrecognised JazzCash inquiry status: {text or 'missing'}")
        reason = "" if status == "PAID" else text
    return Verification(
        status=status,
        provider_transaction_id=str(
            data.get("pp_RetreivalReferenceNo") or data.get("pp_RetrievalReferenceNo")
            or data.get("rrn") or ""
        ),
        amount=from_paisa(data["pp_Amount"]) if data.get("pp_Amount") else None,
        currency=data.get("pp_TxnCurrency") or None,
        reason=reason,
        raw=redact(data),
    )
