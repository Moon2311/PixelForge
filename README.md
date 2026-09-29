# PixelForge Backend

Backend for PixelForge: a **modular monolith**. One Django project and one
process serve every module (authentication, catalog, search, cart) from
**one PostgreSQL database**. PostgreSQL handles both the transactional data
and all search, filtering, sorting and faceting.

```
                 React frontend
                        │
                        ▼
            Django modular monolith :8000
                        │
     ┌──────────────┬───┴──────────┬──────────────┐
     ▼              ▼              ▼              ▼
Authentication   Catalog  ◄──── Search          Cart
     │              ▲              │              │
     │              └─ in-process calls (no HTTP) ┘
     └──────────────┴──────┬───────┴──────────────┘
                           ▼
                      PostgreSQL
            (transactions + search/filtering)
```

There is no Elasticsearch, message broker, Redis/Celery or internal HTTP
between modules.

## Quick start

**Docker (one command):**

```bash
docker compose up --build
```

The backend is at `http://localhost:8000` and runs migrations on start. PostgreSQL is published on host port `5433`.
Seed demo data with:

```bash
docker compose exec backend python manage.py seed_users
docker compose exec backend python manage.py seed_inventory --count 50
docker compose exec backend python manage.py seed_catalog
```

**Local (without Docker)** uses [uv](https://docs.astral.sh/uv/) and requires PostgreSQL on `localhost:5432`.

```bash
uv sync                               # creates .venv from pyproject.toml + uv.lock
cp .env.example .env                  # set DATABASE_URL / SECRET_KEY
createdb pixelforge
uv run manage.py migrate
uv run manage.py seed_users           # roles + demo users (optional)
uv run manage.py runserver            # http://localhost:8000
```

**Tests:** `uv run manage.py test` (needs a PostgreSQL user that can create the test database and the `pg_trgm` extension).

**Dependencies** live in `pyproject.toml` and are pinned in `uv.lock`: `uv add <package>` to add one, `uv lock --upgrade` to refresh.

## Project layout

```
PixelForge/
├── manage.py
├── pyproject.toml, uv.lock   # dependencies (uv)
├── Dockerfile, docker-compose.yml, .env.example
├── config/                 # settings.py (one DATABASE_URL), urls.py, wsgi.py, asgi.py
├── nginx/nginx.conf        # optional production reverse proxy (single upstream)
├── scripts/
│   └── merge_legacy_databases.sh   # one-off copy from the old per-service databases
└── apps/
    ├── common/             # health check, uniform APIResponse, test_db_connection
    ├── authentication/     # Role/UserProfile, register/login/logout/password reset,
    │                       # Bearer token auth (tokens.py, authentication.py), RBAC permissions
    ├── catalog/            # catalog models, /api/catalog/ viewsets, flat /api/products|
    │   │                   # categories|brands|banners/ endpoints, inventory, search stats
    │   ├── selectors.py    # public read API used by other modules (e.g. cart)
    │   ├── product_store.py / catalog_store.py   # flat endpoint documents <-> models
    │   ├── inventory.py    # transaction-safe stock changes, logs, low-stock alerts
    │   └── stats.py        # denormalized price/stock/rating columns (kept in sync)
    ├── search/             # PostgreSQL search
    │   ├── filters.py      # keyword/filter/order builders (Q expressions)
    │   ├── selectors.py    # base querysets
    │   ├── services.py     # ProductSearch, autocomplete, category/brand search, flat search
    │   ├── documents.py    # result documents (prefetched, no N+1)
    │   └── views.py, urls.py
    └── cart/               # user and guest carts; validates products via catalog.selectors
```

Dependency direction: `search → catalog`, `cart → catalog.selectors`, and
everything → `authentication`/`common` for auth and responses. The catalog
doesn't import cart. Its flat `GET /api/products/` view calls
`search.services`, since that endpoint is the storefront search.

## API

All paths are unchanged from the microservice version. Only the host
changed: everything is now served on `:8000`.

| Prefix | Module | Notes |
|--------|--------|-------|
| `/api/auth/` | authentication | register, login, logout, password reset |
| `/api/catalog/` | catalog | DRF CRUD for categories, subcategories, brands, products, variants, images, attributes, specs, prices, inventory, reviews |
| `/api/search/` | search | `products/`, `autocomplete/`, `categories/`, `brands/` |
| `/api/products/`, `/api/categories/`, `/api/brands/`, `/api/banners/` | catalog (+search) | flat storefront/admin endpoints used by the React app |
| `/api/cart/` | cart | user cart (`Bearer`), guest cart (`X-Guest-Session`), merge |
| `/api/health/` | common | Django + PostgreSQL |
| `/admin/` | Django admin | |

Responses keep their existing envelopes. The auth and cart modules return
`{message, status_code, data}`; the catalog and search modules return
`{message, status, data}`.

### Authentication

| Method | Endpoint | Purpose | Auth |
|--------|----------|---------|------|
| POST | `/api/auth/register/buyer/` | Register a buyer | Public |
| POST | `/api/auth/create/inventory-manager/` | Create an inventory manager | Staff |
| POST | `/api/auth/login/` | Log in with **username or email**; returns `{user, access_token}` | Public |
| POST | `/api/auth/logout/` | End the Django session (the client drops its token) | Public |
| POST | `/api/auth/forgot-password/` | Email a reset link to an active buyer (same response either way) | Public, 5/hour |
| POST | `/api/auth/reset-password/` | `{uid, token, new_password, confirm_password}` | Public, 5/hour |

- **Access token:** `Authorization: Bearer <access_token>`. The token is
  signed with `SECRET_KEY` and expires after `ACCESS_TOKEN_MAX_AGE`
  (default 8h). It only carries the user id. Each request loads the active
  `User` and its role from the database, so role changes and deactivation
  apply immediately. It's stateless, so logout can't revoke a token early.
- **Password reset:** the link is `PASSWORD_RESET_URL?uid=…&token=…`. The
  tokens come from Django's `default_token_generator`, so they're
  single-use and expire after `PASSWORD_RESET_TIMEOUT` (1h). Only buyers can
  reset.
- **Roles (RBAC):** `admin`, `catalog_manager`, `inventory_manager`,
  `customer` are enforced by `apps/authentication/permissions.py`, reading
  `user.profile.role`. `seed_users` creates `admin`, `buyer` and
  `inventory_manager`; registration assigns `buyer`.

### Search (`/api/search/products/`)

Query params: `q`/`search`, `category_id`, `category` (slug),
`subcategory_id`, `subcategory`, `brand_id`, `brand` (slug), `min_price`,
`max_price`, `in_stock`, `is_featured`, `min_rating`, `ordering` (`name`,
`price`, `rating`, `review_count`, `created_at`, `relevance`, `-` prefix
for descending), `page`, `page_size` (≤100), `facets`.

**Matching:** every word must appear as a case-insensitive substring
somewhere in the product: name, SKU (partial SKU/part numbers work),
descriptions, specification text/values, color, size, tags, brand,
category or subcategory. Only `active` products are returned unless a
`status` filter is given. Relevance ranks exact name > name prefix > name
contains > SKU match. Facets cover categories, subcategories, brands, price
ranges, ratings and availability.

**Storefront search** (`GET /api/products/?name=…&brand=…&specification=…`)
matches products where *any* comma-separated term matches, with the same
fields, and lists name matches first. The admin list mode (`page`, `sort`,
`stock_status`, … params) is unchanged.

### Cart

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET / DELETE | `/api/cart/` | Get or clear the user's cart (`Bearer`) |
| POST | `/api/cart/items/` | `{product_id, quantity}`; adds or increments |
| PATCH / DELETE | `/api/cart/items/<id>/` | Set quantity / remove (own items only) |
| GET / DELETE | `/api/cart/guest/` | Guest cart for the `X-Guest-Session` header |
| POST | `/api/cart/guest/items/` | Add to guest cart |
| PATCH / DELETE | `/api/cart/guest/items/<id>/` | Set quantity (`0` removes) / remove |
| POST | `/api/cart/merge/` | `{session_id}`: merge a guest cart into the user's cart (qty capped at 99) |

Products are validated in-process through `apps.catalog.selectors.get_product`.
Unknown or deleted products return `404`.

## How search works on PostgreSQL

Every filter, sort, count and facet is computed inside PostgreSQL. No
product lists are filtered in Python.

- **Trigram indexes (`pg_trgm`):** substring matching (`UPPER(x) LIKE
  '%TERM%'`) is index-assisted.
  - `product_search_trgm` indexes one immutable expression that joins the
    product's text columns (`catalog.models.product_search_text()`).
  - `product_name_trgm` serves autocomplete.
  - `spec_value_trgm` serves specification values.
- **Related-name matches:** brand, category and subcategory names are
  resolved to id lists first (small tables). The final `OR` then only
  contains indexable product-table predicates (`search_text LIKE`,
  `brand_id IN (…)`, `pk IN (…)`), which PostgreSQL combines with a
  **BitmapOr**.
- **Denormalized stats:** `Product.price_min/price_max/in_stock/rating_avg/review_count`
  replace per-row subqueries for price, stock and rating filters, sorts and
  facets.
  - `apps/catalog/stats.py` recomputes them with one set-based `UPDATE`
    whenever a price, inventory row, variant or review is saved or deleted,
    in the same transaction.
  - `manage.py refresh_product_stats` rebuilds them all.
  - They're indexed as `(status, price_max)` and `(status, rating_avg)`.
- **Default list sort:** `product_updated_desc_idx` backs the default
  `updated_at DESC` sort of `GET /api/products/`.
- **No N+1:** result documents are built from one prefetched queryset per
  page (variants, prices, inventory, attributes, images, specs).

Measured on 60,000 products (local PostgreSQL 16): keyword queries take 1–7 ms
with the trigram/BitmapOr plan. A full `/api/search/products/?q=…` page with
facets takes about 100 ms, and a price-filtered search with no keyword about
150 ms.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `postgres://postgres:postgres@localhost:5432/pixelforge` | The single database |
| `SECRET_KEY` | dev-only value | Django secret; also signs access tokens |
| `DEBUG` | `True` | |
| `ALLOWED_HOSTS` | `*` | Comma-separated |
| `ACCESS_TOKEN_MAX_AGE` | `28800` | Access-token lifetime (seconds) |
| `PASSWORD_RESET_URL` | `http://localhost:3000/reset-password` | Frontend reset page |
| `PASSWORD_RESET_TIMEOUT` | `3600` | Reset-link lifetime (seconds) |
| `PASSWORD_RESET_THROTTLE_RATE` | `5/hour` | Forgot/reset rate limit |
| `EMAIL_*`, `DEFAULT_FROM_EMAIL` | console backend | Outgoing email |

## Management commands

| Command | Purpose |
|---------|---------|
| `seed_users` | Roles (`admin`, `buyer`, `inventory_manager`) and demo users |
| `seed_inventory --count N` | Demo products with variants, prices, stock, images, specs |
| `seed_catalog` | Demo categories, brands, banners; featured/flash-sale flags |
| `refresh_product_stats` | Rebuild denormalized search stats |
| `cleanup_guest_carts` | Delete expired guest carts |
| `setup_rbac` | Django groups/permissions mirroring the roles |
| `test_db_connection` | Check database connectivity |

## Migrating data from the old per-service databases

The monolith keeps the old table names (`db_table = "apps_<model>"`), so
rows copy over unchanged:

```bash
uv run manage.py migrate      # create the schema in the new database
AUTH_DATABASE_URL=postgres://…/auth-service \
CATALOG_DATABASE_URL=postgres://…/product_catalog_db \
CART_DATABASE_URL=postgres://…/cart_db \
scripts/merge_legacy_databases.sh
```

The script only reads the source databases, refuses to write into
non-empty target tables, resets sequences and rebuilds search stats.
Outbox events aren't copied, because the outbox was removed. Django
groups/permissions aren't copied either, since their content-type ids
differ per database; re-run `setup_rbac`.
