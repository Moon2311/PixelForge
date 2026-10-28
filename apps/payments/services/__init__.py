"""Online payment of orders: open a provider checkout for an order, and apply
what the provider verifies to the payment and its order, exactly once.

JazzCash and Easypaisa are hosted checkouts: the customer is charged on the
provider's page, never by a call from this server. Our server-to-server
calls are status inquiries only, so asking again can't charge anyone; what
must never happen is a *second checkout* for an order whose first attempt
may have been paid. Hence:

* at most one open attempt (PENDING/PROCESSING/UNKNOWN) per order, and an
  open attempt is only replaced after its provider confirmed what happened
  to it (``create_payment``);
* the attempt's ``payment_id`` is the provider reference for its whole life
  (every inquiry and notification uses it);
* when the provider can't be asked (timeout, outage, open circuit), a
  payment it may have charged becomes UNKNOWN and stays open until
  verification or an operator resolves it.

Verification runs in the ``payments`` consumer group
(``request_verification`` -> outbox -> Kafka -> ``tasks.verify_payment`` ->
``run_verification``), behind a per-provider circuit breaker with retries,
backoff and the dead-letter topic; ``reconcile`` re-requests anything left
open. Web requests never wait on a provider, except when replacing an open
attempt (``verify_now``), which fails fast while the circuit is open.

Locks are always taken order first, then payment, so the checkout, the
provider callbacks and verification can't deadlock or race each other.
Provider HTTP calls are made outside database transactions.
"""

import hashlib
import json
import logging
import time
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.events import outbox, topics
from apps.events.retry import sanitize_error
from apps.orders.models import Order

from .. import metrics
from ..models import Payment, PaymentNotification
from . import circuit, easypaisa, jazzcash
from .base import CircuitOpen, ProviderError, Verification, redact

logger = logging.getLogger("apps.payments")

PROVIDERS = {Payment.METHOD_JAZZCASH: jazzcash, Payment.METHOD_EASYPAISA: easypaisa}

HISTORY_LIMIT = 20
# Statuses a provider result can still turn into PAID: the customer may
# finish paying an attempt after we gave up on it or replaced it.
REOPENABLE_STATUSES = (*Payment.ACTIVE_STATUSES, Payment.STATUS_FAILED,
                       Payment.STATUS_CANCELLED, Payment.STATUS_EXPIRED)
RETRYABLE_STATUSES = (Payment.STATUS_FAILED, Payment.STATUS_CANCELLED, Payment.STATUS_EXPIRED)

VERIFICATION_REQUESTED = "payment.verification_requested"


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


def checkout_for(payment, order):
    """The checkout for an attempt the customer can still pay, else None."""
    if payment.status != Payment.STATUS_PENDING or payment.is_expired:
        return None
    return PROVIDERS[payment.payment_method].checkout(payment, order)


# -- starting a payment -------------------------------------------------------


