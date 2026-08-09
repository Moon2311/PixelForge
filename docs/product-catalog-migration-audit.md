# Product Catalog Migration Audit

**Date:** 2026-08-09
**Status:** Phase 0 — Baseline Complete

---

## Current Architecture

The PixelForge backend is a Django-based microservices project with two services:

```
CLIENT → Nginx (port 80) → auth-service (port 8001)
                          → search-service (port 8002)
                          → PostgreSQL (port 5440)
                          → Elasticsearch (port 9200)
```

**Key Finding:** The search-service is a **monolithic combined catalog+search service**. It already owns PostgreSQL as the source of truth, implements the outbox pattern, and has ES as a read projection. The migration task is to **split it into separate product-catalog-service and search-service**, not to build from scratch.

---

## Current Services

| Service | Framework | Port | Database | Status |
|---------|-----------|------|----------|--------|
| auth-service | Django REST | 8001 (host:8000) | SQLite (auth) | Running |
| search-service | Django REST | 8002 (host:8001) | PostgreSQL (product_catalog_db) | Running |
| common-service | — | — | — | Empty directory |

---

## Current Databases

| Database | Engine | Used By | Purpose |
|----------|--------|---------|---------|
| product_catalog_db | PostgreSQL 16 | search-service | Product catalog source of truth |
| auth SQLite | SQLite | auth-service | User accounts, roles |
| search-service db.sqlite3 | SQLite | search-service | Appears unused (PostgreSQL is active) |

### PostgreSQL Roles (product_catalog_db)

| Role | Permissions | Usage |
|------|-------------|-------|
| product_catalog_owner | Full privileges | Migrations, schema changes |
| product_catalog_app | SELECT, INSERT, UPDATE, DELETE | Django runtime |
| product_catalog_readonly | SELECT | Reporting/analytics |

---

## Current Elasticsearch Indices

| Index | Alias | Purpose |
|-------|-------|---------|
| products-v1 | products | Product search documents |
| categories-v1 | categories | Category search documents |
| brands-v1 | brands | Brand search documents |
| (banners) | banners | Banner documents (legacy ES-backed) |

**Versioned indices with alias support are already implemented** (search_indices.py).

---

## Current APIs

### Nginx Routes

| Route | Service | Purpose |
|-------|---------|---------|
| /api/auth/ | auth-service | Authentication |
| /api/catalog/ | search-service | PostgreSQL-backed catalog CRUD |
| /api/search/ | search-service | Elasticsearch-backed search |
| /api/products/ | search-service | Legacy ES-backed product APIs |
| /api/categories/ | search-service | Legacy ES-backed category APIs |
| /api/brands/ | search-service | Legacy ES-backed brand APIs |
| /api/banners/ | search-service | Legacy ES-backed banner APIs |
| /api/health/ | search-service | Health check |

### Catalog API Endpoints (PostgreSQL-backed, catalog_urls.py)

- Categories CRUD: /api/catalog/categories/
- Subcategories CRUD: /api/catalog/subcategories/
- Brands CRUD: /api/catalog/brands/
- Products CRUD: /api/catalog/products/
- Product Images: /api/catalog/products/{id}/images/
- Product Variants: /api/catalog/products/{id}/variants/
- Attributes: /api/catalog/attributes/
- Product Attributes: /api/catalog/products/{id}/attributes/
- Specifications: /api/catalog/products/{id}/specifications/
- Pricing: /api/catalog/variants/{id}/prices/ and /api/catalog/prices/{id}/
- Inventory: /api/catalog/variants/{id}/inventory/
- Inventory Logs: /api/catalog/inventory/logs/
- Low Stock Alerts: /api/catalog/inventory/low-stock/
- Reviews: /api/catalog/products/{id}/reviews/
- Search: /api/catalog/search/

### Search API Endpoints (ES-backed, search_views.py)

- Product Search: /api/search/products/
- Autocomplete: /api/search/autocomplete/
- Category Search: /api/search/categories/
- Brand Search: /api/search/brands/

### Legacy Endpoints (ES-backed, views.py)

- Products: /api/products/ (GET list, POST create)
- Product Detail: /api/products/{id}/
- Product Meta: /api/products/meta/
- Recommended: /api/products/recommended/
- Recently Viewed: /api/products/recently-viewed/
- Stock Update: /api/products/{id}/stock/
- Stock History: /api/products/{id}/stock/history/
- Inventory Logs: /api/products/inventory/logs/
- Low Stock: /api/products/inventory/low-stock/
- Categories: /api/categories/
- Brands: /api/brands/
- Banners: /api/banners/

---

## Current Authentication

- **Shared secret authentication** via `SharedTokenAuthentication` (uses `SHARED_AUTH_SECRET`)
- Token format: signed user_id:role:is_staff
- Token max age: 8 hours
- Auth user carries: pk, role, is_staff

---

## Current Authorization/RBAC

Roles defined in permissions.py:
- admin: Full catalog access
- catalog_manager: Product/category/brand management
- inventory_manager: Inventory management only
- customer: View + create/edit own reviews
- (public): Browse active catalog, search

