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
    ├── cart/               # user and guest carts; validates products via catalog.selectors
    ├── orders/             # checkout: orders from the user's cart, stock via catalog.inventory
    └── payments/           # JazzCash/Easypaisa payments for orders
        ├── services/       # __init__.py: create/verify/apply (locking, idempotency)
        │                   # jazzcash.py, easypaisa.py: provider protocol only
        └── webhooks/       # provider return URLs and IPN listeners
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
| `/api/orders/` | orders | checkout options, place order from the cart, order detail |
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
| POST | `/api/auth/login/` | Log in with **username or email**; returns `{user, access_token, refresh_token}` | Public |
| POST | `/api/auth/refresh/` | `{refresh_token}` → new `{access_token, refresh_token}` | Public |
| POST | `/api/auth/logout/` | End the Django session (the client drops its token) | Public |
| POST | `/api/auth/forgot-password/` | Email a reset link to an active buyer (same response either way) | Public, 5/hour |
| POST | `/api/auth/reset-password/` | `{uid, token, new_password, confirm_password}` | Public, 5/hour |

- **Access token:** `Authorization: Bearer <access_token>`. The token is
  signed with `SECRET_KEY` and expires after `ACCESS_TOKEN_MAX_AGE`
  (default 15 min). It only carries the user id. Each request loads the active
  `User` and its role from the database, so role changes and deactivation
  apply immediately. It's stateless, so logout can't revoke a token early.
- **Refresh token:** when the access token expires (the API answers 401),
  `POST /api/auth/refresh/` with `{refresh_token}` to get a new access token
  and a new refresh token. It expires after `REFRESH_TOKEN_MAX_AGE`
  (default 7 days), after which the user logs in again. Changing or
  resetting the password invalidates all existing refresh tokens.
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

### Orders

| Method | Endpoint | Purpose | Auth |
|--------|----------|---------|------|
| GET | `/api/orders/checkout-options/` | Shipping methods, payment methods (COD, Bank Deposit, and JazzCash/Easypaisa when configured), pickup location | Public |
| POST | `/api/orders/` | Place an order from the user's cart (see below) | `Bearer` |
| GET | `/api/orders/<number>/` | One of the user's orders | `Bearer` |

`POST /api/orders/` takes the contact, `delivery_method` (`ship`/`pickup`),
addresses, `shipping_method`, `payment_method` (`cod`/`bank_deposit`/`jazzcash`/`easypaisa`) and the
`items` and `total` the shopper saw. It returns 409 (with `data.problems`) if
the cart changed, a product is unavailable or out of stock, or a price
changed. On success it takes the stock (logged as `Order <number>`) and
empties the cart. Discount codes aren't supported yet. Bank Deposit, JazzCash
and Easypaisa orders start as `awaiting_payment`.

### Payments

| Method | Endpoint | Purpose | Auth |
|--------|----------|---------|------|
| POST | `/api/payments/create/` | Start paying an order: `{"order_id": "PF-…", "payment_method": "JAZZCASH" \| "EASYPAISA"}` | `Bearer` |
| GET | `/api/payments/<payment_id>/` | Verified payment status (re-checks open payments with the provider) | `Bearer` |
| POST | `/api/payments/jazzcash/callback/` | JazzCash `pp_ReturnURL` (browser, signed) → redirects to the storefront | Provider |
| POST | `/api/payments/jazzcash/ipn/` | JazzCash IPN / status update | Provider |
| GET | `/api/payments/easypaisa/callback/` | Easypaisa `postBackURL` (browser) → `Confirm.jsf`, then the storefront | Provider |
| GET/POST | `/api/payments/easypaisa/ipn/` | Easypaisa IPN | Provider |

