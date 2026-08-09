"""Catalog serializers for the Product Catalog REST APIs.

These serializers handle validation, serialization, and deserialization
for all catalog models.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from apps.models import (
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
# Category / Subcategory
# ---------------------------------------------------------------------------


class SubcategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Subcategory
        fields = [
            "id", "name", "slug", "description", "image",
            "is_active", "display_order", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class SubcategoryListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subcategory
        fields = ["id", "name", "slug", "is_active", "display_order"]


class CategorySerializer(serializers.ModelSerializer):
    subcategories = SubcategorySerializer(many=True, read_only=True)
    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id", "name", "slug", "description", "image",
            "is_active", "display_order", "subcategories", "product_count",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_product_count(self, obj):
        if hasattr(obj, "products"):
            return obj.products.filter(is_deleted=False, status="active").count()
        return 0


class CategoryListSerializer(serializers.ModelSerializer):
    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id", "name", "slug", "is_active", "display_order", "product_count",
        ]

    def get_product_count(self, obj):
        if hasattr(obj, "products"):
            return obj.products.filter(is_deleted=False, status="active").count()
        return 0


# ---------------------------------------------------------------------------
# Brand
# ---------------------------------------------------------------------------


class BrandSerializer(serializers.ModelSerializer):
    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Brand
        fields = [
            "id", "name", "slug", "description", "logo", "website",
            "is_active", "product_count", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_product_count(self, obj):
        if hasattr(obj, "products"):
            return obj.products.filter(is_deleted=False, status="active").count()
        return 0


class BrandListSerializer(serializers.ModelSerializer):
    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Brand
        fields = ["id", "name", "slug", "is_active", "product_count"]

    def get_product_count(self, obj):
        if hasattr(obj, "products"):
            return obj.products.filter(is_deleted=False, status="active").count()
        return 0


# ---------------------------------------------------------------------------
# Product Image
# ---------------------------------------------------------------------------


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = [
            "id", "image_url", "alt_text", "is_primary", "display_order",
            "created_at",
        ]
        read_only_fields = ["created_at"]


# ---------------------------------------------------------------------------
# Attribute / AttributeValue
# ---------------------------------------------------------------------------


class AttributeValueSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeValue
        fields = ["id", "value"]
        read_only_fields = ["id"]


class AttributeSerializer(serializers.ModelSerializer):
    values = AttributeValueSerializer(many=True, read_only=True)

    class Meta:
        model = Attribute
        fields = ["id", "name", "slug", "values", "created_at"]
        read_only_fields = ["created_at"]


class AttributeListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Attribute
        fields = ["id", "name", "slug"]


class ProductAttributeValueSerializer(serializers.ModelSerializer):
    attribute_name = serializers.CharField(source="attribute_value.attribute.name", read_only=True)
    value = serializers.CharField(source="attribute_value.value", read_only=True)

    class Meta:
        model = ProductAttributeValue
        fields = ["id", "attribute_name", "value"]
        read_only_fields = ["id"]


class ProductAttributeValueCreateSerializer(serializers.Serializer):
    attribute_id = serializers.IntegerField()
    value = serializers.CharField(max_length=255)

    def validate(self, data):
        try:
            attribute = Attribute.objects.get(id=data["attribute_id"])
        except Attribute.DoesNotExist:
            raise serializers.ValidationError("Attribute not found")

        attr_value, _ = AttributeValue.objects.get_or_create(
            attribute=attribute, value=data["value"]
        )
        data["attribute_value"] = attr_value
        return data


class VariantAttributeValueSerializer(serializers.ModelSerializer):
    attribute_name = serializers.CharField(source="attribute_value.attribute.name", read_only=True)
    value = serializers.CharField(source="attribute_value.value", read_only=True)

    class Meta:
        model = VariantAttributeValue
        fields = ["id", "attribute_name", "value"]
        read_only_fields = ["id"]


class VariantAttributeValueCreateSerializer(serializers.Serializer):
    attribute_id = serializers.IntegerField()
    value = serializers.CharField(max_length=255)

    def validate(self, data):
        try:
            attribute = Attribute.objects.get(id=data["attribute_id"])
        except Attribute.DoesNotExist:
            raise serializers.ValidationError("Attribute not found")

        attr_value, _ = AttributeValue.objects.get_or_create(
            attribute=attribute, value=data["value"]
        )
        data["attribute_value"] = attr_value
        return data


# ---------------------------------------------------------------------------
# Product Specification
# ---------------------------------------------------------------------------


class ProductSpecificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductSpecification
        fields = [
            "id", "name", "value", "unit", "display_order",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


# ---------------------------------------------------------------------------
# Variant Price
# ---------------------------------------------------------------------------


class VariantPriceSerializer(serializers.ModelSerializer):
    current_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = VariantPrice
        fields = [
            "id", "regular_price", "sale_price", "currency",
            "effective_from", "effective_to", "is_active",
            "current_price", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, data):
        if data.get("sale_price") is not None and data.get("regular_price"):
            if data["sale_price"] >= data["regular_price"]:
                raise serializers.ValidationError(
                    "Sale price must be less than regular price"
                )

        effective_from = data.get("effective_from")
        effective_to = data.get("effective_to")
        if effective_from and effective_to:
            if effective_to <= effective_from:
                raise serializers.ValidationError(
                    "effective_to must be after effective_from"
                )

        return data


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


class InventorySerializer(serializers.ModelSerializer):
    available_quantity = serializers.IntegerField(read_only=True)
    is_in_stock = serializers.BooleanField(read_only=True)
    is_low_stock = serializers.BooleanField(read_only=True)
    is_out_of_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = Inventory
        fields = [
            "id", "stock_quantity", "reserved_quantity",
            "low_stock_threshold", "available_quantity",
            "is_in_stock", "is_low_stock", "is_out_of_stock",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]


class InventoryAdjustSerializer(serializers.Serializer):
    quantity = serializers.IntegerField()
    action = serializers.ChoiceField(
        choices=[
            ("stock_in", "Stock In"),
            ("stock_out", "Stock Out"),
            ("adjustment", "Adjustment"),
            ("reservation", "Reservation"),
            ("release", "Release"),
            ("return", "Return"),
        ]
    )
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_quantity(self, value):
        if value == 0:
            raise serializers.ValidationError("Quantity cannot be zero")
        return value


class InventoryLogSerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    variant_sku = serializers.CharField(source="variant.sku", read_only=True)

    class Meta:
        model = InventoryLog
        fields = [
            "id", "variant", "variant_sku", "previous_quantity", "new_quantity",
            "quantity_change", "action", "action_label", "reason",
            "actor_user_id", "created_at",
        ]
        read_only_fields = ["created_at"]


class LowStockAlertSerializer(serializers.ModelSerializer):
    variant_sku = serializers.CharField(source="variant.sku", read_only=True)

    class Meta:
        model = LowStockAlert
        fields = [
            "id", "variant", "variant_sku", "threshold", "quantity_at_alert",
            "status", "created_at", "resolved_at", "resolved_by",
        ]
        read_only_fields = ["created_at", "resolved_at", "resolved_by"]


# ---------------------------------------------------------------------------
# Product Variant
# ---------------------------------------------------------------------------


class ProductVariantSerializer(serializers.ModelSerializer):
    price = VariantPriceSerializer(read_only=True)
    inventory = InventorySerializer(read_only=True)
    attribute_values = VariantAttributeValueSerializer(many=True, read_only=True)

    class Meta:
        model = ProductVariant
        fields = [
            "id", "sku", "barcode", "name", "status",
            "price", "inventory", "attribute_values",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class ProductVariantListSerializer(serializers.ModelSerializer):
    price = VariantPriceSerializer(read_only=True)
    available_quantity = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = [
            "id", "sku", "barcode", "name", "status",
            "price", "available_quantity",
        ]

    def get_available_quantity(self, obj):
        if hasattr(obj, "inventory"):
            return obj.inventory.available_quantity
        return 0


class ProductVariantCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductVariant
        fields = ["sku", "barcode", "name", "status"]
        read_only_fields = ["created_at", "updated_at"]

    def validate_sku(self, value):
        if ProductVariant.objects.filter(sku=value).exists():
            raise serializers.ValidationError("A variant with this SKU already exists")
        return value


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------


class ProductListSerializer(serializers.ModelSerializer):
    brand_name = serializers.CharField(source="brand.name", read_only=True, default="")
    category_name = serializers.CharField(source="category.name", read_only=True, default="")
    primary_image = serializers.SerializerMethodField()
    min_price = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "sku", "name", "slug", "short_description",
            "brand", "brand_name", "category", "category_name",
            "status", "is_featured", "primary_image", "min_price",
            "created_at", "updated_at",
        ]

    def get_primary_image(self, obj):
        if hasattr(obj, "images"):
            primary = obj.images.filter(is_primary=True).first()
            if primary:
                return primary.image_url
            first = obj.images.first()
            return first.image_url if first else None
        return None

    def get_min_price(self, obj):
        if hasattr(obj, "variants"):
            prices = VariantPrice.objects.filter(
                variant__product=obj, is_active=True
            ).values_list("regular_price", flat=True)
            if prices:
                return min(prices)
        return None


class ProductDetailSerializer(serializers.ModelSerializer):
    brand_name = serializers.CharField(source="brand.name", read_only=True, default="")
    category_name = serializers.CharField(source="category.name", read_only=True, default="")
    subcategory_name = serializers.CharField(source="subcategory.name", read_only=True, default="")
    images = ProductImageSerializer(many=True, read_only=True)
    variants = ProductVariantSerializer(many=True, read_only=True)
    specifications = ProductSpecificationSerializer(many=True, read_only=True)
    attribute_values = ProductAttributeValueSerializer(many=True, read_only=True)
    reviews_count = serializers.SerializerMethodField()
    average_rating = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "sku", "name", "slug", "short_description", "description",
            "brand", "brand_name", "category", "category_name",
            "subcategory", "subcategory_name",
            "status", "is_featured",
            "images", "variants", "specifications", "attribute_values",
            "reviews_count", "average_rating",
            "created_by", "updated_by",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at", "created_by", "updated_by"]

    def get_reviews_count(self, obj):
        if hasattr(obj, "reviews"):
            return obj.reviews.filter(status="approved").count()
        return 0

    def get_average_rating(self, obj):
        if hasattr(obj, "reviews"):
            from django.db.models import Avg
            result = obj.reviews.filter(status="approved").aggregate(
                avg_rating=Avg("rating")
            )
            return result["avg_rating"]
        return None


class ProductCreateUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = [
            "sku", "name", "slug", "short_description", "description",
            "brand", "category", "subcategory",
            "status", "is_featured",
        ]

    def validate_sku(self, value):
        instance = self.instance
        if Product.objects.filter(sku=value).exclude(pk=getattr(instance, "pk", None)).exists():
            raise serializers.ValidationError("A product with this SKU already exists")
        return value

    def validate_slug(self, value):
        instance = self.instance
        if Product.objects.filter(slug=value).exclude(pk=getattr(instance, "pk", None)).exists():
            raise serializers.ValidationError("A product with this slug already exists")
        return value


# ---------------------------------------------------------------------------
# Product Review
# ---------------------------------------------------------------------------


class ProductReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductReview
        fields = [
            "id", "product", "user_id", "rating", "title", "comment",
            "status", "created_at", "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]

    def validate_rating(self, value):
        if value < 1 or value > 5:
            raise serializers.ValidationError("Rating must be between 1 and 5")
        return value


class ProductReviewCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductReview
        fields = ["rating", "title", "comment"]

    def validate_rating(self, value):
        if value < 1 or value > 5:
            raise serializers.ValidationError("Rating must be between 1 and 5")
        return value


class ProductReviewUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductReview
        fields = ["rating", "title", "comment", "status"]

    def validate_rating(self, value):
        if value < 1 or value > 5:
            raise serializers.ValidationError("Rating must be between 1 and 5")
        return value
