# PixelForge Backend

Backend for PixelForge — Django REST Framework microservices behind an nginx gateway.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Client (Browser / SPA)                      │
└──────────────────────────┬──────────────────────────────────────────┘
                           │  /api/auth/*
                           │  /api/products/*
                           ▼
              ┌────────────────────────┐
              │     nginx gateway      │
              │       :80              │
              │  (Docker, proxy)       │
              └────────┬───────────────┘
                       │
            ┌──────────▼───────────┐
            │   auth-service       │       ┌──────────────────────┐
            │   Django DRF :8001   │◄──────│  PostgreSQL :5432    │
            │   (Docker container)  │       │  DB: pixelforge      │
            │                       │       │  User, Role, Profile │
            └──────────┬───────────┘       └──────────────────────┘
                       │
                       │  SharedTokenAuthentication
                       │  (SHARED_AUTH_SECRET, Bearer)
                       │
            ┌──────────▼───────────┐       ┌──────────────────────────┐
             │  search-service     │◄──────│  Elasticsearch           │
            │  Django DRF :8002    │       │  products index          │
            │  (Docker container)   │       │  + SQLite (logs/alerts)  │
            │                       │       └──────────────────────────┘
            └───────────────────────┘
```

### Key design decisions

| Concern | Decision |
|---------|----------|
| **API gateway** | nginx routes `/api/auth/*` → auth-service and `/api/products/*` → search-service. Single entry point for all clients. |
| **Auth service** | Own Django project backed by PostgreSQL. Manages users, roles (`admin`, `buyer`, `inventory_manager`), registration, login, and password reset. Issues shared signed access tokens (`access_token`, 8h TTL, payload `id:role:is_staff`). |
| **Product service** | Own Django project backed by SQLite (ORM for `InventoryLog`/`LowStockAlert` rows) and Elasticsearch (all product data). Elasticsearch enables full-text search, filtering, sorting, and pagination at scale. |
| **Shared auth** | Both services share `SHARED_AUTH_SECRET` to sign/verify JWT-like access tokens. The search-service uses `SharedTokenAuthentication` (Bearer header) to authenticate admin requests. No token → 401; non-admin role → 403. |
| **Deployment** | All services run in Docker containers orchestrated by `docker-compose`. Each service runs gunicorn as the WSGI server. |
| **CORS** | Open (`CORS_ALLOW_ALL_ORIGINS = True`) in auth-service for local dev. |
| **Uniform responses** | All endpoints return `{message, status_code, data}` via the `APIResponse` wrapper. |

### Request flow

1. Client sends request to nginx (`:80`).
2. nginx proxies to the correct upstream service based on path prefix.
3. **Auth requests** (`/api/auth/*`) → auth-service validates against PostgreSQL, returns `APIResponse`.
4. **Product requests** (`/api/products/*`) → search-service:
   - Public endpoints (list, detail, meta) query Elasticsearch directly.
   - Admin endpoints verify the shared access token via `SharedTokenAuthentication`.
   - Writes (create/update/delete/stock) also write `InventoryLog` rows to SQLite and may create/resolve `LowStockAlert` rows.
5. Response flows back through nginx to the client.

## Structure

```
PixelForgebackend/
├── .venv/                       # Python virtual environment (git-ignored)
├── README.md
├── docker-compose.yml
├── requirements.txt
├── nginx/
│   └── nginx.conf               # Gateway: /api/auth/ and /api/products/ → services
├── auth-service/                # Authentication microservice (PostgreSQL)
│   ├── Dockerfile               # gunicorn on :8000
│   ├── manage.py
│   ├── db.sqlite3               # Present but not used (settings use PostgreSQL)
│   ├── config/
│   │   ├── settings.py          # DB, CORS, DRF
│   │   └── urls.py              # Root URLconf → /api/auth/
│   └── apps/
│       ├── models.py            # Role, UserProfile
│       ├── serializers.py       # Register / login / password-reset serializers
│       ├── views.py             # Auth API views
│       ├── urls.py              # /api/auth/* routes
│       ├── responses.py         # Uniform APIResponse wrapper
│       ├── admin.py, apps.py, tests.py
│       ├── management/commands/seed_users.py   # Roles + demo users
│       └── migrations/0001_initial.py
└── search-service/             # Product microservice (SQLite + Elasticsearch)
    ├── Dockerfile               # gunicorn on :8001
    ├── manage.py
    ├── db.sqlite3
    ├── .env / .env.example      # Elasticsearch connection config
    ├── config/
    │   ├── settings.py          # SQLite DB, Elasticsearch via django-environ
    │   └── urls.py              # /api/products/
    └── apps/
        ├── elasticsearch.py     # get_elasticsearch_client() (cached client)
        ├── product_store.py     # ES mapping, doc building, admin query/sort/pagination
        ├── authentication.py    # SharedTokenAuthentication (Bearer) + decode_access_token
        ├── permissions.py       # IsAdmin permission (role == "admin")
        ├── serializers.py       # ProductSerializer + Stock/Log/Alert serializers
        ├── views.py             # Product CRUD, stock, history, inventory, low-stock views
        ├── models.py            # InventoryLog, LowStockAlert
        ├── urls.py              # /api/products/* routes
        └── admin.py, apps.py, tests.py
```

## Services & endpoints

### auth-service → `http://localhost:8001`

| Method | Endpoint                         | Purpose                                  | Auth |
|--------|----------------------------------|------------------------------------------|------|
| POST   | `/api/auth/register/buyer/`      | Register a buyer account                 | Public |
| POST   | `/api/auth/create/inventory-manager/` | Create an inventory manager          | Admin |
| POST   | `/api/auth/login/`               | Log in with **username or email**        | Public |
| POST   | `/api/auth/forgot-password/`     | Generate a reset token for a buyer       | Public |
| POST   | `/api/auth/reset-password/`      | Set a new password using user_id + token | Public |

All responses use a uniform shape from `APIResponse`:

```json
{ "message": "...", "status_code": 200, "data": { ... } }
```

### search-service → `http://localhost:8002`

All product data lives in the Elasticsearch `products` index; writes index/refresh immediately. The Django ORM is used only for `InventoryLog` and `LowStockAlert` rows.

| Method | Endpoint                               | Purpose                                      | Auth |
|--------|----------------------------------------|----------------------------------------------|------|
| GET    | `/api/products/`                       | List / search / filter / sort / paginate     | Public |
| GET    | `/api/products/meta/`                  | Filter options `{categories, brands}`        | Public |
| POST   | `/api/products/`                       | Create a product (multipart)                 | Admin |
| GET    | `/api/products/<id>/`                  | Retrieve a single product                    | Public |
| PUT    | `/api/products/<id>/`                  | Full update (multipart)                      | Admin |
| PATCH  | `/api/products/<id>/`                  | Partial update                               | Admin |
| DELETE | `/api/products/<id>/`                  | Delete a product (removes open low-stock alerts) | Admin |
| PATCH  | `/api/products/<id>/stock/`            | Adjust stock (`stock_quantity` or `delta`, optional `note`) | Admin |
| GET    | `/api/products/<id>/stock/history/`    | Stock movement history for one product       | Public |
| GET    | `/api/products/inventory/logs/`        | Inventory movement logs (filter `product_id`, `action`) | Public |
| GET    | `/api/products/inventory/low-stock/`   | Low-stock alerts (`all=1` includes resolved) | Public |
| PATCH  | `/api/products/inventory/low-stock/<id>/resolve/` | Mark an alert resolved               | Admin |

**Admin auth:** the auth-service issues a shared signed access token at login (`access_token` in the
login response, payload `id:role:is_staff`, 8h TTL). The search-service verifies it via
`SharedTokenAuthentication` (Bearer header, `SHARED_AUTH_SECRET` in both services). No/invalid token → **401**,
valid token with a non-`admin` role → **403**. The catalogue (list/detail/meta) is public — same data the
storefront shows; all create/update/delete/stock changes and the stock history, movement logs and
low-stock endpoints require an admin token.

#### Public search (GET `/api/products/`)

With no query parameters the full catalogue is returned. When any of `page/page_size/search/category/
brand/stock_status/min_price/max_price/status/sort` is present, admin-style list mode is used (search +
term filters + price range + sort + pagination).

| Param           | Example                              | Behavior                                             |
|-----------------|--------------------------------------|------------------------------------------------------|
| `name` (or `q`) | `?name=iphone`                       | OR wildcard match against name / brand / specification (storefront) |
| `brand`         | `?brand=Apple` / `?brand=Apple,Samsung` | Case-insensitive partial match against brand name    |
| `specification` | `?specification=5G` / `?specification=5G,SSD` | Case-insensitive partial match against description, short description, size, color and tags |
| `search`        | `?search=samsung`                    | Wildcard match against name keyword / brand / SKU    |
| `category` / `brand` | `?category=Smartphones`          | Term filter (exact)                                  |
| `status`        | `?status=active`                     | Term filter (`active`/`inactive`/`draft`)            |
| `stock_status`  | `?stock_status=in_stock`             | `in_stock`/`low_stock`/`out_of_stock` (scripted)     |
| `min_price`/`max_price` | `?min_price=100&max_price=1000` | Price range filter                                   |
| `sort`          | `?sort=-price`                       | `price`, `name`, `created_at`, `updated_at`, `stock_quantity` (±) |
| `page`/`page_size` | `?page=2&page_size=10`           | Pagination; response has `count/results/page/page_size/total_pages/has_next/has_previous` |

#### Stock rules

- Negative stock is coerced to `0`.
- When `0 < stock_quantity <= min_stock_alert`, a `LowStockAlert` is created automatically; restocking
  above the threshold auto-resolves it.
- `status` (active/inactive/draft) is independent of stock; stock display is derived from quantity
  (>0 in stock, ==0 out of stock, <=min low stock).
- Every create/update/delete/stock change writes an `InventoryLog` (before/after/change/actor/note).

#### Images

Uploaded via multipart `images` files (jpg/jpeg/png/webp, ≤ 5 MB, saved to `MEDIA_ROOT` under
`products/<uuid>.<ext>` and served at `/media/...` in DEBUG). Existing image URLs may also be passed as
string `images` fields. `thumbnail` defaults to the first image.

Configured via `.env` (`ELASTICSEARCH_HOSTS`, `ELASTICSEARCH_USER`, `ELASTICSEARCH_PASSWORD`, `ELASTICSEARCH_TIMEOUT`).

## Setup (local dev)

### auth-service

Requirements: Python 3.10+, PostgreSQL running on `localhost:5432`.

```bash
cd PixelForgebackend/auth-service
source ../.venv/bin/activate          # or python3 -m venv ../.venv && pip install -r ../requirements.txt
pip install -r ../requirements.txt

createdb pixelforge                   # create DB pixelforge (user postgres)
python manage.py migrate
python manage.py seed_users           # creates roles + demo users
python manage.py runserver 0.0.0.0:8001
```

`config/settings.py` points to database `pixelforge` (user `postgres`, password `3654`, port `5432`). CORS is open (`CORS_ALLOW_ALL_ORIGINS = True`) for local development.

### search-service

```bash
cd PixelForgebackend/search-service
source ../.venv/bin/activate
pip install -r ../requirements.txt

cp .env.example .env                  # set your Elasticsearch credentials
python manage.py runserver 0.0.0.0:8002
```

Uses SQLite (`db.sqlite3`) and reads Elasticsearch settings from `.env`.

### Demo users (from `seed_users`)

| Username           | Email                  | Password        | Role              |
|--------------------|------------------------|-----------------|-------------------|
| `admin`            | admin@pixelforge.com   | `admin123`      | admin             |
| `buyer`            | buyer@pixelforge.com   | `buyer123`      | buyer             |
| `inventory_manager`| inventory@pixelforge.com | `inventory123` | inventory_manager |

> Only the `admin` role can create/update/delete products and adjust inventory (others get HTTP 403).

## Docker / nginx

```bash
cd PixelForgebackend
docker compose up --build
```

- `auth-service` → `http://localhost:8001` (gunicorn on :8000 inside the container)
- `search-service` → `http://localhost:8002` (gunicorn on :8001 inside the container)
- `nginx` → `http://localhost:80`, proxying `/api/auth/*` → auth-service:8000 and `/api/products/*` → search-service:8000

> Note: nginx.conf expects `search-service:8000`, but the search-service Dockerfile binds gunicorn to `:8001`. Align these (e.g. change the nginx upstream to `search-service:8001`) before using the gateway for products.
# PixelForge