def create_payment(user, order_number, method, idempotency_key=""):
    """Start (or resume) paying ``user``'s order with ``method``.

    Returns ``(payment, checkout, created)``; ``checkout`` is None when the
    payment can't be paid any more (an idempotent replay of a finished
    request). Pressing "Pay Now" again while the attempt is still open
    returns the same payment and checkout; repeating an ``idempotency_key``
    returns the payment it created. An earlier attempt with another method,
    or an expired one, is checked with its provider first and only then
    replaced; if the provider can't be asked, nothing is replaced.
    """
    provider = PROVIDERS[method]
    if not provider.is_configured():
        raise PaymentError(f"{dict(Payment.METHOD_CHOICES)[method]} payments aren't available right now.", 400)

    for _ in range(3):
        with transaction.atomic():
            order = Order.objects.select_for_update().filter(number=order_number, user_id=user.id).first()
            if order is None:
                raise PaymentError("Order not found", 404)
            if idempotency_key:
                existing = order.payments.filter(idempotency_key=idempotency_key).first()
                if existing is not None:
                    if existing.payment_method != method:
                        raise PaymentError("This Idempotency-Key was already used for another payment request.", 422)
                    return existing, checkout_for(existing, order), False
            _check_payable(order)
            active = order.payments.filter(status__in=Payment.ACTIVE_STATUSES).first()
            if active is None:
                payment = Payment(
                    order=order,
                    payment_method=method,
                    amount=order.total,
                    currency="PKR",
                    expires_at=timezone.now() + timedelta(minutes=settings.PAYMENT_EXPIRY_MINUTES),
                    idempotency_key=idempotency_key,
                )
                provider.prepare(payment, order)
                payment.save()
                metrics.incr("payment_attempts_created_total")
                metrics.event("info", "payment_attempt_created", payment_id=payment.payment_id,
                              order=order.number, method=method, amount=payment.amount)
                return payment, provider.checkout(payment, order), True
            if (active.payment_method == method and active.status == Payment.STATUS_PENDING
                    and not active.is_expired):
                return active, provider.checkout(active, order), False
            if active.status == Payment.STATUS_UNKNOWN:
                raise PaymentError(
                    "Your previous payment is awaiting confirmation from the provider. Please don't pay "
                    "again: your order will be updated as soon as it is confirmed."
                )

        # The open attempt is for another method or has expired. Ask its
        # provider first, so a payment the customer did make isn't dropped
        # and a second checkout isn't opened while the first may be charged.
        if not verify_now(active):
            raise PaymentError(
                "We can't confirm your previous payment attempt with the provider right now. "
                "Please try again in a few minutes.",
                503,
            )
        with transaction.atomic():
            Order.objects.select_for_update().get(pk=active.order_id)
            stale = Payment.objects.select_for_update().get(pk=active.pk)
            if stale.status in (Payment.STATUS_PROCESSING, Payment.STATUS_UNKNOWN):
                raise PaymentError(
                    "Your previous payment is still being processed. "
                    "Please wait a moment before trying again."
                )
            if stale.status == Payment.STATUS_PENDING:
                stale.status = Payment.STATUS_EXPIRED if stale.is_expired else Payment.STATUS_CANCELLED
                stale.failure_reason = "Replaced by a new payment attempt"
                stale.save(update_fields=["status", "failure_reason", "updated_at"])
    raise PaymentError("Could not start the payment. Please try again.")


def retry_payment(user, payment_id, idempotency_key=""):
    """Pay again after a definitive failure (FAILED/CANCELLED/EXPIRED).

    Opens a new attempt with a new reference, with the same safety checks
    as "Pay Now": refused while any attempt of the order is open or paid.
    """
    payment = (Payment.objects.select_related("order")
               .filter(payment_id=payment_id, order__user_id=user.id).first())
    if payment is None:
        raise PaymentError("Payment not found", 404)
    if payment.status not in RETRYABLE_STATUSES:
        if payment.status == Payment.STATUS_PAID:
            raise PaymentError("This payment has already succeeded.")
        raise PaymentError("This payment hasn't failed, so it can't be retried. Please wait for its result.")
    return create_payment(user, payment.order.number, payment.payment_method, idempotency_key)


# -- verification ---------------------------------------------------------------


def request_verification(payment, reason, include_closed=False, force=False):
    """Queue a provider status inquiry for ``payment`` (payments group).

    At most one request per payment every PAYMENT_RECHECK_SECONDS, unless
    ``force`` (a provider notification). Not queued while the provider's
    circuit is open, unless ``force``: reconciliation asks again later.
    Returns True when a request was queued.
    """
    statuses = REOPENABLE_STATUSES if include_closed else Payment.ACTIVE_STATUSES
    if payment.status not in statuses:
        return False
    if not force and circuit.is_open(payment.payment_method):
        metrics.incr("payment_verifications_deferred_total")
        return False
    now = timezone.now()
    order_number = Order.objects.values_list("number", flat=True).get(pk=payment.order_id)
    with transaction.atomic():
        claim = Payment.objects.filter(pk=payment.pk)
        if not force:
            claim = claim.filter(Q(next_check_at__isnull=True) | Q(next_check_at__lte=now))
        if not claim.update(next_check_at=now + timedelta(seconds=settings.PAYMENT_RECHECK_SECONDS)):
            metrics.incr("payment_verifications_throttled_total")
            return False
        outbox.enqueue(
            VERIFICATION_REQUESTED, topic=topics.PAYMENT_PROCESSING, aggregate_type="payment",
            aggregate_id=payment.payment_id, key=order_number,
            payload={
                "payment_id": payment.payment_id,
                "order_number": order_number,
                "method": payment.payment_method,
                "reason": reason[:30],
                "include_closed": include_closed,
            },
        )
    metrics.incr("payment_verifications_requested_total")
    metrics.event("info", "payment_verification_requested", payment_id=payment.payment_id,
                  order=order_number, reason=reason, status=payment.status)
    return True


