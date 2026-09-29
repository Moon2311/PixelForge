from django.contrib.postgres.indexes import GinIndex, OpClass
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, JSONField, TextField, Value
from django.db.models.functions import Cast, Concat, Upper
from django.utils import timezone


def product_search_text():
    """Upper-cased, space-joined searchable text stored on a product row.

    Keyword search matches ``LIKE '%WORD%'`` against this expression, backed
    by the trigram index ``product_search_trgm`` (see ``Product.Meta``). The
    expression is IMMUTABLE (``||`` + casts + ``upper``) so PostgreSQL can
    index it; the query must use the exact same expression to hit the index.
    """
    parts = [
        F("name"),
        F("sku"),
        F("short_description"),
        F("description"),
        F("specifications_text"),
        F("color"),
        F("size"),
        Cast("tags", TextField()),
    ]
    joined = []
    for part in parts:
        joined += [part, Value(" ")]
    return Upper(Concat(*joined[:-1], output_field=TextField()))


# ---------------------------------------------------------------------------
# Abstract base models
# ---------------------------------------------------------------------------


class TimeStampedModel(models.Model):
    """Abstract model with created_at / updated_at timestamps."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteModel(models.Model):
    """Abstract model supporting soft deletion."""

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_at"])


# ---------------------------------------------------------------------------
# 1. Categories
# ---------------------------------------------------------------------------


class Category(TimeStampedModel, SoftDeleteModel):
    """Top-level product category (e.g. Smartphones, Laptops)."""

    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")
    image = models.URLField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    display_order = models.IntegerField(default=0, db_index=True)

    class Meta:
        db_table = "apps_category"
        ordering = ["display_order", "name"]
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["is_active", "display_order"]),
        ]

    def __str__(self):
        return self.name


class Subcategory(TimeStampedModel, SoftDeleteModel):
    """Subcategory belonging to a parent Category."""

    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="subcategories"
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    description = models.TextField(blank=True, default="")
    image = models.URLField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    display_order = models.IntegerField(default=0, db_index=True)

    class Meta:
        db_table = "apps_subcategory"
        ordering = ["display_order", "name"]
        unique_together = [("category", "name"), ("category", "slug")]
        indexes = [
            models.Index(fields=["category", "is_active"]),
            models.Index(fields=["slug"]),
        ]

    def __str__(self):
        return f"{self.category.name} > {self.name}"


# ---------------------------------------------------------------------------
# 2. Brands
# ---------------------------------------------------------------------------


class Brand(TimeStampedModel, SoftDeleteModel):
    """Product brand (e.g. Apple, Samsung)."""

    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")
    logo = models.URLField(max_length=500, blank=True, default="")
    website = models.URLField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "apps_brand"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["is_active"]),
        ]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# 3. Products
# ---------------------------------------------------------------------------


class Product(TimeStampedModel, SoftDeleteModel):
    """Core product entity. Variants, pricing, and inventory live separately."""

    STATUS_DRAFT = "draft"
    STATUS_ACTIVE = "active"
    STATUS_INACTIVE = "inactive"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_INACTIVE, "Inactive"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    sku = models.CharField(max_length=100, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    short_description = models.CharField(max_length=500, blank=True, default="")
    description = models.TextField(blank=True, default="")

    brand = models.ForeignKey(
        Brand, on_delete=models.PROTECT, related_name="products", null=True, blank=True
    )
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="products", null=True, blank=True
    )
    subcategory = models.ForeignKey(
        Subcategory, on_delete=models.PROTECT, related_name="products", null=True, blank=True
    )

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True
    )
    is_featured = models.BooleanField(default=False, db_index=True)

    # Merchandising fields exposed by the flat /api/products/ endpoints
    specifications_text = models.TextField(blank=True, default="", db_default="")
    cost_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    total_sales = models.IntegerField(default=0, db_default=0)
    flash_sale = models.BooleanField(default=False, db_default=False, db_index=True)
    flash_sale_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    flash_sale_ends_at = models.DateTimeField(null=True, blank=True)
    color = models.CharField(max_length=100, blank=True, default="", db_default="")
    size = models.CharField(max_length=100, blank=True, default="", db_default="")
    weight = models.FloatField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True, db_default=Value([], JSONField()))

    # Denormalized search stats, kept in sync by apps.catalog.stats (signals)
    # in the same transaction as the price/inventory/review change. They let
    # search filter, sort and facet on price/rating/stock with plain indexed
    # columns instead of per-row subqueries.
    price_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    in_stock = models.BooleanField(default=False, db_default=False)
    rating_avg = models.FloatField(null=True, blank=True)
    review_count = models.IntegerField(default=0, db_default=0)

    # Audit fields (auth User ids)
    created_by = models.IntegerField(null=True, blank=True)
    updated_by = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "apps_product"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["sku"]),
            models.Index(fields=["slug"]),
            models.Index(fields=["brand", "status"]),
            models.Index(fields=["category", "status"]),
            models.Index(fields=["subcategory", "status"]),
            models.Index(fields=["status", "is_featured"]),
            models.Index(fields=["created_at"]),
            # Search filters/sorts on price and rating always include status.
            models.Index(fields=["status", "price_max"], name="product_status_price_idx"),
            models.Index(fields=["status", "rating_avg"], name="product_status_rating_idx"),
            # Default sort of GET /api/products/ (storefront and admin list).
            models.Index(fields=["-updated_at"], name="product_updated_desc_idx"),
            # Keyword search: trigram index over the combined searchable text.
            GinIndex(
                OpClass(product_search_text(), name="gin_trgm_ops"),
                name="product_search_trgm",
            ),
            # Autocomplete and name matching: UPPER(name) LIKE '%...%'.
            GinIndex(
                OpClass(Upper("name"), name="gin_trgm_ops"),
                name="product_name_trgm",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.sku})"


# ---------------------------------------------------------------------------
# 4. Product Images
# ---------------------------------------------------------------------------


class ProductImage(TimeStampedModel):
    """Image associated with a product."""

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="images"
    )
    image_url = models.URLField(max_length=500)
    alt_text = models.CharField(max_length=255, blank=True, default="")
    is_primary = models.BooleanField(default=False)
    display_order = models.IntegerField(default=0)

    class Meta:
        db_table = "apps_productimage"
        ordering = ["display_order", "id"]
        indexes = [
            models.Index(fields=["product", "is_primary"]),
        ]

    def __str__(self):
        return f"Image for {self.product.sku} - {self.alt_text or self.image_url}"


# ---------------------------------------------------------------------------
# 5. Product Variants
# ---------------------------------------------------------------------------


class ProductVariant(TimeStampedModel, SoftDeleteModel):
    """A purchasable variant of a product (e.g. 8GB/256GB, Black)."""

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="variants"
    )
    sku = models.CharField(max_length=100, unique=True, db_index=True)
    barcode = models.CharField(max_length=100, blank=True, default="", db_index=True)
    name = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=[
            ("active", "Active"),
            ("inactive", "Inactive"),
            ("archived", "Archived"),
        ],
        default="active",
        db_index=True,
    )

    class Meta:
        db_table = "apps_productvariant"
        ordering = ["name", "id"]
        indexes = [
            models.Index(fields=["product", "status"]),
            models.Index(fields=["sku"]),
            models.Index(fields=["barcode"]),
        ]

    def __str__(self):
        return f"{self.product.name} - {self.name or self.sku}"


# ---------------------------------------------------------------------------
# 6. Attributes (reusable attribute system)
# ---------------------------------------------------------------------------


class Attribute(TimeStampedModel):
    """Reusable attribute definition (e.g. RAM, Color, Storage)."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)

    class Meta:
        db_table = "apps_attribute"
        ordering = ["name"]

    def __str__(self):
        return self.name


