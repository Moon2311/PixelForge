"""Seed demo homepage catalog data (categories, brands, banners) and enrich
products with featured / flash-sale / total-sales flags.

Safe to run repeatedly: existing catalog items are left untouched, and product
flags are only set when the target field is currently empty.
"""

import random
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from apps.catalog_store import (
    BANNER_INDEX,
    BRAND_INDEX,
    CATEGORY_INDEX,
    build_banner_document,
    build_brand_document,
    build_category_document,
    ensure_all_mappings,
    ensure_mapping,
    list_banners,
    list_brands,
    list_categories,
)
from apps.elasticsearch import get_elasticsearch_client
from apps import product_store

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
        client = get_elasticsearch_client()
        try:
            product_store.ensure_mapping(client)
            ensure_all_mappings(client)
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"Could not prepare indices: {exc}"))
            return

        self._seed_categories(client)
        self._seed_brands(client)
        self._seed_banners(client)
        self._enrich_products(client)

        self.stdout.write(self.style.SUCCESS("Done seeding homepage catalog."))

    # ------------------------------------------------------------------

    def _seed_categories(self, client):
        existing = {c["name"] for c in list_categories(client)}
        for name, description, sort_order in CATEGORIES:
            if name in existing:
                self.stdout.write(f"  Found category: {name}")
                continue
            doc_id = self._next(client, CATEGORY_INDEX)
            doc = build_category_document(
                doc_id,
                {
                    "name": name,
                    "description": description,
                    "image": _img(f"cat-{name.lower()}", 600, 400),
                    "is_active": True,
                },
            )
            client.index(index=CATEGORY_INDEX, id=doc_id, document=doc, refresh="wait_for")
            self.stdout.write(f"  Created category: {name}")

    def _seed_brands(self, client):
        existing = {b["name"] for b in list_brands(client)}
        for name, sort_order in BRANDS:
            if name in existing:
                self.stdout.write(f"  Found brand: {name}")
                continue
            doc_id = self._next(client, BRAND_INDEX)
            doc = build_brand_document(
                doc_id,
                {
                    "name": name,
                    "description": f"Trusted {name} products.",
                    "logo": _img(f"brand-{name.lower()}", 200, 200),
                    "is_active": True,
                },
            )
            client.index(index=BRAND_INDEX, id=doc_id, document=doc, refresh="wait_for")
            self.stdout.write(f"  Created brand: {name}")

    def _seed_banners(self, client):
        existing = {b["title"] for b in list_banners(client, active_only=False)}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
        for banner in HERO_BANNERS + PROMO_BANNERS:
            if banner["title"] in existing:
                self.stdout.write(f"  Found banner: {banner['title']}")
                continue
            doc_id = self._next(client, BANNER_INDEX)
            banner_type = "hero" if banner in HERO_BANNERS else "promotion"
            doc = build_banner_document(
                doc_id,
                {
                    **banner,
                    "type": banner_type,
                    "image": _img(f"banner-{banner['title'].lower().replace(' ', '-')}", 1600, 600),
                    "is_active": True,
                    "start_at": now,
                },
            )
            client.index(index=BANNER_INDEX, id=doc_id, document=doc, refresh="wait_for")
            self.stdout.write(f"  Created {banner_type} banner: {banner['title']}")

    # ------------------------------------------------------------------

    def _enrich_products(self, client):
        resp = client.search(index=product_store.PRODUCT_INDEX, query={"match_all": {}}, size=1000)
        hits = resp["hits"]["hits"]
        if not hits:
            self.stdout.write("  No products to enrich.")
            return

        rng = random.Random(20260725)
        featured_ids = rng.sample([h["_id"] for h in hits], min(8, len(hits)))
        sale_ids = rng.sample([h["_id"] for h in hits], min(6, len(hits)))
        flash_ends = datetime.now(timezone.utc) + timedelta(days=1, hours=3)

        updated = 0
        for hit in hits:
            source = hit["_source"]
            changed = {}
            if hit["_id"] in featured_ids and not source.get("is_featured"):
                changed["is_featured"] = True
            if "total_sales" not in source or source.get("total_sales") is None:
                changed["total_sales"] = rng.randint(20, 1200)
            if hit["_id"] in sale_ids and not source.get("flash_sale"):
                changed["flash_sale"] = True
                changed["flash_sale_price"] = round(float(source.get("price", 0)) * 0.7, 2)
                changed["flash_sale_ends_at"] = flash_ends.strftime("%Y-%m-%dT%H:%M:%S.%f")
            if not changed:
                continue
            source.update(changed)
            client.index(index=product_store.PRODUCT_INDEX, id=hit["_id"], document=source, refresh="wait_for")
            updated += 1
        self.stdout.write(f"  Enriched {updated} product(s) with homepage flags.")

    def _next(self, client, index):
        resp = client.search(index=index, size=0, aggs={"max_id": {"max": {"field": "id"}}})
        max_id = resp["aggregations"]["max_id"]["value"]
        return int(max_id) + 1 if max_id else 1
