from decimal import Decimal

from rest_framework import serializers

from apps.catalog.models import (
    Attribute,
    AttributeValue,
    Brand,
    Category,
    Inventory,
    InventoryLog,
    LowStockAlert,
    Product,
    ProductAttributeValue,
    ProductImage,
    ProductReview,
    ProductSpecification,
    ProductVariant,
    Subcategory,
    VariantAttributeValue,
    VariantPrice,
)


# ---------------------------------------------------------------------------
# Legacy flat-endpoint serializers (kept for backward compatibility)
# ---------------------------------------------------------------------------


class CatalogSerializer(serializers.Serializer):
    """Base serializer for categories/brands/banners."""

    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    subtitle = serializers.CharField(required=False, allow_blank=True)
    image = serializers.CharField(required=False, allow_blank=True)
    logo = serializers.CharField(required=False, allow_blank=True)
    cta_text = serializers.CharField(required=False, allow_blank=True)
    cta_link = serializers.CharField(required=False, allow_blank=True)
    type = serializers.ChoiceField(
        choices=["hero", "promotion"], required=False, default="hero"
    )
    discount_badge = serializers.CharField(required=False, allow_blank=True)
    sort_order = serializers.IntegerField(required=False, default=0)
    is_active = serializers.BooleanField(required=False, default=True)
    start_at = serializers.CharField(required=False, allow_blank=True)
    end_at = serializers.CharField(required=False, allow_blank=True)


class ProductSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, error_messages={"required": "Product name is required"})
    brand_name = serializers.CharField(max_length=255, error_messages={"required": "Brand is required"})
    category_name = serializers.CharField(max_length=255, error_messages={"required": "Category is required"})
    sku = serializers.CharField(max_length=100, error_messages={"required": "SKU is required"})

    short_description = serializers.CharField(required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    specifications = serializers.CharField(required=False, allow_blank=True)

    price = serializers.FloatField(required=True, error_messages={"required": "Price is required"})
    discount_price = serializers.FloatField(required=False, allow_null=True)
    cost_price = serializers.FloatField(required=False, allow_null=True)
    stock_quantity = serializers.IntegerField(required=False)
    min_stock_alert = serializers.IntegerField(required=False)
    weight = serializers.FloatField(required=False, allow_null=True)

    status = serializers.ChoiceField(choices=["active", "inactive", "draft"], required=False)
    is_featured = serializers.BooleanField(required=False)

    total_sales = serializers.IntegerField(required=False)
    flash_sale = serializers.BooleanField(required=False)
    flash_sale_price = serializers.FloatField(required=False, allow_null=True)
    flash_sale_ends_at = serializers.CharField(required=False, allow_blank=True)

    color = serializers.CharField(required=False, allow_blank=True)
    size = serializers.CharField(required=False, allow_blank=True)
    tags = serializers.CharField(required=False, allow_blank=True)

    category_id = serializers.IntegerField(required=False)
    brand_id = serializers.IntegerField(required=False)

    def _non_negative(self, name, value):
        if value is not None and value < 0:
            raise serializers.ValidationError(f"{name.replace('_', ' ').title()} cannot be negative")
        return value

    def validate_price(self, value):
        if value is None:
            raise serializers.ValidationError("Price must be greater than zero")
        if value <= 0:
            raise serializers.ValidationError("Price must be greater than zero")
        return value

    def validate_discount_price(self, value):
        return self._non_negative("discount_price", value)

    def validate_cost_price(self, value):
        return self._non_negative("cost_price", value)

    def validate_stock_quantity(self, value):
        return self._non_negative("stock_quantity", value)

    def validate_min_stock_alert(self, value):
        return self._non_negative("min_stock_alert", value)

    def validate_weight(self, value):
        return self._non_negative("weight", value)


class StockUpdateSerializer(serializers.Serializer):
    stock_quantity = serializers.IntegerField(required=False)
    delta = serializers.IntegerField(required=False)
    min_stock_alert = serializers.IntegerField(required=False)
    note = serializers.CharField(required=False, allow_blank=True)

    def validate_stock_quantity(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Stock cannot be negative")
        return value

    def validate_min_stock_alert(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Minimum stock alert cannot be negative")
        return value

    def validate(self, data):
        if "stock_quantity" not in data and "delta" not in data:
            raise serializers.ValidationError(
                "Provide either stock_quantity (absolute) or delta (signed change)"
            )
        return data


class InventoryLogSerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = InventoryLog
        fields = [
            "id",
            "product_id",
            "product_name",
            "sku",
            "action",
            "action_label",
            "quantity_before",
            "quantity_after",
            "quantity_change",
            "note",
            "actor",
            "created_at",
        ]


class LowStockAlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = LowStockAlert
        fields = [
            "id",
            "product_id",
            "product_name",
            "sku",
            "quantity",
            "min_stock_alert",
            "resolved",
            "created_at",
            "resolved_at",
        ]


# ---------------------------------------------------------------------------
# New PostgreSQL-backed serializers
# ---------------------------------------------------------------------------


# -- Category / Subcategory --


class SubcategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Subcategory
        fields = ["id", "name", "slug", "description", "image", "is_active", "display_order"]


class CategorySerializer(serializers.ModelSerializer):
    subcategories = SubcategorySerializer(many=True, read_only=True)

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "description", "image", "is_active", "display_order", "subcategories"]


# -- Brand --


class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = ["id", "name", "slug", "description", "logo", "website", "is_active"]


# -- Product Image --


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ["id", "image_url", "alt_text", "is_primary", "display_order"]


# -- Attribute / AttributeValue --


class AttributeValueSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeValue
        fields = ["id", "value"]


class AttributeSerializer(serializers.ModelSerializer):
    values = AttributeValueSerializer(many=True, read_only=True)

    class Meta:
        model = Attribute
        fields = ["id", "name", "slug", "values"]


class ProductAttributeValueSerializer(serializers.ModelSerializer):
    attribute_name = serializers.CharField(source="attribute_value.attribute.name", read_only=True)
    value = serializers.CharField(source="attribute_value.value", read_only=True)

    class Meta:
        model = ProductAttributeValue
        fields = ["id", "attribute_name", "value"]


class VariantAttributeValueSerializer(serializers.ModelSerializer):
    attribute_name = serializers.CharField(source="attribute_value.attribute.name", read_only=True)
    value = serializers.CharField(source="attribute_value.value", read_only=True)

    class Meta:
        model = VariantAttributeValue
        fields = ["id", "attribute_name", "value"]


# -- Specification --


class ProductSpecificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductSpecification
        fields = ["id", "name", "value", "unit", "display_order"]


# -- Variant Price --


class VariantPriceSerializer(serializers.ModelSerializer):
    current_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = VariantPrice
        fields = [
            "id", "regular_price", "sale_price", "currency",
            "effective_from", "effective_to", "is_active", "current_price",
        ]


# -- Inventory --


class InventorySerializer(serializers.ModelSerializer):
    available_quantity = serializers.IntegerField(read_only=True)
    is_in_stock = serializers.BooleanField(read_only=True)
    is_low_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = Inventory
        fields = [
            "id", "stock_quantity", "reserved_quantity",
            "low_stock_threshold", "available_quantity", "is_in_stock", "is_low_stock",
        ]


# -- Inventory Log (new) --


class NewInventoryLogSerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    variant_sku = serializers.CharField(source="variant.sku", read_only=True)

    class Meta:
        model = InventoryLog
        fields = [
            "id", "variant", "variant_sku", "previous_quantity", "new_quantity",
            "quantity_change", "action", "action_label", "reason", "actor_user_id",
            "created_at",
        ]


# -- Low Stock Alert (new) --


class NewLowStockAlertSerializer(serializers.ModelSerializer):
    variant_sku = serializers.CharField(source="variant.sku", read_only=True)

    class Meta:
        model = LowStockAlert
        fields = [
            "id", "variant", "variant_sku", "threshold", "quantity_at_alert",
            "status", "created_at", "resolved_at", "resolved_by",
        ]


# -- Product Variant --


class ProductVariantSerializer(serializers.ModelSerializer):
    price = VariantPriceSerializer(read_only=True)
    inventory = InventorySerializer(read_only=True)
    attribute_values = VariantAttributeValueSerializer(many=True, read_only=True)

    class Meta:
        model = ProductVariant
        fields = [
            "id", "sku", "barcode", "name", "status",
            "price", "inventory", "attribute_values",
        ]


# -- Product (full) --


class ProductDetailSerializer(serializers.ModelSerializer):
    brand_name = serializers.CharField(source="brand.name", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    subcategory_name = serializers.CharField(source="subcategory.name", read_only=True, default="")
    images = ProductImageSerializer(many=True, read_only=True)
    variants = ProductVariantSerializer(many=True, read_only=True)
    specifications = ProductSpecificationSerializer(many=True, read_only=True)
    attribute_values = ProductAttributeValueSerializer(many=True, read_only=True)
    reviews_count = serializers.IntegerField(read_only=True, default=0)
    average_rating = serializers.DecimalField(max_digits=3, decimal_places=2, read_only=True, default=0)

    class Meta:
        model = Product
        fields = [
            "id", "sku", "name", "slug", "short_description", "description",
            "brand", "brand_name", "category", "category_name",
            "subcategory", "subcategory_name",
            "status", "is_featured",
            "images", "variants", "specifications", "attribute_values",
            "reviews_count", "average_rating",
            "created_at", "updated_at", "created_by", "updated_by",
        ]


class ProductListSerializer(serializers.ModelSerializer):
    brand_name = serializers.CharField(source="brand.name", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    primary_image = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "sku", "name", "slug", "short_description",
            "brand", "brand_name", "category", "category_name",
            "status", "is_featured", "primary_image",
            "created_at", "updated_at",
        ]

    def get_primary_image(self, obj):
        primary = obj.images.filter(is_primary=True).first()
        if primary:
            return primary.image_url
        first = obj.images.first()
        return first.image_url if first else None


# -- Review --


class ProductReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductReview
        fields = [
            "id", "product", "user_id", "rating", "title", "comment",
            "status", "created_at", "updated_at",
        ]
        read_only_fields = ["status"]
