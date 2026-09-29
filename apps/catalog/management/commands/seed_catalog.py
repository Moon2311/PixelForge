"""Seed demo homepage catalog data (categories, brands, banners) and enrich
products with featured / flash-sale / total-sales flags.

Safe to run repeatedly: existing catalog items are left untouched, and product
flags are only set when the target field is currently empty.
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.text import slugify

from apps.catalog.models import Banner, Brand, Category, Product, VariantPrice

IMG = "https://picsum.photos/seed/{seed}/{w}/{h}"

CATEGORIES = [
    ("Smartphones", "Latest phones and 5G devices", 1),
    ("Laptops", "Powerful notebooks for work and play", 2),
    ("Tablets", "Versatile tablets for every task", 3),
    ("Headphones", "Immersive audio for every mood", 4),
    ("Earbuds", "Compact true-wireless earbuds", 5),
    ("Cameras", "Capture life in stunning detail", 6),
    ("Wearables", "Smartwatches and fitness trackers", 7),
    ("Monitors", "Crisp displays for work and gaming", 8),
    ("Gaming", "Consoles and gaming gear", 9),
    ("TV", "Cinema-grade home entertainment", 10),
    ("Storage", "Fast SSDs and reliable storage", 11),
    ("Desktop", "Compact and powerful desktops", 12),
    ("Speaker", "Room-filling portable speakers", 13),
    ("Shoes", "Iconic sneakers and lifestyle kicks", 14),
]

BRANDS = [
    ("Apple", 1),
    ("Samsung", 2),
    ("Dell", 3),
    ("Sony", 4),
    ("Bose", 5),
    ("Canon", 6),
    ("LG", 7),
    ("Nike", 8),
    ("Logitech", 9),
    ("Nintendo", 10),
]

HERO_BANNERS = [
    {
        "title": "Summer Tech Sale",
        "subtitle": "Up to 40% off the season's hottest gadgets.",
        "cta_text": "Shop Now",
        "cta_link": "/products",
        "discount_badge": "Up to 40% OFF",
        "sort_order": 1,
    },
    {
        "title": "New iPhone 15 Series",
        "subtitle": "Titanium design. A17 Pro power. Now in stock.",
        "cta_text": "Learn More",
        "cta_link": "/products?name=iphone",
        "discount_badge": "New Arrival",
        "sort_order": 2,
    },
    {
        "title": "Headphones Week",
        "subtitle": "Block out the world with up to 30% off premium audio.",
        "cta_text": "Shop Headphones",
        "cta_link": "/products?category=Headphones",
        "discount_badge": "Up to 30% OFF",
        "sort_order": 3,
    },
]

PROMO_BANNERS = [
    {
        "title": "Free shipping on orders over $99",
        "subtitle": "Weekend flash — no code needed.",
        "cta_text": "Shop deals",
        "cta_link": "/products",
        "sort_order": 1,
    },
    {
        "title": "Trade in & save more",
        "subtitle": "Get instant credit toward your next upgrade.",
        "cta_text": "Explore trade-in",
        "cta_link": "/products",
        "sort_order": 2,
    },
]


def _img(seed, w=800, h=600):
    return IMG.format(seed=seed, w=w, h=h)


class Command(BaseCommand):
    help = "Seed demo categories, brands and banners, and enrich products."

    def handle(self, *args, **options):
        self._seed_categories()
        self._seed_brands()
        self._seed_banners()
        self._enrich_products()

        self.stdout.write(self.style.SUCCESS("Done seeding homepage catalog."))

    # ------------------------------------------------------------------

    def _seed_categories(self):
        for name, description, sort_order in CATEGORIES:
            _, created = Category.objects.get_or_create(
                name=name,
                defaults={
                    "slug": slugify(name),
                    "description": description,
                    "image": _img(f"cat-{name.lower()}", 600, 400),
                    "display_order": sort_order,
                    "is_active": True,
                },
            )
            self.stdout.write(f"  {'Created' if created else 'Found'} category: {name}")

    def _seed_brands(self):
        for name, _sort_order in BRANDS:
            _, created = Brand.objects.get_or_create(
                name=name,
                defaults={
                    "slug": slugify(name),
                    "description": f"Trusted {name} products.",
                    "logo": _img(f"brand-{name.lower()}", 200, 200),
                    "is_active": True,
                },
            )
            self.stdout.write(f"  {'Created' if created else 'Found'} brand: {name}")

    def _seed_banners(self):
        now = timezone.now()
        for banner in HERO_BANNERS + PROMO_BANNERS:
            banner_type = Banner.TYPE_HERO if banner in HERO_BANNERS else Banner.TYPE_PROMOTION
            _, created = Banner.objects.get_or_create(
                title=banner["title"],
                defaults={
                    **{k: v for k, v in banner.items() if k != "title"},
                    "type": banner_type,
                    "image": _img(f"banner-{banner['title'].lower().replace(' ', '-')}", 1600, 600),
                    "is_active": True,
                    "start_at": now,
                },
            )
            self.stdout.write(f"  {'Created' if created else 'Found'} {banner_type} banner: {banner['title']}")

    # ------------------------------------------------------------------

    def _enrich_products(self):
        products = list(Product.objects.filter(is_deleted=False).order_by("pk"))
        if not products:
            self.stdout.write("  No products to enrich.")
            return

        rng = random.Random(20260725)
        ids = [p.pk for p in products]
        featured_ids = set(rng.sample(ids, min(8, len(ids))))
        sale_ids = set(rng.sample(ids, min(6, len(ids))))
        flash_ends = timezone.now() + timedelta(days=1, hours=3)

        updated = 0
        for product in products:
            changed = []
            if product.pk in featured_ids and not product.is_featured:
                product.is_featured = True
                changed.append("is_featured")
            if not product.total_sales:
                product.total_sales = rng.randint(20, 1200)
                changed.append("total_sales")
            if product.pk in sale_ids and not product.flash_sale:
                price = (
                    VariantPrice.objects.filter(variant__product=product, variant__is_deleted=False)
                    .order_by("variant_id")
                    .values_list("regular_price", flat=True)
                    .first()
                )
                product.flash_sale = True
                product.flash_sale_price = round(price * 7 / 10, 2) if price else None
                product.flash_sale_ends_at = flash_ends
                changed += ["flash_sale", "flash_sale_price", "flash_sale_ends_at"]
            if changed:
                product.save(update_fields=changed + ["updated_at"])
                updated += 1
        self.stdout.write(f"  Enriched {updated} product(s) with homepage flags.")
