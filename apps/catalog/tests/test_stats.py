"""Denormalized product search stats stay in sync with their source rows."""

from decimal import Decimal

from django.test import TestCase

from apps.catalog.models import Inventory, Product, ProductReview, ProductVariant, VariantPrice
from apps.catalog.stats import refresh_product_stats


class ProductStatsSyncTest(TestCase):
    def setUp(self):
        self.product = Product.objects.create(name="Cam", sku="CAM", slug="cam", status="active")
        self.v1 = ProductVariant.objects.create(product=self.product, sku="CAM-1")
        self.v2 = ProductVariant.objects.create(product=self.product, sku="CAM-2")

    def stats(self):
        p = Product.objects.get(pk=self.product.pk)
        return p.price_min, p.price_max, p.in_stock, p.rating_avg, p.review_count

    def test_price_changes(self):
        VariantPrice.objects.create(variant=self.v1, regular_price=Decimal("100"))
        price = VariantPrice.objects.create(variant=self.v2, regular_price=Decimal("300"), sale_price=Decimal("250"))
        self.assertEqual(self.stats()[:2], (Decimal("100.00"), Decimal("250.00")))
        price.delete()
        self.assertEqual(self.stats()[:2], (Decimal("100.00"), Decimal("100.00")))

    def test_stock_and_variant_soft_delete(self):
        inventory = Inventory.objects.create(variant=self.v1, stock_quantity=3, reserved_quantity=3)
        self.assertFalse(self.stats()[2])
        inventory.reserved_quantity = 1
        inventory.save()
        self.assertTrue(self.stats()[2])
        self.v1.soft_delete()
        self.assertFalse(self.stats()[2])

    def test_only_approved_reviews_count(self):
        review = ProductReview.objects.create(product=self.product, user_id=1, rating=5)
        self.assertEqual(self.stats()[3:], (None, 0))
        review.status = ProductReview.STATUS_APPROVED
        review.save()
        ProductReview.objects.create(product=self.product, user_id=2, rating=2, status="approved")
        self.assertEqual(self.stats()[3:], (3.5, 2))

    def test_full_refresh(self):
        VariantPrice.objects.create(variant=self.v1, regular_price=Decimal("10"))
        Product.objects.filter(pk=self.product.pk).update(price_min=None, price_max=None)
        self.assertEqual(refresh_product_stats(), 1)
        self.assertEqual(self.stats()[1], Decimal("10.00"))
