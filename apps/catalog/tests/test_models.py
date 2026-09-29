"""Database and model integration tests.

Tests cover:
    - Product creation and constraints
    - Category/Subcategory/Brand relationships
    - Unique SKU and slug constraints
    - Variant creation
    - Attributes and specifications
    - Pricing
    - Inventory and constraints
    - Reviews
    - Foreign key behavior
    - Soft delete
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

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
# Helpers
# ---------------------------------------------------------------------------


class ModelTestHelpers:
    """Shared factory methods for creating test data."""

    @staticmethod
    def create_category(**kwargs):
        defaults = {
            "name": "Test Category",
            "slug": "test-category",
            "description": "A test category",
            "is_active": True,
            "display_order": 0,
        }
        defaults.update(kwargs)
        return Category.objects.create(**defaults)

    @staticmethod
    def create_subcategory(category=None, **kwargs):
        if category is None:
            category = ModelTestHelpers.create_category()
        defaults = {
            "category": category,
            "name": "Test Subcategory",
            "slug": "test-subcategory",
            "description": "A test subcategory",
            "is_active": True,
            "display_order": 0,
        }
        defaults.update(kwargs)
        return Subcategory.objects.create(**defaults)

    @staticmethod
    def create_brand(**kwargs):
        defaults = {
            "name": "Test Brand",
            "slug": "test-brand",
            "description": "A test brand",
            "is_active": True,
        }
        defaults.update(kwargs)
        return Brand.objects.create(**defaults)

    @staticmethod
    def create_product(category=None, brand=None, subcategory=None, **kwargs):
        if category is None:
            category = ModelTestHelpers.create_category()
        if brand is None:
            brand = ModelTestHelpers.create_brand()
        defaults = {
            "sku": "TEST-001",
            "name": "Test Product",
            "slug": "test-product",
            "description": "A test product",
            "short_description": "Short desc",
            "status": "active",
            "is_featured": False,
            "brand": brand,
            "category": category,
            "subcategory": subcategory,
        }
        defaults.update(kwargs)
        return Product.objects.create(**defaults)

    @staticmethod
    def create_variant(product=None, **kwargs):
        if product is None:
            product = ModelTestHelpers.create_product()
        defaults = {
            "sku": "VAR-001",
            "name": "Test Variant",
            "status": "active",
        }
        defaults.update(kwargs)
        return ProductVariant.objects.create(product=product, **defaults)

    @staticmethod
    def create_attribute(**kwargs):
        defaults = {
            "name": "Color",
            "slug": "color",
        }
        defaults.update(kwargs)
        return Attribute.objects.create(**defaults)

    @staticmethod
    def create_attribute_value(attribute=None, **kwargs):
        if attribute is None:
            attribute = ModelTestHelpers.create_attribute()
        defaults = {
            "value": "Red",
        }
        defaults.update(kwargs)
        return AttributeValue.objects.create(attribute=attribute, **defaults)

    @staticmethod
    def create_price(variant=None, **kwargs):
        if variant is None:
            variant = ModelTestHelpers.create_variant()
        defaults = {
            "regular_price": Decimal("99.99"),
            "sale_price": Decimal("79.99"),
            "currency": "USD",
            "is_active": True,
        }
        defaults.update(kwargs)
        return VariantPrice.objects.create(variant=variant, **defaults)

    @staticmethod
    def create_inventory(variant=None, **kwargs):
        if variant is None:
            variant = ModelTestHelpers.create_variant()
        defaults = {
            "stock_quantity": 100,
            "reserved_quantity": 0,
            "low_stock_threshold": 10,
        }
        defaults.update(kwargs)
        return Inventory.objects.create(variant=variant, **defaults)


# ---------------------------------------------------------------------------
# Category Tests
# ---------------------------------------------------------------------------


class CategoryModelTest(TestCase, ModelTestHelpers):
    """Test Category model creation and constraints."""

    def test_create_category(self):
        category = self.create_category(name="Electronics", slug="electronics")
        self.assertEqual(category.name, "Electronics")
        self.assertEqual(category.slug, "electronics")
        self.assertTrue(category.is_active)
        self.assertFalse(category.is_deleted)

    def test_category_unique_name(self):
        self.create_category(name="Electronics", slug="electronics")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_category(name="Electronics", slug="electronics-2")

    def test_category_unique_slug(self):
        self.create_category(name="Electronics", slug="electronics")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_category(name="Electronics 2", slug="electronics")

    def test_category_soft_delete(self):
        category = self.create_category()
        category.soft_delete()
        self.assertTrue(category.is_deleted)
        self.assertIsNotNone(category.deleted_at)

    def test_category_str(self):
        category = self.create_category(name="Electronics")
        self.assertEqual(str(category), "Electronics")


# ---------------------------------------------------------------------------
# Subcategory Tests
# ---------------------------------------------------------------------------


class SubcategoryModelTest(TestCase, ModelTestHelpers):
    """Test Subcategory model and relationships."""

    def test_create_subcategory(self):
        category = self.create_category(name="Electronics", slug="electronics")
        sub = self.create_subcategory(
            category=category, name="Phones", slug="phones"
        )
        self.assertEqual(sub.category, category)
        self.assertEqual(sub.name, "Phones")

    def test_subcategory_cascade_delete(self):
        category = self.create_category()
        sub = self.create_subcategory(category=category)
        sub_id = sub.pk
        category.delete()
        self.assertFalse(Subcategory.objects.filter(pk=sub_id).exists())

    def test_subcategory_unique_together(self):
        category = self.create_category()
        self.create_subcategory(category=category, name="Phones", slug="phones")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_subcategory(
                    category=category, name="Phones", slug="phones-2"
                )


# ---------------------------------------------------------------------------
# Brand Tests
# ---------------------------------------------------------------------------


class BrandModelTest(TestCase, ModelTestHelpers):
    """Test Brand model creation and constraints."""

    def test_create_brand(self):
        brand = self.create_brand(name="Apple", slug="apple")
        self.assertEqual(brand.name, "Apple")
        self.assertTrue(brand.is_active)

    def test_brand_unique_name(self):
        self.create_brand(name="Apple", slug="apple")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_brand(name="Apple", slug="apple-2")

    def test_brand_unique_slug(self):
        self.create_brand(name="Apple", slug="apple")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_brand(name="Apple 2", slug="apple")


# ---------------------------------------------------------------------------
# Product Tests
# ---------------------------------------------------------------------------


class ProductModelTest(TestCase, ModelTestHelpers):
    """Test Product model creation and constraints."""

    def test_create_product(self):
        product = self.create_product(sku="IPHONE-15", name="iPhone 15")
        self.assertEqual(product.sku, "IPHONE-15")
        self.assertEqual(product.status, "active")
        self.assertFalse(product.is_deleted)

    def test_product_unique_sku(self):
        self.create_product(sku="IPHONE-15")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_product(sku="IPHONE-15", name="iPhone 15 v2")

    def test_product_unique_slug(self):
        self.create_product(slug="iphone-15")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_product(slug="iphone-15", name="iPhone 15 v2")

    def test_product_brand_relationship(self):
        brand = self.create_brand(name="Apple", slug="apple")
        product = self.create_product(brand=brand)
        self.assertEqual(product.brand, brand)

    def test_product_category_relationship(self):
        category = self.create_category(name="Phones", slug="phones")
        product = self.create_product(category=category)
        self.assertEqual(product.category, category)

    def test_product_soft_delete(self):
        product = self.create_product()
        product.soft_delete()
        self.assertTrue(product.is_deleted)
        self.assertIsNotNone(product.deleted_at)

    def test_product_status_choices(self):
        for i, status in enumerate(["draft", "active", "inactive", "archived"]):
            product = self.create_product(
                sku=f"PROD-{status.upper()}",
                slug=f"prod-{status}",
                status=status,
                category=self.create_category(
                    name=f"Category-{i}", slug=f"category-{i}"
                ),
                brand=self.create_brand(
                    name=f"Brand-{i}", slug=f"brand-{i}"
                ),
            )
            self.assertEqual(product.status, status)

    def test_product_str(self):
        product = self.create_product(sku="IPHONE-15", name="iPhone 15")
        self.assertIn("IPHONE-15", str(product))


# ---------------------------------------------------------------------------
# Variant Tests
# ---------------------------------------------------------------------------


class ProductVariantModelTest(TestCase, ModelTestHelpers):
    """Test ProductVariant model creation and constraints."""

    def test_create_variant(self):
        variant = self.create_variant(sku="VAR-001", name="128GB")
        self.assertEqual(variant.sku, "VAR-001")
        self.assertEqual(variant.status, "active")

    def test_variant_unique_sku(self):
        self.create_variant(sku="VAR-001")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_variant(sku="VAR-001")

    def test_variant_cascade_delete(self):
        product = self.create_product()
        variant = self.create_variant(product=product)
        variant_id = variant.pk
        product.delete()
        self.assertFalse(ProductVariant.objects.filter(pk=variant_id).exists())


# ---------------------------------------------------------------------------
# Attribute Tests
# ---------------------------------------------------------------------------


class AttributeModelTest(TestCase, ModelTestHelpers):
    """Test Attribute and AttributeValue models."""

    def test_create_attribute(self):
        attr = self.create_attribute(name="Color", slug="color")
        self.assertEqual(attr.name, "Color")

    def test_create_attribute_value(self):
        attr = self.create_attribute(name="Color", slug="color")
        value = self.create_attribute_value(attribute=attr, value="Red")
        self.assertEqual(value.attribute, attr)
        self.assertEqual(value.value, "Red")

    def test_attribute_value_unique_together(self):
        attr = self.create_attribute()
        self.create_attribute_value(attribute=attr, value="Red")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_attribute_value(attribute=attr, value="Red")

    def test_product_attribute_value(self):
        product = self.create_product()
        attr = self.create_attribute()
        value = self.create_attribute_value(attribute=attr)
        pav = ProductAttributeValue.objects.create(
            product=product, attribute_value=value
        )
        self.assertEqual(pav.product, product)
        self.assertEqual(pav.attribute_value, value)

    def test_variant_attribute_value(self):
        variant = self.create_variant()
        attr = self.create_attribute()
        value = self.create_attribute_value(attribute=attr)
        vav = VariantAttributeValue.objects.create(
            variant=variant, attribute_value=value
        )
        self.assertEqual(vav.variant, variant)


# ---------------------------------------------------------------------------
# Specification Tests
# ---------------------------------------------------------------------------


class ProductSpecificationModelTest(TestCase, ModelTestHelpers):
    """Test ProductSpecification model."""

    def test_create_specification(self):
        product = self.create_product()
        spec = ProductSpecification.objects.create(
            product=product,
            name="Weight",
            value="172",
            unit="grams",
            display_order=1,
        )
        self.assertEqual(spec.product, product)
        self.assertEqual(spec.name, "Weight")

    def test_specification_unique_together(self):
        product = self.create_product()
        ProductSpecification.objects.create(
            product=product, name="Weight", value="172"
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductSpecification.objects.create(
                    product=product, name="Weight", value="180"
                )


# ---------------------------------------------------------------------------
# Pricing Tests
# ---------------------------------------------------------------------------


class VariantPriceModelTest(TestCase, ModelTestHelpers):
    """Test VariantPrice model."""

    def test_create_price(self):
        price = self.create_price(
            regular_price=Decimal("99.99"),
            sale_price=Decimal("79.99"),
        )
        self.assertEqual(price.regular_price, Decimal("99.99"))
        self.assertEqual(price.current_price, Decimal("79.99"))

    def test_price_without_sale(self):
        price = self.create_price(sale_price=None)
        self.assertEqual(price.current_price, price.regular_price)

    def test_price_one_to_one(self):
        variant = self.create_variant()
        self.create_price(variant=variant)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_price(variant=variant)


# ---------------------------------------------------------------------------
# Inventory Tests
# ---------------------------------------------------------------------------


class InventoryModelTest(TestCase, ModelTestHelpers):
    """Test Inventory model and constraints."""

    def test_create_inventory(self):
        inv = self.create_inventory(stock_quantity=100, reserved_quantity=10)
        self.assertEqual(inv.stock_quantity, 100)
        self.assertEqual(inv.reserved_quantity, 10)
        self.assertEqual(inv.available_quantity, 90)

    def test_available_quantity(self):
        inv = self.create_inventory(stock_quantity=50, reserved_quantity=20)
        self.assertEqual(inv.available_quantity, 30)

    def test_available_quantity_floor(self):
        inv = self.create_inventory(stock_quantity=5, reserved_quantity=10)
        self.assertEqual(inv.available_quantity, 0)

    def test_is_in_stock(self):
        inv = self.create_inventory(stock_quantity=10)
        self.assertTrue(inv.is_in_stock)

    def test_is_out_of_stock(self):
        inv = self.create_inventory(stock_quantity=0)
        self.assertTrue(inv.is_out_of_stock)

    def test_is_low_stock(self):
        inv = self.create_inventory(stock_quantity=5, low_stock_threshold=10)
        self.assertTrue(inv.is_low_stock)

    def test_not_low_stock(self):
        inv = self.create_inventory(stock_quantity=50, low_stock_threshold=10)
        self.assertFalse(inv.is_low_stock)

    def test_inventory_one_to_one(self):
        variant = self.create_variant()
        self.create_inventory(variant=variant)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_inventory(variant=variant)


# ---------------------------------------------------------------------------
# Inventory Log Tests
# ---------------------------------------------------------------------------


class InventoryLogModelTest(TestCase, ModelTestHelpers):
    """Test InventoryLog model."""

    def test_create_log(self):
        variant = self.create_variant()
        log = InventoryLog.objects.create(
            variant=variant,
            previous_quantity=100,
            new_quantity=90,
            quantity_change=-10,
            action="stock_out",
            reason="Sold",
            actor_user_id=1,
        )
        self.assertEqual(log.variant, variant)
        self.assertEqual(log.quantity_change, -10)

    def test_log_action_choices(self):
        variant = self.create_variant()
        for action in [
            "stock_in", "stock_out", "adjustment", "reservation",
            "release", "sale", "return", "create", "update", "delete",
        ]:
            log = InventoryLog.objects.create(
                variant=variant,
                action=action,
                quantity_change=0,
            )
            self.assertEqual(log.action, action)


# ---------------------------------------------------------------------------
# Low Stock Alert Tests
# ---------------------------------------------------------------------------


class LowStockAlertModelTest(TestCase, ModelTestHelpers):
    """Test LowStockAlert model."""

    def test_create_alert(self):
        variant = self.create_variant()
        alert = LowStockAlert.objects.create(
            variant=variant,
            threshold=10,
            quantity_at_alert=5,
            status="open",
        )
        self.assertEqual(alert.status, "open")

    def test_resolve_alert(self):
        variant = self.create_variant()
        alert = LowStockAlert.objects.create(
            variant=variant, threshold=10, quantity_at_alert=5
        )
        alert.status = "resolved"
        alert.save()
        self.assertEqual(alert.status, "resolved")


# ---------------------------------------------------------------------------
# Review Tests
# ---------------------------------------------------------------------------


class ProductReviewModelTest(TestCase, ModelTestHelpers):
    """Test ProductReview model."""

    def test_create_review(self):
        product = self.create_product()
        review = ProductReview.objects.create(
            product=product,
            user_id=1,
            rating=5,
            title="Great product",
            comment="Love it!",
            status="pending",
        )
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.status, "pending")

    def test_review_rating_constraint(self):
        product = self.create_product()
        with self.assertRaises(ValidationError):
            review = ProductReview(
                product=product, user_id=1, rating=0, title="Bad"
            )
            review.full_clean()

    def test_review_rating_max(self):
        product = self.create_product()
        with self.assertRaises(ValidationError):
            review = ProductReview(
                product=product, user_id=1, rating=6, title="Too high"
            )
            review.full_clean()

    def test_review_status_choices(self):
        product = self.create_product()
        for status in ["pending", "approved", "rejected"]:
            review = ProductReview.objects.create(
                product=product, user_id=1, rating=4, status=status
            )
            self.assertEqual(review.status, status)


# ---------------------------------------------------------------------------
# Foreign Key Behavior Tests
# ---------------------------------------------------------------------------


class ForeignKeyBehaviorTest(TestCase, ModelTestHelpers):
    """Test foreign key cascade behavior."""

    def test_product_cascade_delete_variant(self):
        product = self.create_product()
        variant = self.create_variant(product=product)
        product.delete()
        self.assertFalse(ProductVariant.objects.filter(pk=variant.pk).exists())

    def test_product_cascade_delete_image(self):
        product = self.create_product()
        image = ProductImage.objects.create(
            product=product, image_url="http://example.com/img.jpg"
        )
        product.delete()
        self.assertFalse(ProductImage.objects.filter(pk=image.pk).exists())

    def test_product_cascade_delete_review(self):
        product = self.create_product()
        review = ProductReview.objects.create(
            product=product, user_id=1, rating=5
        )
        product.delete()
        self.assertFalse(ProductReview.objects.filter(pk=review.pk).exists())

    def test_variant_cascade_delete_price(self):
        variant = self.create_variant()
        price = self.create_price(variant=variant)
        variant.delete()
        self.assertFalse(VariantPrice.objects.filter(pk=price.pk).exists())

    def test_variant_cascade_delete_inventory(self):
        variant = self.create_variant()
        inv = self.create_inventory(variant=variant)
        variant.delete()
        self.assertFalse(Inventory.objects.filter(pk=inv.pk).exists())

    def test_category_protect_product(self):
        category = self.create_category()
        product = self.create_product(category=category)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                category.delete()
