"""Management command to set up Django application RBAC permissions and groups.

Usage:
    python manage.py setup_rbac
    python manage.py setup_rbac --verify
"""

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

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
# Group definitions
# ---------------------------------------------------------------------------

GROUPS = {
    "ADMIN": {
        "description": "Full product catalog administration",
        "permissions": [
            # Products
            ("add_product", Product),
            ("change_product", Product),
            ("delete_product", Product),
            ("view_product", Product),
            # Categories
            ("add_category", Category),
            ("change_category", Category),
            ("delete_category", Category),
            ("view_category", Category),
            ("add_subcategory", Subcategory),
            ("change_subcategory", Subcategory),
            ("delete_subcategory", Subcategory),
            ("view_subcategory", Subcategory),
            # Brands
            ("add_brand", Brand),
            ("change_brand", Brand),
            ("delete_brand", Brand),
            ("view_brand", Brand),
            # Variants
            ("add_productvariant", ProductVariant),
            ("change_productvariant", ProductVariant),
            ("delete_productvariant", ProductVariant),
            ("view_productvariant", ProductVariant),
            # Images
            ("add_productimage", ProductImage),
            ("change_productimage", ProductImage),
            ("delete_productimage", ProductImage),
            ("view_productimage", ProductImage),
            # Attributes
            ("add_attribute", Attribute),
            ("change_attribute", Attribute),
            ("delete_attribute", Attribute),
            ("view_attribute", Attribute),
            ("add_attributevalue", AttributeValue),
            ("change_attributevalue", AttributeValue),
            ("delete_attributevalue", AttributeValue),
            ("view_attributevalue", AttributeValue),
            ("add_productattributevalue", ProductAttributeValue),
            ("change_productattributevalue", ProductAttributeValue),
            ("delete_productattributevalue", ProductAttributeValue),
            ("view_productattributevalue", ProductAttributeValue),
            ("add_variantattributevalue", VariantAttributeValue),
            ("change_variantattributevalue", VariantAttributeValue),
            ("delete_variantattributevalue", VariantAttributeValue),
            ("view_variantattributevalue", VariantAttributeValue),
            # Specifications
            ("add_productspecification", ProductSpecification),
            ("change_productspecification", ProductSpecification),
            ("delete_productspecification", ProductSpecification),
            ("view_productspecification", ProductSpecification),
            # Pricing
            ("add_variantprice", VariantPrice),
            ("change_variantprice", VariantPrice),
            ("delete_variantprice", VariantPrice),
            ("view_variantprice", VariantPrice),
            # Inventory
            ("add_inventory", Inventory),
            ("change_inventory", Inventory),
            ("delete_inventory", Inventory),
            ("view_inventory", Inventory),
            ("add_inventorylog", InventoryLog),
            ("change_inventorylog", InventoryLog),
            ("delete_inventorylog", InventoryLog),
            ("view_inventorylog", InventoryLog),
            ("add_lowstockalert", LowStockAlert),
            ("change_lowstockalert", LowStockAlert),
            ("delete_lowstockalert", LowStockAlert),
            ("view_lowstockalert", LowStockAlert),
            # Reviews
            ("add_productreview", ProductReview),
            ("change_productreview", ProductReview),
            ("delete_productreview", ProductReview),
            ("view_productreview", ProductReview),
        ],
    },
    "CATALOG_MANAGER": {
        "description": "Manage products, categories, brands, images, attributes",
        "permissions": [
            # Products
            ("add_product", Product),
            ("change_product", Product),
            ("view_product", Product),
            # Categories
            ("add_category", Category),
            ("change_category", Category),
            ("view_category", Category),
            ("add_subcategory", Subcategory),
            ("change_subcategory", Subcategory),
            ("view_subcategory", Subcategory),
            # Brands
            ("add_brand", Brand),
            ("change_brand", Brand),
            ("view_brand", Brand),
            # Variants
            ("add_productvariant", ProductVariant),
            ("change_productvariant", ProductVariant),
            ("view_productvariant", ProductVariant),
            # Images
            ("add_productimage", ProductImage),
            ("change_productimage", ProductImage),
            ("delete_productimage", ProductImage),
            ("view_productimage", ProductImage),
            # Attributes
            ("add_attribute", Attribute),
            ("change_attribute", Attribute),
            ("view_attribute", Attribute),
            ("add_attributevalue", AttributeValue),
            ("change_attributevalue", AttributeValue),
            ("view_attributevalue", AttributeValue),
            ("add_productattributevalue", ProductAttributeValue),
            ("change_productattributevalue", ProductAttributeValue),
            ("delete_productattributevalue", ProductAttributeValue),
            ("view_productattributevalue", ProductAttributeValue),
            ("add_variantattributevalue", VariantAttributeValue),
            ("change_variantattributevalue", VariantAttributeValue),
            ("delete_variantattributevalue", VariantAttributeValue),
            ("view_variantattributevalue", VariantAttributeValue),
            # Specifications
            ("add_productspecification", ProductSpecification),
            ("change_productspecification", ProductSpecification),
            ("delete_productspecification", ProductSpecification),
            ("view_productspecification", ProductSpecification),
        ],
    },
    "INVENTORY_MANAGER": {
        "description": "Manage stock, view inventory, resolve low-stock alerts",
        "permissions": [
            # Inventory
            ("add_inventory", Inventory),
            ("change_inventory", Inventory),
            ("view_inventory", Inventory),
            ("add_inventorylog", InventoryLog),
            ("view_inventorylog", InventoryLog),
            # Low stock
            ("change_lowstockalert", LowStockAlert),
            ("view_lowstockalert", LowStockAlert),
            # Products (read-only for context)
            ("view_product", Product),
            ("view_productvariant", ProductVariant),
        ],
    },
    "CUSTOMER": {
        "description": "Read catalog, create/update own reviews",
        "permissions": [
            # Read catalog
            ("view_product", Product),
            ("view_category", Category),
            ("view_subcategory", Subcategory),
            ("view_brand", Brand),
            ("view_productimage", ProductImage),
            ("view_productvariant", ProductVariant),
            ("view_variantprice", VariantPrice),
            # Reviews
            ("add_productreview", ProductReview),
            ("change_productreview", ProductReview),
            ("view_productreview", ProductReview),
        ],
    },
}