def inquire(payment):
    """Ask the provider about ``payment`` through its circuit breaker.

    Returns a ``Verification``; raises a ``ProviderError`` subclass
    (``CircuitOpen`` without calling the provider).
    """
    method = payment.payment_method
    call = circuit.before_call(method)
    metrics.incr("payment_gateway_calls_total")
    started = time.monotonic()
    try:
        result = PROVIDERS[method].verify(payment)
    except Exception as exc:
        duration = (time.monotonic() - started) * 1000
        circuit.record_failure(call, exc)
        metrics.incr("payment_gateway_failures_total")
        metrics.observe_gateway(method, duration, failed=True)
        metrics.event("warning", "payment_gateway_failed", provider=method, payment_id=payment.payment_id,
                      category=getattr(exc, "category", "unexpected"), probe=call.probe,
                      duration_ms=round(duration, 1), error=sanitize_error(exc))
        raise
    duration = (time.monotonic() - started) * 1000
    circuit.record_success(call)
    metrics.observe_gateway(method, duration)
    metrics.event("info", "payment_gateway_answered", provider=method, payment_id=payment.payment_id,
                  provider_status=result.status, probe=call.probe, duration_ms=round(duration, 1))
    return result


def run_verification(payment_id, reason="", include_closed=False):
    """Ask the provider about one payment and apply the answer (the worker's
    task body). Provider errors are recorded on the payment and re-raised
    for the worker to retry or dead-letter; a PROCESSING payment whose
    provider can't be asked becomes UNKNOWN."""
    payment = Payment.objects.get(payment_id=payment_id)
    statuses = REOPENABLE_STATUSES if include_closed else Payment.ACTIVE_STATUSES
    if payment.status not in statuses:
        return payment
    try:
        result = inquire(payment)
    except ProviderError as exc:
        _record_failed_check(payment.pk, exc)
        raise
    return apply_result(payment.pk, _with_signed_details(payment, result), source=f"inquiry:{reason}"[:40])


def verify_now(payment):
    """Verify in the current request (replacing an open attempt). True when
    the provider answered."""
    try:
        run_verification(payment.payment_id, reason="replace")
    except ProviderError as exc:
        logger.warning("Could not verify payment %s: %s", payment.payment_id, sanitize_error(exc))
        return False
    return True


def _record_failed_check(payment_pk, exc):
    now = timezone.now()
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_pk)
        fields = ["last_error", "metadata", "updated_at"]
        if not isinstance(exc, CircuitOpen):
            payment.check_attempts += 1
            fields.append("check_attempts")
        payment.last_error = sanitize_error(exc, limit=255)
        payment.metadata["last_check"] = "error"
        became_unknown = payment.status == Payment.STATUS_PROCESSING
        if became_unknown:
            # The provider told us something happened and can't confirm it.
            payment.status = Payment.STATUS_UNKNOWN
            payment.unknown_since = now
            fields += ["status", "unknown_since"]
        payment.save(update_fields=fields)
    if became_unknown:
        metrics.incr("payment_status_unknown_total")
        metrics.event("warning", "payment_status_unknown", payment_id=payment.payment_id,
                      method=payment.payment_method, category=getattr(exc, "category", ""))


def _with_signed_details(payment, result):
    """Fill what the inquiry didn't return (amount, currency, transaction id)
    from a signed provider notification (JazzCash ``pp_*`` fields)."""
    signed = payment.metadata.get("signed_result") or {}
    if not signed:
        return result
    amount = result.amount
    if amount is None and signed.get("amount"):
        amount = Decimal(signed["amount"])
    return Verification(
        status=result.status,
        provider_transaction_id=result.provider_transaction_id or signed.get("provider_transaction_id", ""),
        amount=amount,
        currency=result.currency or signed.get("currency") or None,
        reason=result.reason,
        raw=result.raw,
    )


# -- provider notifications ------------------------------------------------------


def handle_notification(payment, *, provider_source, kind, fields, signature_valid, reported=None,
                        claims_success=False):
    """A provider (or the customer's browser, sent back by it) told us
    something about ``payment``. Record it (once per distinct content),
    note what it says, and queue a verification; the inquiry API decides.

    ``reported``: a signed result (JazzCash). A signed non-success is
    authentic and applied at once (still verified afterwards); a signed
    success only makes the payment PROCESSING ("pending confirmation").
    ``claims_success``: an unsigned success claim (Easypaisa), same effect.
    """
    _record_notification(payment, kind, fields, signature_valid, reported)
    if payment.status in (Payment.STATUS_PAID, Payment.STATUS_REFUNDED):
        return payment  # already settled: nothing a notification can add
    if reported is not None and reported.status != Payment.STATUS_PAID:
        payment = apply_result(payment.pk, reported, source=provider_source, verified=False)
    elif reported is not None or claims_success:
        payment = _note_success_signal(payment.pk, reported, provider_source)
    request_verification(payment, reason=provider_source, include_closed=True, force=True)
    return Payment.objects.get(pk=payment.pk)