See [Online payments](#online-payments-jazzcash-and-easypaisa) for the flow and setup.

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
| `ACCESS_TOKEN_MAX_AGE` | `900` | Access-token lifetime (seconds) |
| `REFRESH_TOKEN_MAX_AGE` | `604800` | Refresh-token lifetime (seconds) |
| `BANK_DEPOSIT_INSTRUCTIONS` | empty | Bank details shown at checkout for Bank Deposit |
| `PICKUP_LOCATION` | empty | Pickup address shown at checkout |
| `PASSWORD_RESET_URL` | `http://localhost:3000/reset-password` | Frontend reset page |
| `PASSWORD_RESET_TIMEOUT` | `3600` | Reset-link lifetime (seconds) |
| `PASSWORD_RESET_THROTTLE_RATE` | `5/hour` | Forgot/reset rate limit |
| `EMAIL_*`, `DEFAULT_FROM_EMAIL` | console backend | Outgoing email |
| `PAYMENT_*`, `JAZZCASH_*`, `EASYPAISA_*` | empty | Online payments; see below |

## Online payments (JazzCash and Easypaisa)

### Flow

1. Checkout places the order (`payment_method` `jazzcash`/`easypaisa`). The
   order gets its usual `PF-XXXXXXXX` number and status `awaiting_payment`.
2. React calls `POST /api/payments/create/`. The server checks the user owns
   the order and that it is payable, takes the amount from `Order.total`
   (anything the client sends is ignored), and creates a `Payment` with its
   own `payment_id` (`PY` + 16 hex). The `payment_id` is the reference sent to
   the provider (`pp_TxnRefNo` / `orderRefNum`), never the order number.
3. The response's `checkout` (`{method, url, fields}`) is submitted by the
   browser to the provider's page.
4. The provider sends the browser back to our callback. **Nothing in that
   request marks a payment paid by itself:**
   * JazzCash: the `pp_SecureHash` (HMAC-SHA256 with the integrity salt) and
     merchant id must match, then the **Payment Inquiry API** gives the
     status; the signed amount and currency must match the payment.
   * Easypaisa: the redirect isn't signed, so the status and amount always
     come from the **inquire-transaction API**.
5. A verified PAID result sets the payment to `PAID` and the order to
   `confirmed` (once). The browser lands on
   `PAYMENT_RESULT_URL?payment_id=…`, and React polls
   `GET /api/payments/<payment_id>/` until the status is final.

Order status mapping: payment `PENDING`/`PROCESSING`/`FAILED`/`CANCELLED`/
`EXPIRED` → order stays `awaiting_payment` (the customer can pay again from
the order page); `PAID` → `confirmed`.

### Idempotency and safety

* Order and payment rows are locked (`select_for_update`, order first) for
  every change; provider HTTP calls happen outside transactions.
* At most one open (`PENDING`/`PROCESSING`) payment per order (partial
  unique constraint). Pressing Pay Now again returns the same payment and
  checkout. Switching provider or retrying after expiry first re-checks the
  old attempt with its provider; a `PROCESSING` attempt can't be replaced.
* `(payment_method, provider_transaction_id)` is unique, so one provider
  transaction can't pay two payments.
* Duplicate callbacks, IPN retries and browser refreshes are no-ops once
  paid; a later failure report never undoes `PAID`.
* A verified payment that arrives late still confirms the order. If the
  order was already paid or cancelled, the payment is kept as `PAID` with
  `metadata.needs_refund = true` and an `ERROR` log line. Amount/currency
  mismatches are `FAILED` with `metadata.needs_review = true`.
* Credentials, hashes and card data are never stored in `metadata` or
  logged.

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `PAYMENT_RESULT_URL` | `http://localhost:5173/payment/result` | Storefront page the customer returns to |
| `PAYMENT_EXPIRY_MINUTES` | `30` | How long an attempt stays open |
| `PAYMENT_PROVIDER_TIMEOUT` | `20` | Provider API timeout (seconds) |
| `JAZZCASH_ENVIRONMENT` | `sandbox` | `sandbox` or `production` |
| `JAZZCASH_MERCHANT_ID`, `JAZZCASH_PASSWORD`, `JAZZCASH_INTEGRITY_SALT` | empty | From the JazzCash merchant portal |
| `JAZZCASH_RETURN_URL` | empty | Public URL of `/api/payments/jazzcash/callback/` |
| `JAZZCASH_VERSION` | `1.1` | `pp_Version` your merchant account uses |
| `JAZZCASH_TXN_TYPE` | empty | Empty = customer chooses; or `MWALLET`, `MIGS`, `OTC` |
| `JAZZCASH_CHECKOUT_URL`, `JAZZCASH_INQUIRY_URL` | sandbox URLs | **Required in production** (JazzCash issues them) |
| `EASYPAISA_ENVIRONMENT` | `sandbox` | `sandbox` or `production` |
| `EASYPAISA_STORE_ID`, `EASYPAISA_HASH_KEY` | empty | Store id and hash key (16/24/32 chars) |
| `EASYPAISA_USERNAME`, `EASYPAISA_PASSWORD`, `EASYPAISA_ACCOUNT_NUM` | empty | Partner account for the REST API (`Credentials` header, `accountNum`) |
| `EASYPAISA_RETURN_URL` | empty | Public URL of `/api/payments/easypaisa/callback/` (`postBackURL`) |
| `EASYPAISA_PAYMENT_METHOD` | `MA_PAYMENT_METHOD` | Or `OTC_PAYMENT_METHOD`, `CC_PAYMENT_METHOD` |
| `EASYPAISA_CHECKOUT_URL`, `EASYPAISA_CONFIRM_URL`, `EASYPAISA_INQUIRY_URL` | built-in sandbox/production URLs | Override if Easypaisa gives you others |

A provider is only offered at checkout when all of its credentials are set.

### Local testing with sandbox credentials

1. Get sandbox credentials: JazzCash at <https://sandbox.jazzcash.com.pk>
   (Merchant ID, Password, Integrity Salt); Easypaisa from your Easypay
   merchant onboarding (Store ID, Hash Key, partner username/password,
   account number).
2. The providers must reach your callbacks, and `localhost` isn't reachable
   from them. Expose Django over HTTPS with a tunnel, e.g.
   `cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000`, and
   add the tunnel host to `ALLOWED_HOSTS` if you restrict it.
3. Put the credentials in `.env` with
   `JAZZCASH_RETURN_URL=https://<tunnel>/api/payments/jazzcash/callback/` and
   `EASYPAISA_RETURN_URL=https://<tunnel>/api/payments/easypaisa/callback/`.
   Keep `PAYMENT_RESULT_URL=http://localhost:5173/payment/result` (only your
   browser goes there).
4. `uv sync`, `python manage.py migrate`, `python manage.py runserver`.
5. In the storefront: `npm run dev`, then add to cart → checkout → choose
   JazzCash or Easypaisa → **Pay Now**.
6. In the provider dashboards:
   * JazzCash sandbox: register the return URL (JazzCash checks that
     `pp_ReturnURL` starts with the URL on your merchant profile) and set the
     IPN URL to `https://<tunnel>/api/payments/jazzcash/ipn/`.
   * Easypaisa: set the IPN URL to `https://<tunnel>/api/payments/easypaisa/ipn/`
     and make sure the postBack URL domain is allowed for your store.
7. Test cases: use the test wallet/card numbers shown in the JazzCash
   sandbox and the Easypaisa staging accounts from onboarding. The JazzCash
   guide (Appendix II) also maps test amounts to response codes (e.g. a
   successful card test uses PKR 100.00 = `10000`).

Automated tests mock every provider call:
`python manage.py test apps.payments`.

For production, set `*_ENVIRONMENT=production`, the production URLs JazzCash
issues, production credentials, and `https://` callback URLs on your own
domain.

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