class Command(BaseCommand):
    help = "Set up Django application RBAC permissions and groups"

    def add_arguments(self, parser):
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Verify current RBAC setup",
        )
        parser.add_argument(
            "--drop",
            action="store_true",
            help="Drop all custom groups (careful!)",
        )

    def handle(self, *args, **options):
        if options["verify"]:
            self._verify_rbac()
            return

        if options["drop"]:
            self._drop_groups()
            return

        self._create_rbac()

    def _create_rbac(self):
        """Create Django groups and permissions."""
        self.stdout.write("Setting up Django RBAC...")

        for group_name, config in GROUPS.items():
            self.stdout.write(f"  Creating group: {group_name}")

            group, created = Group.objects.get_or_create(name=group_name)
            if created:
                self.stdout.write(f"    Created new group")
            else:
                self.stdout.write(f"    Group already exists")

            # Clear existing permissions
            group.permissions.clear()

            # Add permissions
            for perm_name, model in config["permissions"]:
                try:
                    content_type = ContentType.objects.get_for_model(model)
                    permission = Permission.objects.get(
                        codename=perm_name,
                        content_type=content_type,
                    )
                    group.permissions.add(permission)
                except Permission.DoesNotExist:
                    self.stdout.write(
                        self.style.WARNING(
                            f"    Permission not found: {perm_name} for {model.__name__}"
                        )
                    )

            self.stdout.write(f"    Added {len(config['permissions'])} permissions")

        self.stdout.write(self.style.SUCCESS("\nDjango RBAC setup complete!"))
        self._verify_rbac()

    def _drop_groups(self):
        """Drop all custom groups."""
        self.stdout.write(self.style.WARNING("Dropping custom groups..."))

        for group_name in GROUPS:
            try:
                group = Group.objects.get(name=group_name)
                group.delete()
                self.stdout.write(f"  Deleted group: {group_name}")
            except Group.DoesNotExist:
                self.stdout.write(f"  Group not found: {group_name}")

        self.stdout.write(self.style.SUCCESS("Groups dropped!"))

    def _verify_rbac(self):
        """Verify current RBAC setup."""
        self.stdout.write("\nVerifying Django RBAC...\n")

        for group_name, config in GROUPS.items():
            try:
                group = Group.objects.get(name=group_name)
                perm_count = group.permissions.count()
                self.stdout.write(f"  {group_name}: {perm_count} permissions")
            except Group.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"  {group_name}: NOT FOUND"))

        # Summary
        total_groups = Group.objects.filter(name__in=GROUPS.keys()).count()
        total_permissions = Permission.objects.count()
        self.stdout.write(f"\n  Total custom groups: {total_groups}")
        self.stdout.write(f"  Total Django permissions: {total_permissions}")
        self.stdout.write("")
