# search-service

Product microservice for PixelForge — Django REST Framework backed by SQLite (ORM for logs/alerts) and Elasticsearch (all product/catalog data). Handles product CRUD, inventory management, low-stock alerts, and homepage catalog (categories, brands, banners).

## Architecture

```
Client (Browser)
    │
    │  /api/products/*
    ▼
nginx (:80) ──► search-service (:8002) ──┬── Elasticsearch (:9200)
                                           │   products index
                                           │   categories index
                                           │   brands index
                                           │   banners index
                                           │
                                           └── SQLite (db.sqlite3)
                                               InventoryLog
                                               LowStockAlert
                                               RecentlyViewed
```

### Data stores

| Store | Purpose |
|-------|---------|
| Elasticsearch | All product data, catalog data (categories, brands, banners), full-text search, filtering, sorting |
| SQLite (`db.sqlite3`) | `InventoryLog`, `LowStockAlert`, `RecentlyViewed` rows (ORM-managed) |

### Key components

| Component | Path | Purpose |
|-----------|------|---------|
| **models.py** | `apps/models.py` | `RecentlyViewed`, `InventoryLog`, `LowStockAlert` |
| **serializers.py** | `apps/serializers.py` | `ProductSerializer`, `StockUpdateSerializer`, `CatalogSerializer`, `InventoryLogSerializer`, `LowStockAlertSerializer`, `CatalogSerializer` |
| **views.py** | `apps/views.py` | All API views: product CRUD, stock, history, inventory logs, low-stock alerts, catalog (categories/brands/banners), homepage aggregation (recommended, recently-viewed) |
| **product_store.py** | `apps/product_store.py` | ES product index operations: `get_product`, `create_document`, `replace_document`, `delete_product_doc`, `search_products`, `build_document`, `get_filter_options`, `next_product_id` |
| **catalog_store.py** | `apps/catalog_store.py` | ES catalog indices (categories, brands, banners): CRUD, list with active filters, product count aggregations |
| **elasticsearch.py** | `apps/elasticsearch.py` | `get_elasticsearch_client()` — lazily creates and caches a module-level ES client with optional basic_auth |
| **authentication.py** | `apps/authentication.py` | `SharedTokenAuthentication` (DRF auth backend, Bearer token) and `AuthUser` lightweight principal |
| **permissions.py** | `apps/permissions.py` | `IsAdmin` permission class (role == `"admin"`) |
| **urls.py** | `apps/urls.py` | Routes for products, catalog, inventory, and homepage aggregation |
| **urls.py (config)** | `config/urls.py` | Root URLconf — includes `apps.urls` under `/api/` |
| **settings.py** | `config/settings.py` | SQLite DB, Elasticsearch via django-environ (`.env`), `SHARED_AUTH_SECRET`, media config |
| **admin.py** | `apps/admin.py` | Registers `InventoryLogAdmin` and `LowStockAlertAdmin` |
| **seed_catalog.py** | `apps/management/commands/seed_catalog.py` | Seeds demo categories, brands, banners, and enriches products with featured/sale flags |
| **Dockerfile** | `Dockerfile` | Python 3.10-slim, gunicorn on `:8001` |

### Endpoints