class AttributeValue(TimeStampedModel):
    """A specific value for an attribute (e.g. 8GB for RAM, Black for Color)."""

    attribute = models.ForeignKey(
        Attribute, on_delete=models.CASCADE, related_name="values"
    )
    value = models.CharField(max_length=255)

    class Meta:
        db_table = "apps_attributevalue"
        ordering = ["attribute", "value"]
        unique_together = ("attribute", "value")

    def __str__(self):
        return f"{self.attribute.name}: {self.value}"


class ProductAttributeValue(TimeStampedModel):
    """Links a product to an attribute value."""

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="attribute_values"
    )
    attribute_value = models.ForeignKey(
        AttributeValue, on_delete=models.CASCADE, related_name="product_assignments"
    )

    class Meta:
        db_table = "apps_productattributevalue"
        unique_together = ("product", "attribute_value")
        indexes = [
            models.Index(fields=["product"]),
            models.Index(fields=["attribute_value"]),
        ]

    def __str__(self):
        return f"{self.product.sku} - {self.attribute_value}"


class VariantAttributeValue(TimeStampedModel):
    """Links a variant to an attribute value (for variant-specific attributes)."""

    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE, related_name="attribute_values"
    )
    attribute_value = models.ForeignKey(
        AttributeValue, on_delete=models.CASCADE, related_name="variant_assignments"
    )

    class Meta:
        db_table = "apps_variantattributevalue"
        unique_together = ("variant", "attribute_value")
        indexes = [
            models.Index(fields=["variant"]),
            models.Index(fields=["attribute_value"]),
        ]

    def __str__(self):
        return f"{self.variant.sku} - {self.attribute_value}"