def _record_notification(payment, kind, fields, signature_valid, reported):
    data = redact(fields)
    key = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    now = timezone.now()
    try:
        with transaction.atomic():
            PaymentNotification.objects.create(
                provider=payment.payment_method, source=kind, payment=payment, dedupe_key=key,
                signature_valid=signature_valid, reported_status=reported.status if reported else "",
                data=data, first_received_at=now, last_received_at=now,
            )
        return False
    except IntegrityError:
        PaymentNotification.objects.filter(provider=payment.payment_method, dedupe_key=key).update(
            receive_count=F("receive_count") + 1, last_received_at=now,
        )
        metrics.incr("payment_duplicate_notifications_total")
        metrics.event("info", "payment_notification_duplicate", provider=payment.payment_method,
                      payment_id=payment.payment_id, kind=kind)
        return True


def _note_success_signal(payment_pk, reported, source):
    order_id = Payment.objects.values_list("order_id", flat=True).get(pk=payment_pk)
    with transaction.atomic():
        Order.objects.select_for_update().get(pk=order_id)
        payment = Payment.objects.select_for_update().get(pk=payment_pk)
        meta = payment.metadata
        if reported is not None:
            meta["signed_result"] = {
                "status": reported.status,
                "amount": str(reported.amount) if reported.amount is not None else "",
                "currency": reported.currency or "",
                "provider_transaction_id": reported.provider_transaction_id,
            }
        meta["history"] = (meta.get("history", []) + [
            {"at": timezone.now().isoformat(), "source": source, "status": "SUCCESS_REPORTED"},
        ])[-HISTORY_LIMIT:]
        if payment.status == Payment.STATUS_PENDING:
            payment.status = Payment.STATUS_PROCESSING
        payment.save()
    return payment


