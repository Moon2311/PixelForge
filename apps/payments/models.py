import secrets

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.orders.models import Order


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

    STATUS_PENDING = "PENDING"
    STATUS_PROCESSING = "PROCESSING"
    STATUS_PAID = "PAID"
    STATUS_FAILED = "FAILED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_EXPIRED = "EXPIRED"
    STATUS_REFUNDED = "REFUNDED"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_PAID, "Paid"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_REFUNDED, "Refunded"),
    ]
    # Still waiting for the customer or the provider.
    ACTIVE_STATUSES = (STATUS_PENDING, STATUS_PROCESSING)

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

    class Meta:
        db_table = "apps_payment"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["order", "status"])]
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
                condition=Q(status__in=["PENDING", "PROCESSING"]),
                name="payment_one_active_per_order",
            ),
        ]

    def __str__(self):
        return f"Payment {self.payment_id} ({self.payment_method}, {self.status})"

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at