# ---------------------------------------------------------------------------
# 7. Product Specifications
# ---------------------------------------------------------------------------


class ProductSpecification(TimeStampedModel):
    """Non-attribute specifications (e.g. Processor = Intel Core i5)."""

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="specifications"
    )
    name = models.CharField(max_length=255)
    value = models.CharField(max_length=500)
    unit = models.CharField(max_length=50, blank=True, default="")
    display_order = models.IntegerField(default=0)

    class Meta:
        db_table = "apps_productspecification"
        ordering = ["display_order", "name"]
        unique_together = ("product", "name")
        indexes = [
            models.Index(fields=["product"]),
            # Keyword search matches specification values: UPPER(value) LIKE '%...%'.
            GinIndex(
                OpClass(Upper("value"), name="gin_trgm_ops"),
                name="spec_value_trgm",
            ),
        ]

    def __str__(self):
        unit = f" {self.unit}" if self.unit else ""
        return f"{self.name}: {self.value}{unit}"


# ---------------------------------------------------------------------------
# 8. Pricing (variant-level)
# ---------------------------------------------------------------------------


class VariantPrice(TimeStampedModel):
    """Price for a product variant. Supports regular/sale pricing."""

    variant = models.OneToOneField(
        ProductVariant, on_delete=models.CASCADE, related_name="price"
    )
    regular_price = models.DecimalField(
        max_digits=12, decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    sale_price = models.DecimalField(
        max_digits=12, decimal_places=2,
        null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    currency = models.CharField(max_length=3, default="USD")
    effective_from = models.DateTimeField(null=True, blank=True)
    effective_to = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "apps_variantprice"
        indexes = [
            models.Index(fields=["variant", "is_active"]),
            models.Index(fields=["is_active", "effective_from", "effective_to"]),
        ]

    def __str__(self):
        return f"{self.variant.sku}: {self.regular_price} {self.currency}"

    @property
    def current_price(self):
        """Return the effective price (sale or regular)."""
        now = timezone.now()
        if (
            self.sale_price is not None
            and self.is_active
            and (self.effective_from is None or self.effective_from <= now)
            and (self.effective_to is None or self.effective_to >= now)
        ):
            return self.sale_price
        return self.regular_price


# ---------------------------------------------------------------------------
# 9. Inventory (variant-level)
# ---------------------------------------------------------------------------


class Inventory(TimeStampedModel):
    """Inventory tracking for a product variant."""

    variant = models.OneToOneField(
        ProductVariant, on_delete=models.CASCADE, related_name="inventory"
    )
    stock_quantity = models.IntegerField(default=0)
    reserved_quantity = models.IntegerField(default=0)
    low_stock_threshold = models.IntegerField(default=10)

    class Meta:
        db_table = "apps_inventory"
        indexes = [
            models.Index(fields=["variant"]),
        ]

    def __str__(self):
        return f"{self.variant.sku}: {self.available_quantity} available"

    @property
    def available_quantity(self):
        """Safe available quantity calculation."""
        return max(0, self.stock_quantity - self.reserved_quantity)

    @property
    def is_in_stock(self):
        return self.available_quantity > 0

    @property
    def is_low_stock(self):
        return 0 < self.available_quantity <= self.low_stock_threshold

    @property
    def is_out_of_stock(self):
        return self.available_quantity <= 0


# ---------------------------------------------------------------------------
# 10. Inventory Logs
# ---------------------------------------------------------------------------


class InventoryLog(TimeStampedModel):
    """Audit trail for inventory changes at the variant level."""

    ACTION_STOCK_IN = "stock_in"
    ACTION_STOCK_OUT = "stock_out"
    ACTION_ADJUSTMENT = "adjustment"
    ACTION_RESERVATION = "reservation"
    ACTION_RELEASE = "release"
    ACTION_SALE = "sale"
    ACTION_RETURN = "return"
    ACTION_CREATE = "create"
    ACTION_UPDATE = "update"
    ACTION_DELETE = "delete"

    ACTION_CHOICES = [
        (ACTION_STOCK_IN, "Stock In"),
        (ACTION_STOCK_OUT, "Stock Out"),
        (ACTION_ADJUSTMENT, "Adjustment"),
        (ACTION_RESERVATION, "Reservation"),
        (ACTION_RELEASE, "Release"),
        (ACTION_SALE, "Sale"),
        (ACTION_RETURN, "Return"),
        (ACTION_CREATE, "Created"),
        (ACTION_UPDATE, "Updated"),
        (ACTION_DELETE, "Deleted"),
    ]

    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE, related_name="inventory_logs"
    )
    previous_quantity = models.IntegerField(default=0)
    new_quantity = models.IntegerField(default=0)
    quantity_change = models.IntegerField(default=0)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, db_index=True)
    reason = models.TextField(blank=True, default="")
    actor_user_id = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "apps_inventorylog"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["variant", "action"]),
            models.Index(fields=["variant", "created_at"]),
            models.Index(fields=["action", "created_at"]),
        ]

    def __str__(self):
        return f"{self.variant.sku} {self.action} ({self.quantity_change:+d})"