---

## Current Data Model

### PostgreSQL Models (search-service/apps/models.py)

Already implements the full normalized schema:
- Category (with soft delete)
- Subcategory (FK to Category, unique_together)
- Brand (with soft delete)
- Product (with soft delete, FK to brand/category/subcategory)
- ProductImage (FK to Product)
- ProductVariant (with soft delete, FK to Product)
- Attribute (reusable)
- AttributeValue (FK to Attribute)
- ProductAttributeValue (junction)
- VariantAttributeValue (junction)
- ProductSpecification (FK to Product)
- VariantPrice (OneToOne to ProductVariant, Decimal pricing)
- Inventory (OneToOne to ProductVariant, stock tracking)
- InventoryLog (FK to ProductVariant, audit trail)
- LowStockAlert (FK to ProductVariant)
- ProductReview (FK to Product, user_id integer, rating 1-5)
- OutboxEvent (Transactional Outbox pattern)
- RecentlyViewed (Legacy)

---

## Current Event Infrastructure

### Event Publisher (event_publisher.py)
- Writes OutboxEvent within the same transaction as domain changes
- All event types defined: Product, Category, Subcategory, Brand, Image, Variant, Price, Inventory, Review events
- Event payloads include: event_id, event_type, aggregate_type, aggregate_id, version, occurred_at, data

### Event Relay (event_relay.py)
- Polls OutboxEvent table for pending events
- Publishes to Kafka (topic: catalog.{aggregate_type})
- Exponential backoff for retries
- Dead-letter queue support
- Graceful shutdown

### Event Consumers (event_consumers.py)
- CatalogEventConsumer: processes events and updates Elasticsearch
- Handles all event types (product, category, subcategory, brand, image, variant, price, inventory, review)
- All handlers are idempotent

### Event Consumer Relay (event_consumer_relay.py)
- Consumes from Kafka topics
- Routes to CatalogEventConsumer
- Graceful shutdown

### Event Contracts (event_contracts.py)
- Full event schema definitions with dataclasses
- Event validation
- Versioned events (current version: 1)

---

## Current Docker Architecture

```yaml
services:
  auth-service: port 8001:8000
  search-service: port 8002:8001 (depends on product-catalog-db)
  product-catalog-db: PostgreSQL 16, port 5440:5432
  nginx: port 80:80

volumes:
  product_catalog_data
```

---

## What Already Exists vs. Target

| Target Component | Current Status |
|-----------------|----------------|
| PostgreSQL source of truth | ✅ Already in search-service |
| Normalized PG models | ✅ Already implemented |
| Application RBAC | ✅ Already implemented |
| Database RBAC (roles) | ✅ Already implemented (init.sql) |
| Transactional Outbox | ✅ Already implemented |
| Event Publisher | ✅ Already implemented |
| Event Relay | ✅ Already implemented (needs Kafka) |
| Event Consumers | ✅ Already implemented |
| Search Document Builder | ✅ Already implemented |
| Search Features (full-text, facets, etc.) | ✅ Already implemented |
| Versioned ES Indices | ✅ Already implemented |
| Kafka infrastructure | ❌ Not deployed |
| Separate product-catalog-service | ❌ Combined in search-service |
| Separate search-service | ❌ Combined in search-service |

---

## Existing Problems

1. **Monolithic search-service**: Combines catalog CRUD, inventory, reviews, and search in one service
2. **Kafka not deployed**: Event relay and consumer relay code exists but Kafka isn't running
3. **Legacy ES-backed APIs**: Still serve /api/products/, /api/categories/, /api/brands/ from ES
4. **Banner API**: Banners are only in ES, not in PostgreSQL
5. **No product-catalog-service**: Needs to be extracted from search-service
6. **SQLite unused**: db.sqlite3 exists in search-service but PostgreSQL is active
7. **common-service empty**: Unused directory

---

## Migration Risks

1. **Service split without downtime**: Must keep existing APIs working during transition
2. **Kafka dependency**: Event relay needs Kafka to function
3. **Data consistency**: Both services need access to the same PostgreSQL initially
4. **Frontend compatibility**: All existing endpoints must remain functional
5. **ES downtime**: ES is the source of truth for legacy endpoints; must preserve

---

## Backward Compatibility Requirements

- /api/products/ must continue working
- /api/categories/ must continue working
- /api/brands/ must continue working
- /api/banners/ must continue working
- /api/catalog/* must continue working
- /api/search/* must continue working
- /api/auth/* must continue working
- Authentication mechanism must remain unchanged
- Response format must remain unchanged

---

## Migration Strategy

Since the search-service already contains the full catalog implementation, the migration is a **service split**, not a rewrite:

1. Create `product-catalog-service` by extracting catalog models, views, serializers, URLs from search-service
2. Deploy Kafka infrastructure
3. Update search-service to consume events and only handle search
4. Update Docker Compose and Nginx routing
5. Preserve all backward-compatible endpoints
