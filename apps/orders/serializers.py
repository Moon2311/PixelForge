import re

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from rest_framework import serializers

from apps.payments.services import enabled_order_methods

from .models import Order
from .services import SHIPPING_METHODS

PHONE_RE = re.compile(r"^\+?[0-9][0-9\s()-]*$")


def validate_phone(value):
    """Lenient phone check: digits with optional +, spaces, dashes, brackets.

    Accepts local Pakistani numbers (0300 1234567) and international ones
    (+92 300 1234567).
    """
    digits = re.sub(r"\D", "", value)
    if not PHONE_RE.match(value) or not 10 <= len(digits) <= 15:
        raise serializers.ValidationError("Enter a valid phone number")
    return value.strip()


class AddressSerializer(serializers.Serializer):
    country = serializers.CharField(max_length=100)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    address1 = serializers.CharField(max_length=255)
    address2 = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    city = serializers.CharField(max_length=100)
    postal_code = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=30)

    def validate_phone(self, value):
        return validate_phone(value)


class OrderLineSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)


class PlaceOrderSerializer(serializers.Serializer):
    contact = serializers.CharField(max_length=254)
    marketing_opt_in = serializers.BooleanField(required=False, default=False)
    delivery_method = serializers.ChoiceField(choices=Order.DELIVERY_CHOICES)
    shipping_address = AddressSerializer(required=False, allow_null=True)
    shipping_method = serializers.CharField(required=False, allow_blank=True, default="")
    billing_same_as_shipping = serializers.BooleanField(required=False, default=True)
    billing_address = AddressSerializer(required=False, allow_null=True)
    payment_method = serializers.ChoiceField(choices=Order.PAYMENT_CHOICES)
    discount_code = serializers.CharField(required=False, allow_blank=True, default="")
    # What the shopper saw; compared against the cart and current prices.
    items = OrderLineSerializer(many=True)
    total = serializers.DecimalField(max_digits=12, decimal_places=2)

    def validate_contact(self, value):
        value = value.strip()
        if "@" in value:
            try:
                validate_email(value)
            except DjangoValidationError:
                raise serializers.ValidationError("Enter a valid email address")
            return value
        try:
            return validate_phone(value)
        except serializers.ValidationError:
            raise serializers.ValidationError("Enter a valid email or mobile phone number")

    def validate_payment_method(self, value):
        if value in Order.ONLINE_PAYMENT_METHODS and value not in enabled_order_methods():
            raise serializers.ValidationError("This payment method isn't available right now")
        return value

    def validate_discount_code(self, value):
        if value.strip():
            raise serializers.ValidationError("Discount codes aren't available yet")
        return ""

    def validate(self, data):
        errors = {}
        ship = data["delivery_method"] == Order.DELIVERY_SHIP
        if ship:
            if not data.get("shipping_address"):
                errors["shipping_address"] = ["Shipping address is required"]
            if data.get("shipping_method") not in SHIPPING_METHODS:
                errors["shipping_method"] = ["Choose a shipping method"]
        else:
            data["shipping_address"] = None
            data["shipping_method"] = ""
            # Nothing is shipped, so billing must be given explicitly.
            data["billing_same_as_shipping"] = False
        if not data["billing_same_as_shipping"] and not data.get("billing_address"):
            errors["billing_address"] = ["Billing address is required"]
        if not data["items"]:
            errors["items"] = ["Your cart is empty"]
        if errors:
            raise serializers.ValidationError(errors)
        if data["billing_same_as_shipping"]:
            data["billing_address"] = data["shipping_address"]
        return data


class OrderItemSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    sku = serializers.CharField()
    image_url = serializers.CharField()
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    quantity = serializers.IntegerField()
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2)


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "number", "status", "contact", "delivery_method", "shipping_address",
            "billing_address", "shipping_method", "payment_method", "subtotal",
            "shipping_total", "discount_total", "total", "items", "created_at",
        ]


class AdminOrderSerializer(OrderSerializer):
    """An order with its buyer; pass ``customers`` (from
    ``selectors.customers_by_id``) in the serializer context."""

    user_id = serializers.IntegerField(read_only=True)
    customer = serializers.SerializerMethodField()

    class Meta(OrderSerializer.Meta):
        fields = OrderSerializer.Meta.fields + ["user_id", "customer", "updated_at"]

    def get_customer(self, obj):
        return self.context["customers"].get(obj.user_id)