# ---------------------------------------------------------------------------
# 11. Low Stock Alerts
# ---------------------------------------------------------------------------


class LowStockAlert(TimeStampedModel):
    """Alert when variant stock falls below threshold."""

    STATUS_OPEN = "open"
    STATUS_RESOLVED = "resolved"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_RESOLVED, "Resolved"),
    ]

    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE, related_name="low_stock_alerts"
    )
    threshold = models.IntegerField()
    quantity_at_alert = models.IntegerField()
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "apps_lowstockalert"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["variant", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.variant.sku} low stock ({self.quantity_at_alert} <= {self.threshold})"


# ---------------------------------------------------------------------------
# 12. Product Reviews
# ---------------------------------------------------------------------------


class ProductReview(TimeStampedModel):
    """User review for a product."""

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="reviews"
    )
    user_id = models.IntegerField(db_index=True)
    rating = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    title = models.CharField(max_length=255, blank=True, default="")
    comment = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )

    class Meta:
        db_table = "apps_productreview"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["product", "status"]),
            models.Index(fields=["product", "rating"]),
            models.Index(fields=["user_id"]),
        ]

    def __str__(self):
        return f"Review #{self.pk} for {self.product.sku} - {self.rating}/5"


# ---------------------------------------------------------------------------
# 13. Banners (homepage merchandising)
# ---------------------------------------------------------------------------


class Banner(TimeStampedModel):
    """Homepage hero/promotion banner."""

    TYPE_HERO = "hero"
    TYPE_PROMOTION = "promotion"
    TYPE_CHOICES = [
        (TYPE_HERO, "Hero"),
        (TYPE_PROMOTION, "Promotion"),
    ]

    title = models.CharField(max_length=255)
    subtitle = models.TextField(blank=True, default="")
    image = models.CharField(max_length=500, blank=True, default="")
    cta_text = models.CharField(max_length=100, blank=True, default="Shop Now")
    cta_link = models.CharField(max_length=500, blank=True, default="/products")
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_HERO, db_index=True)
    discount_badge = models.CharField(max_length=100, blank=True, default="")
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "apps_banner"
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(fields=["type", "is_active"]),
        ]

    def __str__(self):
        return f"{self.get_type_display()} banner: {self.title}"


# ---------------------------------------------------------------------------
# Legacy models (kept for backward compatibility during migration)
# ---------------------------------------------------------------------------


class RecentlyViewed(models.Model):
    """Server-side recently-viewed history for authenticated users."""

    user_id = models.IntegerField()
    product_id = models.IntegerField()
    product_name = models.CharField(max_length=255, blank=True, default="")
    thumbnail = models.CharField(max_length=500, blank=True, default="")
    viewed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "apps_recentlyviewed"
        ordering = ["-viewed_at", "-id"]
        unique_together = ("user_id", "product_id")

    def __str__(self):
        return f"user {self.user_id} -> #{self.product_id} {self.product_name}"