#### Products

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/products/` | Public | List/search/filter/sort/paginate (admin params activate admin mode) |
| GET | `/api/products/meta/` | Public | Filter options `{categories, brands}` |
| GET | `/api/products/recommended/` | Public (auth optional) | Personalized recommendations or trending products |
| GET | `/api/products/recently-viewed/` | Public (auth optional) | Server-side recently-viewed or guest history |
| POST | `/api/products/` | Admin | Create product (multipart images) |
| GET | `/api/products/<id>/` | Public | Retrieve single product |
| PUT | `/api/products/<id>/` | Admin | Full update (multipart) |
| PATCH | `/api/products/<id>/` | Admin | Partial update |
| DELETE | `/api/products/<id>/` | Admin | Delete product + resolve open low-stock alerts |
| PATCH | `/api/products/<id>/stock/` | Admin | Adjust stock (`stock_quantity` or `delta`, optional `note`) |
| GET | `/api/products/<id>/stock/history/` | Admin | Stock movement history |
| GET | `/api/products/inventory/logs/` | Admin | Inventory movement logs (filter by `product_id`, `action`) |
| GET | `/api/products/inventory/low-stock/` | Admin | Low-stock alerts (`all=1` includes resolved) |
| PATCH | `/api/products/inventory/low-stock/<id>/resolve/` | Admin | Mark alert resolved |

#### Catalog (categories, brands, banners)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/categories/` | Public | List active categories with product counts |
| POST | `/api/categories/` | Admin | Create category |
| GET | `/api/categories/<id>/` | Public | Category detail |
| PUT | `/api/categories/<id>/` | Admin | Update category |
| DELETE | `/api/categories/<id>/` | Admin | Delete category |
| GET | `/api/brands/` | Public | List active brands with product counts |
| POST | `/api/brands/` | Admin | Create brand |
| GET | `/api/brands/<id>/` | Public | Brand detail |
| PUT | `/api/brands/<id>/` | Admin | Update brand |
| DELETE | `/api/brands/<id>/` | Admin | Delete brand |
| GET | `/api/banners/` | Public | List active banners (optionally by `type=hero|promotion`) |
| POST | `/api/banners/` | Admin | Create banner |
| GET | `/api/banners/<id>/` | Public | Banner detail |
| PUT | `/api/banners/<id>/` | Admin | Update banner |
| DELETE | `/api/banners/<id>/` | Admin | Delete banner |

### Authentication & authorization

- **Public endpoints** (list, detail, meta, recommended, recently-viewed, catalog browse): No authentication required.
- **Admin endpoints** (create/update/delete products, stock changes, inventory logs, low-stock alerts, catalog CRUD): Require a valid shared access token with `admin` role.
- **Token format**: Bearer token issued by auth-service at login. Verified by `SharedTokenAuthentication` using `SHARED_AUTH_SECRET`. Payload: `{user_id}:{role}:{is_staff}`, 8h TTL.
- **Auth failure**: Missing/invalid token → `401`; valid token with non-admin role → `403`.

### Search & filtering (GET `/api/products/`)

When admin-only parameters (`page`, `page_size`, `search`, `category`, `stock_status`, `min_price`, `max_price`, `status`, `sort`, `order`, `limit`, `featured`, `flash_sale`) are present, admin list mode activates with pagination, term filters, price range, and sort.

Without admin params, the endpoint behaves as the public storefront search with OR wildcard matching across name, brand, and specification fields.

### Stock rules

- Negative stock is coerced to `0`.
- When `0 < stock_quantity <= min_stock_alert`, a `LowStockAlert` is created automatically; restocking above the threshold auto-resolves it.
- `status` (active/inactive/draft) is independent of stock.
- Every create/update/delete/stock change writes an `InventoryLog` (before/after/change/actor/note).

### Images

Uploaded via multipart `images` files (jpg/jpeg/png/webp, ≤ 5 MB, saved to `MEDIA_ROOT` under `products/<uuid>.<ext>`). Existing image URLs may also be passed as string `images` fields. `thumbnail` defaults to the first image.

### Elasticsearch configuration

Configured via `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `ELASTICSEARCH_HOSTS` | `http://localhost:9200` | Comma-separated list of ES hosts |
| `ELASTICSEARCH_TIMEOUT` | `30` | Request timeout in seconds |
| `ELASTICSEARCH_USER` | — | Basic auth username |
| `ELASTICSEARCH_PASSWORD` | — | Basic auth password |

### Setup (local dev)

```bash
cd PixelForgebackend/search-service
source ../.venv/bin/activate
pip install -r ../requirements.txt

cp .env.example .env                  # set Elasticsearch credentials
python manage.py migrate
python manage.py seed_catalog         # seed demo catalog data
python manage.py runserver 0.0.0.0:8002
```

- DB: SQLite (`db.sqlite3`)
- Elasticsearch: must be running on `localhost:9200` (or the address in `.env`)
- `SHARED_AUTH_SECRET` must match the auth-service value for cross-service auth

### Docker

```bash
cd PixelForgebackend
docker compose up --build
```

`search-service` is exposed on host port `8002` (gunicorn binds `:8001` inside the container).