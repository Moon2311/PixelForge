"""Search document builder for denormalized Elasticsearch documents.

Builds denormalized search documents from PostgreSQL models and indexes
them into Elasticsearch. Used for both event-driven updates and full reindexing.
"""

import logging
from decimal import Decimal
from typing import Any, Optional

from django.db.models import Avg, Q
from elasticsearch import Elasticsearch

from apps.models import (
    Brand,
    Category,
    Inventory,
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
from apps.search_indices import (
    BRAND_ALIAS,
    CATEGORY_ALIAS,
    PRODUCT_ALIAS,
)

logger = logging.getLogger("catalog.search")


class SearchDocumentBuilder:
    """Builds and indexes denormalized search documents."""

    def __init__(self, client: Elasticsearch):
        self.client = client

    # ------------------------------------------------------------------
    # Product document
    # ------------------------------------------------------------------

    def build_product_document(self, product: Product) -> dict[str, Any]:
        """Build a denormalized product document for Elasticsearch."""
        # Fetch related data
        brand = product.brand
        category = product.category
        subcategory = product.subcategory

        # Fetch variants with related data
        variants = ProductVariant.objects.filter(
            product=product, is_deleted=False
        ).select_related("price", "inventory")

        variant_docs = []
        all_attribute_values = []
        min_price = None
        max_price = None
        total_stock = 0
        any_in_stock = False

        for variant in variants:
            price = getattr(variant, "price", None)
            inventory = getattr(variant, "inventory", None)

            # Build variant price
            variant_price = {
                "regular": float(price.regular_price) if price else 0,
                "sale": float(price.sale_price) if price and price.sale_price else None,
                "currency": price.currency if price else "USD",
            }

            # Build variant inventory
            variant_inventory = {
                "stock_quantity": inventory.stock_quantity if inventory else 0,
                "reserved_quantity": inventory.reserved_quantity if inventory else 0,
                "available_quantity": inventory.available_quantity if inventory else 0,
                "in_stock": inventory.is_in_stock if inventory else False,
            }

            # Build variant attribute values
            attr_values = VariantAttributeValue.objects.filter(
                variant=variant
            ).select_related("attribute_value__attribute")

            variant_attr_docs = []
            for vav in attr_values:
                av = vav.attribute_value
                attr = av.attribute
                variant_attr_docs.append({
                    "attribute_name": attr.name,
                    "attribute_slug": attr.slug,
                    "value": av.value,
                })
                all_attribute_values.append({
                    "name": attr.name,
                    "slug": attr.slug,
                    "values": [av.value],
                })

            variant_docs.append({
                "variant_id": variant.pk,
                "sku": variant.sku,
                "barcode": variant.barcode or "",
                "name": variant.name or "",
                "status": variant.status,
                "price": variant_price,
                "inventory": variant_inventory,
                "attribute_values": variant_attr_docs,
            })

            # Track price range
            if price:
                current = float(price.sale_price or price.regular_price)
                if min_price is None or current < min_price:
                    min_price = current
                if max_price is None or current > max_price:
                    max_price = current

            # Track stock
            if inventory:
                total_stock += inventory.available_quantity
                if inventory.is_in_stock:
                    any_in_stock = True

        # Deduplicate attribute values
        seen_attrs = {}
        unique_attributes = []
        for attr in all_attribute_values:
            key = attr["slug"]
            if key not in seen_attrs:
                seen_attrs[key] = True
                unique_attributes.append(attr)

        # Fetch specifications
        specs = ProductSpecification.objects.filter(product=product)
        specifications = [
            {"name": s.name, "value": s.value, "unit": s.unit or ""}
            for s in specs
        ]

        # Fetch images
        images = ProductImage.objects.filter(product=product).order_by(
            "display_order"
        )
        image_docs = [
            {
                "image_id": img.pk,
                "url": img.image_url,
                "alt_text": img.alt_text or "",
                "is_primary": img.is_primary,
                "display_order": img.display_order,
            }
            for img in images
        ]

        primary_image = ""
        for img in images:
            if img.is_primary:
                primary_image = img.image_url
                break
        if not primary_image and images:
            primary_image = images[0].image_url

        # Fetch reviews
        review_stats = ProductReview.objects.filter(
            product=product, status="approved"
        ).aggregate(
            avg_rating=Avg("rating"),
            total_reviews=Avg("id"),  # We'll use count below
        )
        avg_rating = float(review_stats["avg_rating"] or 0)
        review_count = ProductReview.objects.filter(
            product=product, status="approved"
        ).count()

        # Build document
        document = {
            "product_id": product.pk,
            "sku": product.sku,
            "name": product.name,
            "slug": product.slug,
            "description": product.description or "",
            "short_description": product.short_description or "",
            "status": product.status,
            "is_featured": product.is_featured,
            "created_at": product.created_at.isoformat() if product.created_at else None,
            "updated_at": product.updated_at.isoformat() if product.updated_at else None,
            "brand": {
                "id": brand.pk if brand else None,
                "name": brand.name if brand else "",
                "slug": brand.slug if brand else "",
                "logo": brand.logo if brand else "",
            } if brand else None,
            "category": {
                "id": category.pk if category else None,
                "name": category.name if category else "",
                "slug": category.slug if category else "",
            } if category else None,
            "subcategory": {
                "id": subcategory.pk if subcategory else None,
                "name": subcategory.name if subcategory else "",
                "slug": subcategory.slug if subcategory else "",
            } if subcategory else None,
            "variants": variant_docs,
            "attributes": unique_attributes,
            "specifications": specifications,
            "images": image_docs,
            "primary_image": primary_image,
            "price": {
                "regular": max_price or 0,
                "min": min_price or 0,
                "max": max_price or 0,
                "sale": None,
                "currency": "USD",
            },
            "rating": round(avg_rating, 2),
            "review_count": review_count,
            "availability": {
                "in_stock": any_in_stock,
                "quantity": total_stock,
            },
            "tags": [],  # Can be populated from product tags if needed
            "color": "",
            "size": "",
            "weight": 0,
        }

        return document

    # ------------------------------------------------------------------
    # Category document
    # ------------------------------------------------------------------

    def build_category_document(self, category: Category) -> dict[str, Any]:
        """Build a denormalized category document for Elasticsearch."""
        # Fetch subcategories
        subcategories = Subcategory.objects.filter(
            category=category, is_deleted=False
        ).order_by("display_order", "name")

        subcategory_docs = []
        for sub in subcategories:
            product_count = Product.objects.filter(
                subcategory=sub, is_deleted=False
            ).count()
            subcategory_docs.append({
                "subcategory_id": sub.pk,
                "name": sub.name,
                "slug": sub.slug,
                "is_active": sub.is_active,
                "display_order": sub.display_order,
                "product_count": product_count,
            })

        # Count products in this category
        product_count = Product.objects.filter(
            category=category, is_deleted=False
        ).count()

        return {
            "category_id": category.pk,
            "name": category.name,
            "slug": category.slug,
            "description": category.description or "",
            "image": category.image or "",
            "is_active": category.is_active,
            "display_order": category.display_order,
            "product_count": product_count,
            "subcategories": subcategory_docs,
            "created_at": category.created_at.isoformat() if category.created_at else None,
            "updated_at": category.updated_at.isoformat() if category.updated_at else None,
        }

    # ------------------------------------------------------------------
    # Brand document
    # ------------------------------------------------------------------

    def build_brand_document(self, brand: Brand) -> dict[str, Any]:
        """Build a denormalized brand document for Elasticsearch."""
        # Count products for this brand
        product_count = Product.objects.filter(
            brand=brand, is_deleted=False
        ).count()

        return {
            "brand_id": brand.pk,
            "name": brand.name,
            "slug": brand.slug,
            "description": brand.description or "",
            "logo": brand.logo or "",
            "website": brand.website or "",
            "is_active": brand.is_active,
            "product_count": product_count,
            "created_at": brand.created_at.isoformat() if brand.created_at else None,
            "updated_at": brand.updated_at.isoformat() if brand.updated_at else None,
        }

    # ------------------------------------------------------------------
    # Index operations
    # ------------------------------------------------------------------

    def index_product(self, product: Product):
        """Index a single product into Elasticsearch."""
        doc = self.build_product_document(product)
        self.client.index(
            index=PRODUCT_ALIAS,
            id=f"product:{product.pk}",
            document=doc,
        )
        logger.info(f"Indexed product {product.pk}")

    def index_category(self, category: Category):
        """Index a single category into Elasticsearch."""
        doc = self.build_category_document(category)
        self.client.index(
            index=CATEGORY_ALIAS,
            id=f"category:{category.pk}",
            document=doc,
        )
        logger.info(f"Indexed category {category.pk}")

    def index_brand(self, brand: Brand):
        """Index a single brand into Elasticsearch."""
        doc = self.build_brand_document(brand)
        self.client.index(
            index=BRAND_ALIAS,
            id=f"brand:{brand.pk}",
            document=doc,
        )
        logger.info(f"Indexed brand {brand.pk}")

    def delete_product(self, product_id: int):
        """Delete a product from Elasticsearch."""
        try:
            self.client.delete(index=PRODUCT_ALIAS, id=f"product:{product_id}")
            logger.info(f"Deleted product {product_id} from ES")
        except Exception:
            logger.warning(f"Product {product_id} not found in ES for deletion")

    def delete_category(self, category_id: int):
        """Delete a category from Elasticsearch."""
        try:
            self.client.delete(index=CATEGORY_ALIAS, id=f"category:{category_id}")
            logger.info(f"Deleted category {category_id} from ES")
        except Exception:
            logger.warning(f"Category {category_id} not found in ES for deletion")

    def delete_brand(self, brand_id: int):
        """Delete a brand from Elasticsearch."""
        try:
            self.client.delete(index=BRAND_ALIAS, id=f"brand:{brand_id}")
            logger.info(f"Deleted brand {brand_id} from ES")
        except Exception:
            logger.warning(f"Brand {brand_id} not found in ES for deletion")

    # ------------------------------------------------------------------
    # Bulk reindexing
    # ------------------------------------------------------------------

    def reindex_products(self) -> int:
        """Reindex all active products from PostgreSQL."""
        products = Product.objects.filter(
            is_deleted=False
        ).select_related("brand", "category", "subcategory")

        count = 0
        for product in products:
            try:
                self.index_product(product)
                count += 1
            except Exception as e:
                logger.error(f"Failed to index product {product.pk}: {e}")

        return count

    def reindex_categories(self) -> int:
        """Reindex all active categories from PostgreSQL."""
        categories = Category.objects.filter(is_deleted=False)

        count = 0
        for category in categories:
            try:
                self.index_category(category)
                count += 1
            except Exception as e:
                logger.error(f"Failed to index category {category.pk}: {e}")

        return count

    def reindex_brands(self) -> int:
        """Reindex all active brands from PostgreSQL."""
        brands = Brand.objects.filter(is_deleted=False)

        count = 0
        for brand in brands:
            try:
                self.index_brand(brand)
                count += 1
            except Exception as e:
                logger.error(f"Failed to index brand {brand.pk}: {e}")

        return count
