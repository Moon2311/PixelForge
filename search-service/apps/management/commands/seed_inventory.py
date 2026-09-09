"""Seed 300 dummy products with images, variants, pricing, and inventory.

Uses InventoryManager for stock operations. Safe to run repeatedly:
existing SKUs are skipped.
"""

import random
import string
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from apps.models import (
    Attribute,
    AttributeValue,
    Brand,
    Category,
    Inventory,
    InventoryLog,
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
from apps.inventory_manager import InventoryManager

IMG = "https://picsum.photos/seed/{seed}/{w}/{h}"

CATEGORIES_DATA = [
    ("Smartphones", "Latest phones and 5G devices"),
    ("Laptops", "Powerful notebooks for work and play"),
    ("Tablets", "Versatile tablets for every task"),
    ("Headphones", "Immersive audio for every mood"),
    ("Earbuds", "Compact true-wireless earbuds"),
    ("Cameras", "Capture life in stunning detail"),
    ("Wearables", "Smartwatches and fitness trackers"),
    ("Monitors", "Crisp displays for work and gaming"),
    ("Gaming", "Consoles and gaming gear"),
    ("TV", "Cinema-grade home entertainment"),
    ("Storage", "Fast SSDs and reliable storage"),
    ("Desktop", "Compact and powerful desktops"),
    ("Speaker", "Room-filling portable speakers"),
    ("Shoes", "Iconic sneakers and lifestyle kicks"),
]

SUBCATEGORIES_DATA = {
    "Smartphones": ["Android Phones", "iPhones", "5G Phones", "Budget Phones"],
    "Laptops": ["Gaming Laptops", "Ultrabooks", "Business Laptops", "Chromebooks"],
    "Tablets": ["iPads", "Android Tablets", "Drawing Tablets"],
    "Headphones": ["Over-Ear", "On-Ear", "Noise Cancelling", "Studio"],
    "Earbuds": ["True Wireless", "Sport Earbuds", "Budget Earbuds"],
    "Cameras": ["DSLR", "Mirrorless", "Action Cams", "Point & Shoot"],
    "Wearables": ["Smartwatches", "Fitness Bands", "Smart Rings"],
    "Monitors": ["Gaming Monitors", "4K Monitors", "Ultrawide", "Portable"],
    "Gaming": ["Consoles", "Controllers", "Gaming Keyboards", "Gaming Mice"],
    "TV": ["Smart TVs", "OLED TVs", "QLED TVs", "Projectors"],
    "Storage": ["SSDs", "HDDs", "USB Drives", "Memory Cards"],
    "Desktop": ["Mini PCs", "All-in-One", "Workstations"],
    "Speaker": ["Bluetooth Speakers", "Soundbars", "Bookshelf"],
    "Shoes": ["Running", "Casual", "Basketball", "Formal"],
}

BRANDS_DATA = [
    "Apple", "Samsung", "Dell", "Sony", "Bose", "Canon", "LG",
    "Nike", "Logitech", "Nintendo", "HP", "Lenovo", "Asus", "Xiaomi",
    "OnePlus", "Google", "JBL", "Sennheiser", "GoPro", "Garmin",
]

ATTRIBUTES_DATA = {
    "Color": ["Black", "White", "Silver", "Blue", "Red", "Green", "Gold", "Purple", "Pink", "Gray"],
    "Storage": ["32GB", "64GB", "128GB", "256GB", "512GB", "1TB", "2TB"],
    "RAM": ["4GB", "8GB", "16GB", "32GB", "64GB"],
    "Size": ["XS", "S", "M", "L", "XL", "XXL"],
    "Material": ["Plastic", "Metal", "Glass", "Fabric", "Leather", "Rubber"],
}

# Product name templates per category
PRODUCT_TEMPLATES = {
    "Smartphones": [
        "{brand} {model} Pro", "{brand} {model} Max", "{brand} {model} Lite",
        "{brand} {model} Ultra", "{brand} {model} SE", "{brand} {model} 5G",
    ],
    "Laptops": [
        "{brand} {model} Pro", "{brand} {model} Air", "{brand} {model} Gaming",
        "{brand} {model} Ultrabook", "{brand} {model} Workstation", "{brand} {model} Plus",
    ],
    "Tablets": [
        "{brand} {model} Pro", "{brand} {model} Air", "{brand} {model} Mini",
        "{brand} {model} Lite", "{brand} {model} Studio",
    ],
    "Headphones": [
        "{brand} {model} ANC", "{brand} {model} Pro", "{brand} {model} Elite",
        "{brand} {model} Studio", "{brand} {model} Wireless",
    ],
    "Earbuds": [
        "{brand} {model} Buds", "{brand} {model} Pro", "{brand} {model} Lite",
        "{brand} {model} Sport", "{brand} {model} Air",
    ],
    "Cameras": [
        "{brand} {model} Mark", "{brand} {model} Pro", "{brand} {model} R",
        "{brand} {model} S", "{brand} {model} X",
    ],
    "Wearables": [
        "{brand} {model} Watch", "{brand} {model} Band", "{brand} {model} Tracker",
        "{brand} {model} Sport", "{brand} {model} Ultra",
    ],
    "Monitors": [
        "{brand} {model} Gaming", "{brand} {model} 4K", "{brand} {model} Pro",
        "{brand} {model} Ultra", "{brand} {model} Curved",
    ],
    "Gaming": [
        "{brand} {model} Console", "{brand} {model} Controller", "{brand} {model} Pad",
        "{brand} {model} Elite", "{brand} {model} Racing",
    ],
    "TV": [
        "{brand} {model} OLED", "{brand} {model} QLED", "{brand} {model} Smart",
        "{brand} {model} 4K", "{brand} {model} 8K",
    ],
    "Storage": [
        "{brand} {model} SSD", "{brand} {model} NVMe", "{brand} {model} Portable",
        "{brand} {model} Pro", "{brand} {model} Ultra",
    ],
    "Desktop": [
        "{brand} {model} Mini", "{brand} {model} Pro", "{brand} {model} Tower",
        "{brand} {model} Compact", "{brand} {model} Workstation",
    ],
    "Speaker": [
        "{brand} {model} Boom", "{brand} {model} Pro", "{brand} {model} Max",
        "{brand} {model} Mini", "{brand} {model} Studio",
    ],
    "Shoes": [
        "{brand} {model} Runner", "{brand} {model} Classic", "{brand} {model} Elite",
        "{brand} {model} Sport", "{brand} {model} Daily",
    ],
}

MODEL_NAMES = [
    "Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta",
    "Iota", "Kappa", "Lambda", "Mu", "Nu", "Xi", "Omicron", "Pi", "Rho",
    "Sigma", "Tau", "Upsilon", "Phi", "Chi", "Psi", "Omega",
    "Pulse", "Nexus", "Apex", "Core", "Flux", "Vibe", "Spark", "Edge",
    "Bolt", "Surge", "Wave", "Peak", "Stride", "Glide", "Swift", "Blaze",
]

COLORS = ["Midnight", "Starlight", "Graphite", "Silver", "Blue", "Green", "Purple", "Red"]

SPEC_NAMES = [
    "Processor", "Display", "Battery", "Weight", "Dimensions", "Water Resistance",
    "Connectivity", "Operating System", "Camera", "Audio", "Build", "Refresh Rate",
]


def _img(seed, w=800, h=600):
    return IMG.format(seed=seed, w=w, h=h)


def _sku():
    return "SKU-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def _slug(name):
    return slugify(name) + "-" + "".join(random.choices(string.digits, k=4))


class Command(BaseCommand):
    help = "Seed 300 dummy products with images, variants, pricing, and inventory via InventoryManager."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=300, help="Number of products to create.")

    def handle(self, *args, **options):
        count = options["count"]
        rng = random.Random(20260812)

        self.stdout.write("Seeding categories...")
        categories = self._seed_categories(rng)

        self.stdout.write("Seeding subcategories...")
        subcategories = self._seed_subcategories(categories, rng)

        self.stdout.write("Seeding brands...")
        brands = self._seed_brands(rng)

        self.stdout.write("Seeding attributes...")
        attributes, attr_values = self._seed_attributes()

        existing_skus = set(
            Product.objects.values_list("sku", flat=True)
        )
        self.stdout.write(f"  {len(existing_skus)} existing products found.")

        created = 0
        for i in range(count):
            if created >= count:
                break

            cat = rng.choice(categories)
            subcat = rng.choice(subcategories[cat.pk])
            brand = rng.choice(brands)

            name = self._gen_name(cat.name, brand.name, rng)
            if name in existing_skus:
                continue

            sku = _sku()
            while sku in existing_skus:
                sku = _sku()
            existing_skus.add(sku)

            product = self._create_product(
                name, sku, brand, cat, subcat, rng
            )
            self._create_image(product, rng)
            variants = self._create_variants(product, rng)
            self._create_specs(product, rng)
            self._create_product_attrs(product, attributes, attr_values, rng)

            for variant in variants:
                self._create_variant_attrs(variant, attributes, attr_values, rng)
                self._create_price(variant, rng)
                self._stock_variant(variant, rng)

            created += 1
            if created % 50 == 0:
                self.stdout.write(f"  Created {created}/{count} products...")

        self.stdout.write(self.style.SUCCESS(f"Done. Created {created} products."))

    # ------------------------------------------------------------------

    def _seed_categories(self, rng):
        cats = []
        for idx, (name, desc) in enumerate(CATEGORIES_DATA):
            cat, _ = Category.objects.get_or_create(
                name=name,
                defaults={
                    "slug": slugify(name),
                    "description": desc,
                    "image": _img(f"cat-{name.lower()}", 600, 400),
                    "is_active": True,
                    "display_order": idx + 1,
                },
            )
            cats.append(cat)
        return cats

    def _seed_subcategories(self, categories, rng):
        subcats = {}
        for cat in categories:
            subcat_list = []
            for name in SUBCATEGORIES_DATA.get(cat.name, ["General"]):
                sc, _ = Subcategory.objects.get_or_create(
                    category=cat,
                    name=name,
                    defaults={
                        "slug": slugify(f"{cat.name}-{name}"),
                        "description": f"{name} under {cat.name}",
                        "image": _img(f"subcat-{cat.name.lower()}-{name.lower()}", 400, 300),
                        "is_active": True,
                        "display_order": len(subcat_list) + 1,
                    },
                )
                subcat_list.append(sc)
            subcats[cat.pk] = subcat_list
        return subcats

    def _seed_brands(self, rng):
        brands = []
        for name in BRANDS_DATA:
            brand, _ = Brand.objects.get_or_create(
                name=name,
                defaults={
                    "slug": slugify(name),
                    "description": f"Premium {name} products.",
                    "logo": _img(f"brand-{name.lower()}", 200, 200),
                    "website": f"https://www.{name.lower()}.com",
                    "is_active": True,
                },
            )
            brands.append(brand)
        return brands

    def _seed_attributes(self):
        attributes = {}
        attr_values = {}
        for attr_name, values in ATTRIBUTES_DATA.items():
            attr, _ = Attribute.objects.get_or_create(
                name=attr_name,
                defaults={"slug": slugify(attr_name)},
            )
            attributes[attr_name] = attr
            attr_values[attr_name] = []
            for val in values:
                av, _ = AttributeValue.objects.get_or_create(
                    attribute=attr,
                    value=val,
                )
                attr_values[attr_name].append(av)
        return attributes, attr_values

    def _gen_name(self, cat_name, brand_name, rng):
        templates = PRODUCT_TEMPLATES.get(cat_name, ["{brand} {model} Product"])
        template = rng.choice(templates)
        model = rng.choice(MODEL_NAMES)
        suffix = rng.choice(["", " " + rng.choice(COLORS)])
        return template.format(brand=brand_name, model=model).strip() + suffix

    def _create_product(self, name, sku, brand, category, subcategory, rng):
        status = rng.choice(["active", "active", "active", "draft"])
        product = Product.objects.create(
            sku=sku,
            name=name,
            slug=_slug(name),
            short_description=f"High-quality {name} from {brand.name}.",
            description=f"The {name} by {brand.name} delivers exceptional performance and style. Perfect for everyday use with premium build quality.",
            brand=brand,
            category=category,
            subcategory=subcategory,
            status=status,
            is_featured=rng.random() < 0.1,
        )
        return product

    def _create_image(self, product, rng):
        seed = f"prod-{product.sku}"
        ProductImage.objects.create(
            product=product,
            image_url=_img(seed, 800, 800),
            alt_text=product.name,
            is_primary=True,
            display_order=0,
        )
        for i in range(rng.randint(1, 3)):
            ProductImage.objects.create(
                product=product,
                image_url=_img(f"{seed}-img{i}", 800, 800),
                alt_text=f"{product.name} view {i + 2}",
                is_primary=False,
                display_order=i + 1,
            )

    def _create_variants(self, product, rng):
        variant_count = rng.randint(1, 3)
        variants = []
        for i in range(variant_count):
            var_sku = _sku()
            color = rng.choice(ATTRIBUTES_DATA["Color"])
            name = f"{color}" if variant_count > 1 else ""
            variant = ProductVariant.objects.create(
                product=product,
                sku=var_sku,
                barcode="".join(rng.choices(string.digits, k=12)),
                name=name,
                status="active",
            )
            variants.append(variant)
        return variants

    def _create_specs(self, product, rng):
        for spec_name in rng.sample(SPEC_NAMES, rng.randint(3, 6)):
            val = f"Sample {spec_name} value {rng.randint(1, 100)}"
            ProductSpecification.objects.create(
                product=product,
                name=spec_name,
                value=val,
                unit=rng.choice(["", "mm", "g", "mAh", "hrs"]),
                display_order=rng.randint(0, 10),
            )

    def _create_product_attrs(self, product, attributes, attr_values, rng):
        for attr_name in rng.sample(list(attributes.keys()), rng.randint(1, 3)):
            vals = rng.sample(attr_values[attr_name], rng.randint(1, min(3, len(attr_values[attr_name]))))
            for av in vals:
                ProductAttributeValue.objects.get_or_create(
                    product=product,
                    attribute_value=av,
                )

    def _create_variant_attrs(self, variant, attributes, attr_values, rng):
        for attr_name in rng.sample(list(attributes.keys()), rng.randint(0, 2)):
            val = rng.choice(attr_values[attr_name])
            VariantAttributeValue.objects.get_or_create(
                variant=variant,
                attribute_value=val,
            )

    def _create_price(self, variant, rng):
        base = Decimal(str(rng.choice([29.99, 49.99, 79.99, 99.99, 149.99, 199.99, 299.99, 499.99, 699.99, 999.99, 1299.99])))
        has_sale = rng.random() < 0.3
        sale = (base * Decimal(str(rng.uniform(0.7, 0.9)))).quantize(Decimal("0.01")) if has_sale else None
        VariantPrice.objects.create(
            variant=variant,
            regular_price=base,
            sale_price=sale,
            currency="USD",
            is_active=True,
        )

    def _stock_variant(self, variant, rng):
        stock = rng.randint(0, 500)
        reserved = rng.randint(0, min(stock, 20))
        Inventory.objects.create(
            variant=variant,
            stock_quantity=stock,
            reserved_quantity=reserved,
            low_stock_threshold=rng.choice([5, 10, 15, 20, 25]),
        )
        InventoryLog.objects.create(
            variant=variant,
            previous_quantity=0,
            new_quantity=stock,
            quantity_change=stock,
            action="stock_in",
            reason="Initial stock seeding",
        )
