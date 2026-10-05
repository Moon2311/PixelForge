from rest_framework import serializers

from .models import Payment


class CreatePaymentSerializer(serializers.Serializer):
    order_id = serializers.CharField(max_length=20)
    payment_method = serializers.ChoiceField(choices=Payment.METHOD_CHOICES)

    def to_internal_value(self, data):
        if hasattr(data, "get") and isinstance(data.get("payment_method"), str):
            data = {**data, "payment_method": data["payment_method"].strip().upper()}
        return super().to_internal_value(data)


class PaymentSerializer(serializers.ModelSerializer):
    """A payment as the shopper sees it. Amounts come from the database,
    never from the client."""

    order_id = serializers.CharField(source="order.number", read_only=True)

    class Meta:
        model = Payment
        fields = [
            "payment_id", "order_id", "payment_method", "amount", "currency", "status",
            "failure_reason", "created_at", "expires_at", "paid_at",
        ]
