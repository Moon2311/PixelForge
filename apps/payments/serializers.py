from rest_framework import serializers

from .models import Payment


class CreatePaymentSerializer(serializers.Serializer):
    order_id = serializers.CharField(max_length=20)
    payment_method = serializers.ChoiceField(choices=Payment.METHOD_CHOICES)

    def to_internal_value(self, data):
        if hasattr(data, "get") and isinstance(data.get("payment_method"), str):
            data = {**data, "payment_method": data["payment_method"].strip().upper()}
        return super().to_internal_value(data)


# Payment status -> what the storefront shows (``state``, ``status_message``).
STATES = {
    Payment.STATUS_PENDING: ("requires_action", "Complete your payment on the provider's page."),
    Payment.STATUS_PROCESSING: ("processing", "Payment processing. We're confirming it with the provider."),
    Payment.STATUS_UNKNOWN: (
        "pending_confirmation",
        "Payment pending confirmation. The provider hasn't confirmed it yet; please don't pay again. "
        "Your order will be updated automatically.",
    ),
    Payment.STATUS_PAID: ("succeeded", "Payment successful."),
    Payment.STATUS_FAILED: ("failed", "Payment failed."),
    Payment.STATUS_CANCELLED: ("failed", "The payment was cancelled."),
    Payment.STATUS_EXPIRED: ("failed", "The payment was not completed in time."),
    Payment.STATUS_REFUNDED: ("refunded", "This payment was refunded."),
}


class PaymentSerializer(serializers.ModelSerializer):
    """A payment as the shopper sees it. Amounts come from the database,
    never from the client.

    ``status`` is the stored status (unchanged contract); ``state`` and
    ``status_message`` are its storefront meaning; ``can_retry`` says
    whether ``POST /api/payments/<payment_id>/retry/`` would start a new
    attempt.
    """

    order_id = serializers.CharField(source="order.number", read_only=True)
    state = serializers.SerializerMethodField()
    status_message = serializers.SerializerMethodField()
    can_retry = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "payment_id", "order_id", "payment_method", "amount", "currency", "status",
            "failure_reason", "created_at", "expires_at", "paid_at",
            "state", "status_message", "can_retry",
        ]

    def get_state(self, payment):
        return STATES[payment.status][0]

    def get_status_message(self, payment):
        return STATES[payment.status][1]

    def get_can_retry(self, payment):
        from apps.orders.models import Order

        return (payment.status in (Payment.STATUS_FAILED, Payment.STATUS_CANCELLED, Payment.STATUS_EXPIRED)
                and payment.order.status == Order.STATUS_AWAITING_PAYMENT)
