import re
import secrets

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.orders.models import Order


# Our payment reference: "PY" + 16 upper-case hex digits.
PAYMENT_ID_RE = re.compile(r"^PY[0-9A-F]{16}$")


def new_payment_id():
    """Our reference for one payment attempt, sent to the provider as its
    transaction/order reference. Letters and digits only, 18 characters: it
    fits JazzCash's ``pp_TxnRefNo`` (20 AN) and Easypaisa's ``orderRefNum``."""
    return f"PY{secrets.token_hex(8).upper()}"


class Payment(models.Model):
    """One attempt to pay an order online (JazzCash or Easypaisa).

    The order number stays the business identifier; every attempt gets its
    own ``payment_id``, and the provider's reference for the money movement
    is kept in ``provider_transaction_id``. Only the server-side verification
    of a provider response changes ``status``.
    """

    METHOD_JAZZCASH = "JAZZCASH"
    METHOD_EASYPAISA = "EASYPAISA"
    METHOD_CHOICES = [(METHOD_JAZZCASH, "JazzCash"), (METHOD_EASYPAISA, "Easypaisa")]
    # Payment method -> the matching ``Order.payment_method`` value.
    ORDER_METHODS = {
        METHOD_JAZZCASH: Order.PAYMENT_JAZZCASH,
        METHOD_EASYPAISA: Order.PAYMENT_EASYPAISA,
    }

    # PENDING: the checkout is open; the customer still has to pay on the
    # provider's page ("requires action").
    STATUS_PENDING = "PENDING"
    # The provider reported the payment in progress, or told us it happened
    # and we are confirming it with its inquiry API.
    STATUS_PROCESSING = "PROCESSING"
    # The provider may have charged the customer but couldn't confirm it
    # (inquiry timed out, gateway down). Never failed or charged again
    # automatically: reconciliation asks the provider until it answers.
    STATUS_UNKNOWN = "UNKNOWN"
    STATUS_PAID = "PAID"
    STATUS_FAILED = "FAILED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_EXPIRED = "EXPIRED"
    STATUS_REFUNDED = "REFUNDED"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_UNKNOWN, "Unknown (awaiting confirmation)"),
        (STATUS_PAID, "Paid"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_REFUNDED, "Refunded"),
    ]
    # Still waiting for the customer or the provider.
    ACTIVE_STATUSES = (STATUS_PENDING, STATUS_PROCESSING, STATUS_UNKNOWN)
    FINAL_STATUSES = (STATUS_PAID, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED, STATUS_REFUNDED)

    payment_id = models.CharField(max_length=20, unique=True, default=new_payment_id, editable=False)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payments")
    payment_method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    provider_transaction_id = models.CharField(max_length=64, blank=True, default="")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="PKR")
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    # Provider request details and the last verified provider response
    # (never credentials, hashes or card data).
    metadata = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    # Client-chosen key of the "Pay Now" request (Idempotency-Key header):
    # repeating it returns this payment instead of starting another.
    idempotency_key = models.CharField(max_length=64, blank=True, default="")
    # Provider status inquiries made for this payment, and the last one
    # that got an answer.
    check_attempts = models.PositiveIntegerField(default=0)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    # No new verification request before this time (throttles polling).
    next_check_at = models.DateTimeField(null=True, blank=True)
    # The last failed inquiry, sanitized (never credentials or responses).
    last_error = models.CharField(max_length=255, blank=True, default="")
    # When the payment became UNKNOWN, and when that was escalated.
    unknown_since = models.DateTimeField(null=True, blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "apps_payment"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["order", "status"]),
            # The reconciliation job's queue.
            models.Index(
                fields=["status", "last_checked_at"], name="payment_open_checked_idx",
                condition=Q(status__in=["PENDING", "PROCESSING", "UNKNOWN"]),
            ),
        ]
        constraints = [
            # One provider transaction can only ever settle one payment.
            models.UniqueConstraint(
                fields=["payment_method", "provider_transaction_id"],
                condition=~Q(provider_transaction_id=""),
                name="payment_unique_provider_transaction",
            ),
            # Only one attempt per order can be in flight at a time, so a
            # double-clicked "Pay Now" can't open two provider checkouts.
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(status__in=["PENDING", "PROCESSING", "UNKNOWN"]),
                name="payment_one_active_per_order",
            ),
            models.UniqueConstraint(
                fields=["order", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="payment_unique_idempotency_key",
            ),
            # PAID always records when; only a PAID or REFUNDED payment has it.
            models.CheckConstraint(
                condition=(Q(status="PAID", paid_at__isnull=False) | ~Q(status="PAID")),
                name="payment_paid_has_paid_at",
            ),
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_positive"),
        ]

    def __str__(self):
        return f"Payment {self.payment_id} ({self.payment_method}, {self.status})"

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_final(self):
        return self.status in self.FINAL_STATUSES


class PaymentNotification(models.Model):
    """A provider notification (browser return or IPN) that named one of our
    payments, recorded once per distinct content.

    Providers retry notifications and browsers get refreshed: a repeated
    delivery only bumps ``receive_count``. A notification never changes a
    payment's status on its own (the inquiry API is the authority); it is
    kept for audit and duplicate detection. Only fields the provider sent,
    without hashes or credentials (``base.redact``).
    """

    SOURCE_RETURN = "return"
    SOURCE_IPN = "ipn"
    SOURCE_CHOICES = [(SOURCE_RETURN, "Browser return"), (SOURCE_IPN, "IPN")]

    provider = models.CharField(max_length=10, choices=Payment.METHOD_CHOICES)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="notifications")
    # SHA-256 of the notification's content: the duplicate-delivery key.
    dedupe_key = models.CharField(max_length=64)
    # JazzCash: pp_SecureHash checked. Easypaisa notifications aren't signed.
    signature_valid = models.BooleanField(default=False)
    reported_status = models.CharField(max_length=10, blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    receive_count = models.PositiveIntegerField(default=1)
    first_received_at = models.DateTimeField(default=timezone.now)
    last_received_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "apps_payment_notification"
        ordering = ["-first_received_at"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "dedupe_key"], name="payment_notification_unique"),
        ]

    def __str__(self):
        return f"{self.provider} {self.source} for {self.payment_id} (x{self.receive_count})"


class GatewayCircuit(models.Model):
    """Circuit-breaker state of one provider's API, shared by every process
    (web instances, payment workers, reconciler) through PostgreSQL.

    CLOSED: calls go through; consecutive failures are counted.
    OPEN: calls fail fast until ``opened_at`` + recovery time.
    HALF_OPEN: one probe call at a time (``probe_until`` is its lease);
    enough successes close the circuit, a failure opens it again.
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
    STATE_CHOICES = [(CLOSED, "Closed"), (OPEN, "Open"), (HALF_OPEN, "Half open")]

    provider = models.CharField(max_length=10, primary_key=True, choices=Payment.METHOD_CHOICES)
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default=CLOSED)
    consecutive_failures = models.PositiveIntegerField(default=0)
    half_open_successes = models.PositiveIntegerField(default=0)
    opened_at = models.DateTimeField(null=True, blank=True)
    probe_until = models.DateTimeField(null=True, blank=True)
    last_failure = models.CharField(max_length=255, blank=True, default="")
    last_failure_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "apps_payment_gateway_circuit"

    def __str__(self):
        return f"{self.provider}: {self.state}"
