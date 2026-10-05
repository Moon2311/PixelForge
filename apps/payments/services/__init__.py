"""Online payment of orders: open a provider checkout for an order, and apply
what the provider verifies to the payment and its order, exactly once.

Locks are always taken order first, then payment, so the checkout, the
provider callbacks and status polling can't deadlock or race each other.
Provider HTTP calls are made outside database transactions.
"""

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.orders.models import Order

from ..models import Payment
from . import easypaisa, jazzcash
from .base import ProviderError, Verification

logger = logging.getLogger("apps.payments")

PROVIDERS = {Payment.METHOD_JAZZCASH: jazzcash, Payment.METHOD_EASYPAISA: easypaisa}

# Ask the provider about the same payment at most this often (status
# polling, IPN retries).
RECHECK_SECONDS = 10
HISTORY_LIMIT = 20
# Statuses a provider result can still turn into PAID: the customer may
# finish paying an attempt after we gave up on it or replaced it.
REOPENABLE_STATUSES = (*Payment.ACTIVE_STATUSES, Payment.STATUS_FAILED,
                       Payment.STATUS_CANCELLED, Payment.STATUS_EXPIRED)


class PaymentError(Exception):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def enabled_methods():
    """Payment methods whose credentials are configured."""
    return [method for method, provider in PROVIDERS.items() if provider.is_configured()]


def enabled_order_methods():
    """``Order.payment_method`` values that can be paid online right now."""
    return [Payment.ORDER_METHODS[method] for method in enabled_methods()]


def _check_payable(order):
    if order.payments.filter(status=Payment.STATUS_PAID).exists():
        raise PaymentError("This order has already been paid.")
    if order.payment_method not in Order.PAYABLE_ONLINE_METHODS or order.status != Order.STATUS_AWAITING_PAYMENT:
        raise PaymentError("This order can't be paid online.")


def create_payment(user, order_number, method):
    """Start (or resume) paying ``user``'s order with ``method``.

    Returns ``(payment, checkout, created)``. Pressing "Pay Now" again while
    the attempt is still open returns the same payment and checkout. An
    earlier attempt with another method, or an expired one, is checked with
    its provider first and only then replaced.
    """
    provider = PROVIDERS[method]
    if not provider.is_configured():
        raise PaymentError(f"{dict(Payment.METHOD_CHOICES)[method]} payments aren't available right now.", 400)

    for _ in range(3):
        with transaction.atomic():
            order = Order.objects.select_for_update().filter(number=order_number, user_id=user.id).first()
            if order is None:
                raise PaymentError("Order not found", 404)
            _check_payable(order)
            active = order.payments.filter(status__in=Payment.ACTIVE_STATUSES).first()
            if active is None:
                payment = Payment(
                    order=order,
                    payment_method=method,
                    amount=order.total,
                    currency="PKR",
                    expires_at=timezone.now() + timedelta(minutes=settings.PAYMENT_EXPIRY_MINUTES),
                )
                provider.prepare(payment, order)
                payment.save()
                logger.info("Payment %s started for order %s (%s, %s)",
                            payment.payment_id, order.number, method, payment.amount)
                return payment, provider.checkout(payment, order), True
            if (active.payment_method == method and active.status == Payment.STATUS_PENDING
                    and not active.is_expired):
                return active, provider.checkout(active, order), False

        # The open attempt is for another method or has expired. Ask its
        # provider first, so a payment the customer did make isn't dropped.
        refresh(active, force=True)
        with transaction.atomic():
            Order.objects.select_for_update().get(pk=active.order_id)
            stale = Payment.objects.select_for_update().get(pk=active.pk)
            if stale.status == Payment.STATUS_PROCESSING and not stale.is_expired:
                raise PaymentError(
                    "Your previous payment is still being processed. "
                    "Please wait a moment before trying again."
                )
            if stale.status in Payment.ACTIVE_STATUSES:
                stale.status = Payment.STATUS_EXPIRED if stale.is_expired else Payment.STATUS_CANCELLED
                stale.failure_reason = "Replaced by a new payment attempt"
                stale.save(update_fields=["status", "failure_reason", "updated_at"])
    raise PaymentError("Could not start the payment. Please try again.")


def _recently_checked(payment):
    checked = payment.metadata.get("last_checked_at")
    if not checked:
        return False
    try:
        checked_at = datetime.fromisoformat(checked)
    except ValueError:
        return False
    return timezone.now() - checked_at < timedelta(seconds=RECHECK_SECONDS)