# -- applying a result -------------------------------------------------------------


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
    provider's API didn't confirm (a signed notification); it can never
    mark a payment PAID.
    """
    order_id = Payment.objects.values_list("order_id", flat=True).get(pk=payment_pk)
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        payment = Payment.objects.select_for_update().get(pk=payment_pk)
        now = timezone.now()
        meta = payment.metadata
        if verified:
            payment.last_checked_at = now
            payment.check_attempts += 1
            payment.next_check_at = now + timedelta(seconds=settings.PAYMENT_RECHECK_SECONDS)
            payment.last_error = ""
        meta["last_check"] = result.status if verified else "unverified"
        meta["provider_response"] = result.raw
        meta["history"] = (meta.get("history", []) + [
            {"at": now.isoformat(), "source": source, "status": result.status},
        ])[-HISTORY_LIMIT:]
        previous = payment.status

        if result.status == Payment.STATUS_PAID and previous not in (Payment.STATUS_PAID, Payment.STATUS_REFUNDED):
            if verified:
                _apply_paid(order, payment, result, now)
            else:
                logger.error("Refusing an unverified PAID result for payment %s", payment.payment_id)
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
        elif result.status == Payment.STATUS_PROCESSING and previous in (Payment.STATUS_PENDING,
                                                                         Payment.STATUS_UNKNOWN):
            payment.status = Payment.STATUS_PROCESSING
        elif (result.status == Payment.STATUS_PENDING and previous in (Payment.STATUS_PENDING,
                                                                       Payment.STATUS_PROCESSING)
              and payment.is_expired):
            # The provider has no record of a payment and its time is up.
            # (UNKNOWN stays UNKNOWN: we were told it was paid.)
            payment.status = Payment.STATUS_EXPIRED
            payment.failure_reason = "The payment was not completed in time"

        if payment.status != Payment.STATUS_UNKNOWN:
            payment.unknown_since = None
        payment.save()
        if payment.status != previous:
            _enqueue_status_event(order, payment, previous, verified)
    if payment.status != previous:
        if payment.status == Payment.STATUS_PAID:
            metrics.incr("payment_status_paid_total")
        elif payment.status == Payment.STATUS_FAILED:
            metrics.incr("payment_status_failed_total")
        metrics.event("info", "payment_status_changed", payment_id=payment.payment_id, order=order.number,
                      from_status=previous, to_status=payment.status, source=source, verified=verified)
    elif previous in Payment.FINAL_STATUSES:
        metrics.incr("payment_duplicate_results_total")
    return payment


# Payment status -> event for downstream work (emails, audit). Expired and
# cancelled attempts need nobody's attention.
STATUS_EVENTS = {
    Payment.STATUS_PAID: "payment.succeeded",
    Payment.STATUS_FAILED: "payment.failed",
    Payment.STATUS_REFUNDED: "payment.refunded",
}


def _enqueue_status_event(order, payment, previous, verified):
    """Write the payment's new status to the outbox, inside the transaction
    that applied it: the event exists exactly when the change committed.

    Only a server-side verified result can mark a payment PAID (a browser
    redirect alone never does), so ``payment.succeeded`` is only ever
    published for verified payments. No provider response, transaction id
    or credentials go into the event.
    """
    event_type = STATUS_EVENTS.get(payment.status)
    if event_type is None:
        return
    if payment.status == Payment.STATUS_PAID and not verified:
        logger.error("Refusing to publish an unverified PAID status for payment %s", payment.payment_id)
        return
    outbox.enqueue(
        event_type, topic=topics.PAYMENT_EVENTS, aggregate_type="payment", aggregate_id=payment.payment_id,
        key=order.number,
        payload={
            "payment_id": payment.payment_id,
            "order_number": order.number,
            "user_id": order.user_id,
            "method": payment.payment_method,
            "status": payment.status,
            "previous_status": previous,
            "amount": payment.amount,
            "currency": payment.currency,
            "order_status": order.status,
            "needs_refund": bool(payment.metadata.get("needs_refund")),
            "needs_review": bool(payment.metadata.get("needs_review")),
            "reason": payment.failure_reason[:120],
        },
    )


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


# -- reconciliation ------------------------------------------------------------------


def reconcile(now=None, limit=None):
    """One reconciliation pass (``manage.py payments_reconcile``).

    Queues a verification for PROCESSING/UNKNOWN payments not verified for
    PAYMENT_RECONCILE_AFTER seconds and for PENDING ones that expired that
    long ago, oldest check first (skipped while their provider's circuit is
    open). Flags UNKNOWN payments older than PAYMENT_UNKNOWN_ESCALATE_AFTER
    for manual review.
    """
    now = now or timezone.now()
    limit = limit or settings.PAYMENT_RECONCILE_BATCH
    cutoff = now - timedelta(seconds=settings.PAYMENT_RECONCILE_AFTER)
    due = Q(next_check_at__isnull=True) | Q(next_check_at__lte=now)
    stale_open = Q(status__in=[Payment.STATUS_PROCESSING, Payment.STATUS_UNKNOWN]) & (
        Q(last_checked_at__lte=cutoff) | Q(last_checked_at__isnull=True, created_at__lte=cutoff)
    )
    abandoned = Q(status=Payment.STATUS_PENDING, expires_at__lte=cutoff)
    candidates = list(
        Payment.objects.filter(due).filter(stale_open | abandoned)
        .order_by(F("last_checked_at").asc(nulls_first=True), "created_at")[:limit]
    )
    requested = sum(1 for payment in candidates if request_verification(payment, reason="reconcile"))
    escalated = _escalate_unknown(now, limit)
    if requested:
        metrics.incr("payment_reconciled_total", requested)
    result = {"candidates": len(candidates), "requested": requested,
              "deferred": len(candidates) - requested, "escalated": escalated}
    if candidates or escalated:
        metrics.event("info", "payment_reconcile_pass", **result)
    return result


def _escalate_unknown(now, limit):
    cutoff = now - timedelta(seconds=settings.PAYMENT_UNKNOWN_ESCALATE_AFTER)
    ids = list(Payment.objects.filter(status=Payment.STATUS_UNKNOWN, unknown_since__lte=cutoff,
                                      escalated_at__isnull=True).values_list("pk", flat=True)[:limit])
    escalated = 0
    for pk in ids:
        with transaction.atomic():
            payment = Payment.objects.select_for_update().select_related("order").get(pk=pk)
            if payment.status != Payment.STATUS_UNKNOWN or payment.escalated_at:
                continue
            payment.escalated_at = now
            payment.metadata["needs_review"] = True
            payment.save(update_fields=["escalated_at", "metadata", "updated_at"])
        escalated += 1
        metrics.incr("payment_escalated_total")
        metrics.event("error", "payment_unknown_escalated", payment_id=payment.payment_id,
                      order=payment.order.number, method=payment.payment_method,
                      unknown_since=payment.unknown_since.isoformat(), last_error=payment.last_error)
    return escalated
