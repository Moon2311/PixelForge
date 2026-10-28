"""Payment verification (consumer group ``payments``, topic
``payment-processing``; ``kafka_worker payments``).

One message = "ask the provider what happened to payment X". The task is
safe to run any number of times for the same payment: the provider call is
a read-only status inquiry, and ``apply_result`` changes the payment and its
order under row locks only when the answer moves it forward (one PAID, one
order confirmation, one ``payment.succeeded`` event).

Failures, classified by ``services.base``:

* gateway unavailable, timeout, rate limited, circuit open: retried through
  the retry topic with exponential backoff and jitter, never sooner than the
  circuit's recovery time (``retry_after``); the consumer never sleeps.
  After PAYMENT_VERIFY_MAX_ATTEMPTS the message is dead-lettered and the
  payment stays open (UNKNOWN if the provider had reported it):
  ``payments_reconcile`` asks again once the provider is back.
* bad answers (invalid signature, error codes): retried, without tripping
  the circuit.
* authentication/configuration errors, malformed messages, unknown
  payments: dead-lettered at once (retrying can't fix them).
"""

from django.conf import settings

from apps.events.registry import task
from apps.events.retry import NonRetryableError

from .models import PAYMENT_ID_RE, Payment
from .services import VERIFICATION_REQUESTED, run_verification


@task(
    "payments", VERIFICATION_REQUESTED, name="payments.verify",
    max_attempts=max(1, settings.PAYMENT_VERIFY_MAX_ATTEMPTS),
    base_delay=settings.PAYMENT_VERIFY_RETRY_BASE_DELAY,
    max_delay=settings.PAYMENT_VERIFY_RETRY_MAX_DELAY,
    # The inquiry's own timeout plus time for the database work.
    timeout=settings.PAYMENT_PROVIDER_TIMEOUT + 15,
)
def verify_payment(event):
    data = event.payload
    payment_id = data.get("payment_id")
    if not isinstance(payment_id, str) or not PAYMENT_ID_RE.match(payment_id):
        raise NonRetryableError("verification request without a valid payment_id")
    order_number = data.get("order_number")
    payment = Payment.objects.select_related("order").filter(payment_id=payment_id).first()
    if payment is None:
        # The request commits with the payment, so this can't be lag.
        raise NonRetryableError(f"payment {payment_id} not found")
    if order_number is not None and payment.order.number != order_number:
        raise NonRetryableError(f"payment {payment_id} doesn't belong to order {order_number}")
    reason = str(data.get("reason") or "")[:30]
    run_verification(payment_id, reason=reason, include_closed=bool(data.get("include_closed")))