def refresh(payment, force=False, include_closed=False):
    """Re-verify ``payment`` with its provider and apply the answer.

    Only open payments are checked unless ``include_closed`` (a provider
    told us something happened). Provider errors leave the payment as it is.
    """
    statuses = REOPENABLE_STATUSES if include_closed else Payment.ACTIVE_STATUSES
    if payment.status not in statuses or (not force and _recently_checked(payment)):
        return payment
    try:
        result = PROVIDERS[payment.payment_method].verify(payment)
    except ProviderError as exc:
        logger.warning("Could not verify payment %s: %s", payment.payment_id, exc)
        _note_check(payment.pk, "error")
        return Payment.objects.get(pk=payment.pk)
    return apply_result(payment.pk, result, source="inquiry")


def _note_check(payment_pk, outcome):
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_pk)
        payment.metadata["last_checked_at"] = timezone.now().isoformat()
        payment.metadata["last_check"] = outcome
        payment.save(update_fields=["metadata", "updated_at"])


def _mismatch(payment, result):
    if result.amount is not None and result.amount != payment.amount:
        return f"Amount mismatch: provider reported {result.amount}, expected {payment.amount}"
    if result.currency and result.currency.upper() != payment.currency:
        return f"Currency mismatch: provider reported {result.currency}, expected {payment.currency}"
    return ""


def apply_result(payment_pk, result: Verification, source, verified=True):
    """Apply a provider ``result`` to the payment and its order.

    Idempotent: repeated or late callbacks never pay an order twice, never
    turn a paid payment back into a failed one, and only the first PAID
    result confirms the order. ``verified=False`` marks a result the
    provider's API didn't confirm, so status polling asks again at once.
    """
    order_id = Payment.objects.values_list("order_id", flat=True).get(pk=payment_pk)
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        payment = Payment.objects.select_for_update().get(pk=payment_pk)
        now = timezone.now()
        meta = payment.metadata
        if verified:
            meta["last_checked_at"] = now.isoformat()
        meta["last_check"] = result.status if verified else "unverified"
        meta["provider_response"] = result.raw
        meta["history"] = (meta.get("history", []) + [
            {"at": now.isoformat(), "source": source, "status": result.status},
        ])[-HISTORY_LIMIT:]
        previous = payment.status

        if result.status == Payment.STATUS_PAID and previous not in (Payment.STATUS_PAID, Payment.STATUS_REFUNDED):
            _apply_paid(order, payment, result, now)
        elif result.status == Payment.STATUS_REFUNDED and previous == Payment.STATUS_PAID:
            payment.status = Payment.STATUS_REFUNDED
            meta["needs_review"] = True
            logger.error("Payment %s for order %s was refunded/reversed at the provider; "
                         "review the order", payment.payment_id, order.number)
        elif result.status in (Payment.STATUS_FAILED, Payment.STATUS_CANCELLED,
                               Payment.STATUS_EXPIRED, Payment.STATUS_REFUNDED):
            if previous in Payment.ACTIVE_STATUSES:
                payment.status = (Payment.STATUS_FAILED if result.status == Payment.STATUS_REFUNDED
                                  else result.status)
                payment.failure_reason = result.reason[:255]
        elif result.status == Payment.STATUS_PROCESSING and previous == Payment.STATUS_PENDING:
            payment.status = Payment.STATUS_PROCESSING
        elif result.status == Payment.STATUS_PENDING and previous in Payment.ACTIVE_STATUSES and payment.is_expired:
            # The provider has no record of a payment and its time is up.
            payment.status = Payment.STATUS_EXPIRED
            payment.failure_reason = "The payment was not completed in time"

        payment.save()
    if payment.status != previous:
        logger.info("Payment %s: %s -> %s (%s)", payment.payment_id, previous, payment.status, source)
    return payment


def _apply_paid(order, payment, result, now):
    meta = payment.metadata
    problem = _mismatch(payment, result)
    txn = result.provider_transaction_id
    if not problem and txn and Payment.objects.filter(
        payment_method=payment.payment_method, provider_transaction_id=txn
    ).exclude(pk=payment.pk).exists():
        problem = f"Provider transaction {txn} already belongs to another payment"
    if problem:
        logger.error("Payment %s for order %s not accepted: %s", payment.payment_id, order.number, problem)
        payment.status = Payment.STATUS_FAILED
        payment.failure_reason = problem[:255]
        meta["needs_review"] = True
        return

    payment.status = Payment.STATUS_PAID
    payment.paid_at = now
    payment.failure_reason = ""
    if txn:
        payment.provider_transaction_id = txn
    if order.status == Order.STATUS_AWAITING_PAYMENT:
        order.status = Order.STATUS_CONFIRMED
        order.payment_method = Payment.ORDER_METHODS[payment.payment_method]
        order.save(update_fields=["status", "payment_method", "updated_at"])
        logger.info("Order %s confirmed by payment %s", order.number, payment.payment_id)
    else:
        # Paid twice (another attempt already paid) or paid after the order
        # was cancelled: keep the record and flag it for a refund.
        meta["needs_refund"] = True
        logger.error("Payment %s was paid but order %s is already %s; refund required",
                     payment.payment_id, order.number, order.status)
