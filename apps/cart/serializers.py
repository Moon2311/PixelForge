from rest_framework import serializers


class CartItemSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    product_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class CartItemUpdateSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1)


class CartResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    user_id = serializers.IntegerField()
    items = serializers.ListField(child=serializers.DictField())
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class GuestCartItemSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    product_id = serializers.IntegerField()
    variant_id = serializers.IntegerField(required=False, allow_null=True)
    product_name = serializers.CharField(max_length=255)
    product_image_url = serializers.URLField(required=False, allow_blank=True)
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    quantity = serializers.IntegerField(min_value=1)
    line_total = serializers.SerializerMethodField()

    def get_line_total(self, obj):
        return str(obj.unit_price * obj.quantity)


class GuestCartItemCreateSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    variant_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    product_name = serializers.CharField(max_length=255)
    product_image_url = serializers.URLField(required=False, allow_blank=True, default="")
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    quantity = serializers.IntegerField(min_value=1, default=1)


class GuestCartItemUpdateSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=0)


class GuestCartSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    session_id = serializers.CharField()
    items = GuestCartItemSerializer(many=True, read_only=True)
    total = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()
    expires_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def get_total(self, obj):
        return str(sum(item.unit_price * item.quantity for item in obj.items.all()))

    def get_item_count(self, obj):
        return obj.items.count()
