# PixelForge Backend

Backend for PixelForge: a **modular monolith**. One Django project serves
every module (authentication, catalog, search, cart, orders, payments) from
**one PostgreSQL primary**. PostgreSQL handles both the transactional data
and all search, filtering, sorting and faceting. An optional **read
replica** (asynchronous streaming replication) serves the read-heavy public
catalog, with read-your-writes protection (see [Read replica](#read-replica)).
**Redis** is an optional shared cache in front of hot catalog reads; the
primary stays the source of truth (see [Caching](#caching-redis)).

Several identical, stateless instances of the monolith run behind an
**nginx L7 load balancer** (see [L7 load balancer](#l7-load-balancer)).
Work that doesn't have to happen inside the request (emails, image
optimisation, audit records) is written to a **transactional outbox** in
the same PostgreSQL transaction as the business change, published to
**Kafka**, and done by **consumer workers** with retries, a dead-letter
queue and idempotency (see [Background tasks](#background-tasks-kafka)).

```
                         React frontend / browsers
                                   │  HTTP(S)
                                   ▼
                    nginx L7 load balancer :8000 (:8443 TLS)
              least_conn, passive health checks, retries, X-Forwarded-*
                  ┌────────────────┴────────────────┐
                  ▼                                 ▼
     Django instance backend-1          Django instance backend-2       (stateless, same code)
     modular monolith: auth · catalog · search · cart · orders · payments
                  │  one transaction: business rows + outbox row
                  ▼
   Redis (cache, ◄── PostgreSQL primary ── async WAL ──► replica(s)
   RYW markers)       (source of truth, outbox,          (catalog/search reads)
                       idempotency records)
                          │ ▲
         outbox-publisher │ │ ProcessedEvent / Notification / AuditEvent rows
                          ▼ │
                 Kafka (KRaft) topics ──► consumer groups (kafka_worker):
                 order-events               notifications ×2  (emails)
                 payment-events             images           (WebP variants)
                 inventory-events           audit            (audit trail)
                 notification-events        dlq-archiver     (DLQ archive)
                 image-processing
                 audit-events, retry-events, dead-letter-events
```

The modules still call each other in-process (no HTTP between modules) and
there is no Elasticsearch or Celery. Kafka only carries background work:
checkout, stock reservation, payment verification and payment state changes
stay synchronous and transactional.

## Quick start

**Docker (one command):**

```bash
docker compose up --build          # stop with: docker compose down  (volumes are kept)
```

The API is at `http://localhost:8000`, served by the nginx load balancer in
front of two Django instances (`backend-1`, `backend-2`; no host ports of
their own). A one-shot `migrate` service applies migrations before they start
and `kafka-init` creates the Kafka topics; then `outbox-publisher` and the
workers (`worker-notifications` ×2, `worker-images`, `worker-audit`,
`worker-dlq`) start. The PostgreSQL primary is published on `127.0.0.1:5433`
and the read replica on `127.0.0.1:5434`, Kafka's host listener on
`127.0.0.1:9094` (tests, tooling); Redis runs inside the Compose network only.
On first start the replica clones the primary (`pg_basebackup`) and then
streams its WAL. A `postgres_data` volume created before the replica was
added needs the replication role once (see [Read replica](#docker)).
Emails are printed by the notification workers (`docker compose logs -f
worker-notifications`) unless `EMAIL_BACKEND` is set. Seed demo data with:

```bash
docker compose exec backend-1 python manage.py seed_users
docker compose exec backend-1 python manage.py seed_inventory --count 50
docker compose exec backend-1 python manage.py seed_catalog
```

**Single backend, no load balancer or Kafka** (the previous setup; events are
processed in-process after each commit, see [Inline mode](#inline-mode-no-kafka)):

```bash
docker compose up --build backend  # http://localhost:8000; stop `lb` first if the full stack runs
```

**Local (without Docker)** uses [uv](https://docs.astral.sh/uv/) and requires PostgreSQL on `localhost:5432`
(no replica needed: without `DB_REPLICA_HOST`/`DATABASE_REPLICA_URL` every query uses that one database, as before;
no Kafka needed: without `KAFKA_BOOTSTRAP_SERVERS` background tasks run in-process after each commit).
Redis on `localhost:6379` is optional (`sudo apt install redis-server`, `brew install redis`, or
`docker run -d -p 127.0.0.1:6379:6379 redis:7-alpine`); without it every read goes to PostgreSQL.

```bash
uv sync                               # creates .venv from pyproject.toml + uv.lock
cp .env.example .env                  # set DATABASE_URL / SECRET_KEY
createdb pixelforge
uv run manage.py migrate
uv run manage.py seed_users           # roles + demo users (optional)
uv run manage.py runserver            # http://localhost:8000
```

**Tests:** `uv run manage.py test` (needs a PostgreSQL user that can create the test database and the `pg_trgm` extension).
The suite runs with the cache off and events in inline mode; the cache tests use Redis at `REDIS_TEST_URL` (default
`redis://127.0.0.1:6379/15`), the Kafka integration tests a real broker at `KAFKA_TEST_BOOTSTRAP_SERVERS` (default
`localhost:9094`, i.e. `docker compose up -d kafka`), and the load balancer tests the running stack at `LB_TEST_URL`;
each group is skipped when its service isn't reachable or configured. See [Testing the pipeline](#testing-the-pipeline).

**Dependencies** live in `pyproject.toml` and are pinned in `uv.lock`: `uv add <package>` to add one, `uv lock --upgrade` to refresh.

## Project layout

```
PixelForge/
├── manage.py
├── pyproject.toml, uv.lock   # dependencies (uv)
├── Dockerfile, docker-compose.yml, .env.example
├── config/                 # settings.py (primary + optional replica, proxy, Kafka), urls.py, wsgi.py, asgi.py
├── docker-compose.ha.yml   # HA cluster: Patroni nodes, etcd, HAProxy, reconciler
├── docker/postgres/        # primary replication setup, replica entrypoint (pg_basebackup)
├── docker/patroni/         # PostgreSQL 16 + Patroni image, patroni.yml, bootstrap
├── docker/haproxy/         # writer/reader endpoints
├── docker/ha-reconciler/   # replica-count reconciler
├── scripts/ha/             # failure scenarios, import of existing data
├── nginx/                  # L7 load balancer: templates/ (upstreams, proxying, TLS),
│                           # entrypoint.d/ (renders them from env), certs/ (TLS files, git-ignored)
├── scripts/
│   ├── lb/check_lb.sh      # load balancer failover check against the running stack
│   └── merge_legacy_databases.sh   # one-off copy from the old per-service databases
└── apps/
    ├── common/             # health checks, uniform APIResponse, test_db_connection,
    │                       # proxy.py (trusted X-Forwarded-*, request ids), logging.py (JSON logs)
    │   ├── cache/          # Redis cache service: manager (cache-aside, negative cache, TTL jitter,
    │   │                   # fenced refills), locks (rebuild mutex), keys, client (failure handling), metrics
    │   └── db/             # read replicas: router, middleware, consistency (LSN markers),
    │                       # replica (per-alias health/lag), context (reader pool choice),
    │                       # cluster (writer/replication/HA view), metrics
    ├── authentication/     # Role/UserProfile, register/login/logout/password reset,
    │                       # Bearer token auth (tokens.py, authentication.py), RBAC permissions
    ├── catalog/            # catalog models, /api/catalog/ viewsets, flat /api/products|
    │   │                   # categories|brands|banners/ endpoints, inventory, search stats
    │   ├── selectors.py    # public read API used by other modules (e.g. cart)
    │   ├── product_store.py / catalog_store.py   # flat endpoint documents <-> models
    │   ├── inventory.py    # transaction-safe stock changes, logs, low-stock alerts
    │   ├── cache.py        # cached product/category/brand reads + invalidation signals
    │   └── stats.py        # denormalized price/stock/rating columns (kept in sync)
    ├── search/             # PostgreSQL search
    │   ├── filters.py      # keyword/filter/order builders (Q expressions)
    │   ├── selectors.py    # base querysets
    │   ├── services.py     # ProductSearch, autocomplete, category/brand search, flat search
    │   ├── documents.py    # result documents (prefetched, no N+1)
    │   └── views.py, urls.py
    ├── cart/               # user and guest carts; validates products via catalog.selectors
    ├── orders/             # checkout: orders from the user's cart, stock via catalog.inventory
    ├── payments/           # JazzCash/Easypaisa payments for orders
    │   ├── services/       # __init__.py: create/verify/apply (locking, idempotency, status events)
    │   │                   # jazzcash.py, easypaisa.py: provider protocol only
    │   └── webhooks/       # provider return URLs and IPN listeners
    ├── events/             # outbox -> Kafka -> workers (see "Background tasks")
    │   ├── outbox.py       # enqueue() in the business transaction; audit() best effort
    │   ├── publisher.py    # outbox_publisher: claim, produce, mark published on ack
    │   ├── worker.py       # kafka_worker <group>: commit after durable work, retry/DLQ
    │   ├── processing.py   # one task run: dedup, time limit, classification
    │   ├── retry.py        # retry policy, error classification, sanitized errors
    │   ├── registry.py     # consumer groups and @task registrations
    │   ├── topics.py, envelope.py, kafka.py, inline.py, dlq.py, health.py, heartbeat.py
    │   └── management/commands/   # kafka_create_topics, outbox_publisher, kafka_worker,
    │                              # kafka_dlq, outbox, events_healthcheck
    └── notifications/      # idempotent email delivery (Notification) and its tasks
```

Each app's `tasks.py` registers its background tasks (`catalog/tasks.py`:
image processing, `notifications/tasks.py`: emails, `events/tasks.py`:
audit and DLQ archive).

Dependency direction: `search → catalog`, `cart → catalog.selectors`, and
everything → `authentication`/`common` for auth and responses. The catalog
doesn't import cart. Its flat `GET /api/products/` view calls
`search.services`, since that endpoint is the storefront search.

## API

All paths are unchanged from the microservice version. Only the host
changed: everything is now served on `:8000` (the load balancer, or a
single instance).

| Prefix | Module | Notes |
|--------|--------|-------|
| `/api/auth/` | authentication | register, login, logout, password reset |
| `/api/catalog/` | catalog | DRF CRUD for categories, subcategories, brands, products, variants, images, attributes, specs, prices, inventory, reviews |
| `/api/search/` | search | `products/`, `autocomplete/`, `categories/`, `brands/` |
| `/api/products/`, `/api/categories/`, `/api/brands/`, `/api/banners/` | catalog (+search) | flat storefront/admin endpoints used by the React app |
| `/api/cart/` | cart | user cart (`Bearer`), guest cart (`X-Guest-Session`), merge |
| `/api/orders/` | orders | checkout options, place order from the cart, order detail |
| `/api/health/` | common | Django + PostgreSQL (primary, replica) + Redis + event pipeline summary; reports the answering `instance` |
| `/api/health/database/` | common | Writer, reader pool and cluster: roles, replicas, lag, failover state, routing counters |
| `/api/health/events/` | events | Outbox backlog, Kafka brokers/topics, consumer groups and lag, worker heartbeats, task latency, dead letters |
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

## Caching (Redis)

Redis is a **shared cache** for read-heavy, public catalog data. Every
Gunicorn worker uses the same Redis, so an entry built by one worker serves
all of them. **PostgreSQL remains the source of truth:** Redis only holds
copies, can be emptied at any time, and nothing ever writes data to Redis
that isn't in PostgreSQL first.

```
Browser → Nginx → Django ──► Redis           HIT  → response
                       │
                       └──► PostgreSQL       MISS → store in Redis → response
```

Application code uses `apps.common.cache`, never Redis directly. It runs on
django-redis's connection for `CACHES["default"]`, so there is one Redis
configuration.

### What is cached

| Data | Endpoint | Key | TTL |
|------|----------|-----|-----|
| Product document | `GET /api/products/<id>/` | `pixelforge:product:<id>` | 300 s + 0–30 s jitter |
| Unknown product id | `GET /api/products/<id>/` → 404 | `pixelforge:product:<id>` (not-found marker) | 60 s + 0–15 s jitter |
| Active categories | `GET /api/categories/` | `pixelforge:catalog:categories` | 600 s + 0–60 s jitter |
| Active brands | `GET /api/brands/` | `pixelforge:catalog:brands` | 600 s + 0–60 s jitter |

Rebuild locks are `pixelforge:lock:<key>`, e.g. `pixelforge:lock:product:42`.

**Never cached:** cart, orders, checkout validation, inventory locking,
payments and their callbacks, auth tokens, roles and permissions, and
personalized endpoints (recommended, recently viewed). Checkout reads
products and stock from PostgreSQL inside its transaction
(`catalog.selectors`, `catalog.inventory`), so a stale cached `stock_quantity`
can only affect what a page displays, never what gets sold.

### Cache-aside with a rebuild mutex (stampede protection)

When a hot key expires, hundreds of requests can miss at the same moment
and all query PostgreSQL (a *cache stampede*). Only the request holding the
key's mutex rebuilds it:

```
                    Request
                       │
                       ▼
                     Redis
                       │
              ┌────────┴────────┐
              │                 │
             HIT               MISS
              │                 │
              ▼                 ▼
            Return       SET lock NX PX
                                │
                         ┌──────┴──────┐
                         │             │
                      acquired      held by another request
                         │             │
                         ▼             ▼
                 check Redis again   wait 50 → 100 → 200 ms
                  (hit? return)        │  then read Redis
                         │             │  (hit? return; else retry the lock)
                         ▼             │
                   PostgreSQL          │  after CACHE_LOCK_WAIT_TIMEOUT:
                         │             │  load from PostgreSQL itself
                         ▼             │
                   store in Redis ◄────┘
                         │
                 release lock (only if
                 it still holds our token)
                         │
                         ▼
                       Return
```

* The lock is `SET pixelforge:lock:<key> <random token> NX PX <CACHE_LOCK_TTL>`:
  atomic, one holder, and it always expires, so a crashed worker can't
  deadlock a key.
* Release runs a Lua script that deletes the lock only if it still holds the
  caller's token. A request whose lock already expired can't delete the
  next holder's lock. Release happens in `finally`, also when the loader fails.
* The holder **re-checks Redis after acquiring the lock**: a previous holder
  may have just stored the value.
* Waiters back off (50 → 100 → 200 ms, ±20% jitter, so they don't poll in
  lockstep) and never wait longer than `CACHE_LOCK_WAIT_TIMEOUT`.

Measured on the dev server: 50 concurrent requests for a cold product ran
**one** rebuild; the other 49 were served from Redis.

### Negative caching (penetration protection)

Requests for ids that don't exist (`/api/products/999999/`, bots, scrapers)
would always miss and always reach PostgreSQL. A genuine "does not exist"
result is stored as an explicit marker with a short TTL
(`NEGATIVE_CACHE_TTL`), and the 404 is answered from Redis until it expires.

Values are stored as JSON envelopes, so a miss, a cached `null` and a cached
"not found" can't be confused:

```
pixelforge:product:42      {"v": {"id": 42, "name": "...", ...}}
pixelforge:product:999999  {"nf": "Product 999999 not found"}
```

Only the "does not exist" exception is cached. Database errors and timeouts
propagate and are never cached. Creating the product deletes its marker.

### TTL jitter (avalanche protection)

Entries written together (after a deploy, a Redis restart, or a busy minute)
would expire together and send all that traffic to PostgreSQL at once. Every
TTL is `base + random(0, jitter)` seconds (always ≥ 1), so expiries spread out.
Each kind of data has its own base TTL and jitter (`CachePolicy`), see the
table above.

### Invalidation

Signal handlers in `apps/catalog/cache.py` delete affected keys **after the
transaction commits** (`transaction.on_commit`). A rolled-back change
invalidates nothing, and no reader can re-cache data that is about to be
rolled back.

| Change | Deleted |
|--------|---------|
| Product saved / soft-deleted | `product:<id>`, `catalog:categories`, `catalog:brands` (product counts) |
| Variant, price, inventory (incl. every order), image, review | `product:<id>` |
| Category or brand saved | its list key + `product:<id>` of each of its products |
| `refresh_product_stats` full rebuild (`queryset.update()` sends no signals) | every `product:*` key (SCAN, maintenance only) |

TTLs only bound staleness when an invalidation is lost, e.g. Redis was
unreachable at that moment.

### When Redis is unavailable

```
Redis error ─► log one WARNING ─► skip Redis in this worker for CACHE_FAILURE_COOLDOWN ─► PostgreSQL ─► response
```

Requests never fail because of Redis. Connect and socket timeouts are
`REDIS_SOCKET_TIMEOUT` (0.25 s), and after an error each worker skips Redis
for 30 s, so an outage costs one timeout and one log line per worker per
cooldown instead of one per request. Once Redis answers again, caching
resumes and an INFO line is logged. DRF throttling (password reset) also
uses this cache; django-redis's `IGNORE_EXCEPTIONS` lets it carry on
without Redis.

`/api/health/` reports Redis separately: PostgreSQL down is `unhealthy`
(503), Redis down is only `degraded` (200). `CACHE_ENABLED=True` with a
`CACHES` backend other than django-redis fails `manage.py check` (`common.E001`).

### Observability

Logger `apps.cache` (set `CACHE_LOG_LEVEL=DEBUG` to see every hit, miss and
set). Per-worker counters are in `/api/health/` under
`checks.redis.cache_metrics`: `cache_hits_total`, `cache_misses_total`,
`cache_negative_hits_total`, `cache_sets_total`, `cache_deletes_total`,
`cache_lock_acquired_total`, `cache_lock_contention_total`,
`cache_lock_timeout_total`, `cache_rebuild_total`, `cache_errors_total`.
Logs never include Redis URLs, credentials or user data.

### Cache configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `REDIS_URL` | `redis://127.0.0.1:6379/1` (Compose: `redis://redis:6379/1`) | Redis connection (cache and read-your-writes markers); may contain a password, never logged |
| `REDIS_SOCKET_TIMEOUT` | `0.25` | Connect and command timeout (seconds) |
| `CACHE_ENABLED` | `True` | `False` sends every read to PostgreSQL |
| `CACHE_KEY_PREFIX` | `pixelforge` | Namespace of every key |
| `CACHE_DEFAULT_TTL` / `CACHE_TTL_JITTER` | `300` / `30` | Default TTL + max random extra (seconds) |
| `NEGATIVE_CACHE_TTL` / `NEGATIVE_CACHE_TTL_JITTER` | `60` / `15` | "Not found" marker TTL + jitter |
| `CACHE_LOCK_TTL` | `5.0` | Rebuild-lock lifetime; longer than the slowest rebuild |
| `CACHE_LOCK_WAIT_TIMEOUT` | `2.0` | Max wait for another request's rebuild |
| `CACHE_LOCK_RETRY_DELAY` / `CACHE_LOCK_RETRY_MAX_DELAY` | `0.05` / `0.2` | Waiter backoff: first delay, doubling up to the max |
| `CACHE_FAILURE_COOLDOWN` | `30` | Seconds a worker skips Redis after an error |
| `CACHE_LOG_LEVEL` | `INFO` | `DEBUG` logs every hit/miss/set |

### Caching another read

Wrap the existing store/service function; don't cache in views or cache
model instances:

```python
from apps.common import cache

def get_category(category_id):
    return cache.get_or_set(
        cache.build_key("category", category_id),
        lambda: catalog_store.get_document(catalog_store.CATEGORY, category_id),
        policy=cache.CachePolicy(ttl=600, jitter=60),
        not_found=catalog_store.CatalogDoesNotExist,
    )
```

Then add invalidation for every write that changes the result, and a test
that the second request runs no queries. Per-user data must pass
`user_id=` to `build_key`. Free-text parts (search queries) are hashed
automatically.

### Testing

```bash
uv run manage.py test                                  # everything (cache tests need Redis)
uv run manage.py test apps.common.tests_cache          # cache service: hit/miss, TTL, jitter,
                                                       # negative cache, locks, 100-thread stampede,
                                                       # Redis down
uv run manage.py test apps.catalog.tests.test_cache    # endpoints, invalidation, checkout vs cache
REDIS_TEST_URL=redis://127.0.0.1:6379/15 uv run manage.py test apps.common.tests_cache
```

Cache tests only touch keys under `pixelforge-test:`.

## Read replica

```
                         Internet
                            │
                            ▼
                         Django
                            │
             ┌──────────────┴──────────────┐
             │                             │
           WRITE                          READ
   (POST/PUT/PATCH/DELETE,         (catalog/search GETs;
    any transaction, cart,          everything else reads
    orders, payments, auth)         the primary)
             │                             │
             ▼                             ▼
       ┌───────────┐                 ┌─────────┐
       │  Primary  │                 │  Redis  │── HIT ──► response
       │ PostgreSQL│                 │  Cache  │
       └─────┬─────┘                 └────┬────┘
             │                            │ MISS / uncached read
             │                ┌───────────┴───────────┐
             │                │                       │
             │      caller's last write (LSN)   nothing pending, or
             │      not yet replayed, replica   replica has replayed it
             │      lagging/down, or Redis down        │
             │                │                       │
             │                ▼                       ▼
             │             Primary                 Replica
             │
             │ asynchronous streaming replication (WAL)
             ▼
       ┌───────────┐
       │  Replica  │  hot standby, read-only
       │ PostgreSQL│
       └───────────┘
```

**Why the primary takes every write.** There is one source of truth. The
replica is a hot standby that replays the primary's write-ahead log (WAL);
it is read-only, so a write sent there would fail (and here never gets
there: see below).

**Why normal reads go to the replica.** Search, listings, facets and product
pages are most of the traffic and only read. Serving them from a replica
takes that load (CPU, I/O, connections) off the primary, which is left with
writes, checkout and payments.

**Replication lag.** Replication is asynchronous: the primary commits and
answers without waiting for the replica, which replays the change a little
later (normally milliseconds; seconds or more under load or after a
network hiccup). Until then the replica returns the old rows.

**Read-after-write consistency** means a caller always sees its own
committed writes, even though the replica may not have them yet. Other
callers may see the change a moment later (eventual consistency).

**Why Redis doesn't replace the replica.** Redis only holds a few hot,
identical-for-everyone documents (product pages, category and brand
lists). Search with filters and facets, admin listings and per-user data
are too varied to cache and still need a database; the replica serves them.

### Reader pool: several replicas

Readers are configured in one of three ways (first match wins):

| Setting | Aliases | Who spreads the reads |
|---------|---------|-----------------------|
| `READ_REPLICA_COUNT=N` + `DB_REPLICA_<i>_HOST/PORT/NAME/USER/PASSWORD` (or `DATABASE_REPLICA_<i>_URL`), i = 1..N | `replica_1` .. `replica_N` | Django (below) |
| `DB_READER_ENDPOINT=host:port` | `replica` | the load balancer behind the endpoint (HAProxy, Aurora reader endpoint, ...) |
| `DB_REPLICA_HOST` / `DATABASE_REPLICA_URL` (as before) | `replica` | - (one replica) |

With a pool, Django is the reader endpoint. Each alias has its own
connection, health status, lag, replay position, failure cooldown and
read-only protection (`apps/common/db/replica.py`). For each request:

1. **Usable replicas only:** reachable, a hot standby, at most
   `REPLICA_MAX_LAG` behind, not in failure cooldown. An unusable replica
   leaves the pool (`event=replica_removed`) and comes back on the first
   healthy check (`event=replica_rejoined`).
2. **Lag-weighted random order:** weight `1 / (1 + lag_seconds)`, so load
   spreads over the pool and a slower replica gets less of it.
3. **Read-your-writes per replica:** a caller with a pending write gets the
   first replica in that order that has replayed it; only if none has does it
   read the primary.
4. **Sticky per request:** a request keeps the replica it started on.

The replay position cache is per physical connection, so a new connection
through a load-balanced endpoint (which may reach another server) never
reuses an old position.

### What may read the replica

| Reads | Where |
|-------|-------|
| `catalog` models (products, variants, prices, stock, images, specs, reviews, categories, brands, banners, recently viewed), i.e. the catalog and search endpoints, in GET/HEAD/OPTIONS requests | Replica, unless one of the rules below applies |
| Auth, sessions, admin, cart, orders, payments | **Always the primary** (`READ_REPLICA_APPS`) |
| Anything in a POST/PUT/PATCH/DELETE request | Primary (validation reads what the write is about to change) |
| Anything inside `transaction.atomic()`, including `select_for_update()` | Primary (sees the transaction's own writes, locks rows) |
| Management commands, shell | Primary |

Cart, orders and payments stay on the primary on purpose: `GET /api/cart/`
creates carts, `GET /api/payments/<id>/` updates the payment before reading
it, payment callbacks come from the provider (they can't be tied to the
shopper who reads the status next), and checkout re-validates prices and
stock inside its transaction.

### Read-your-writes with WAL positions (LSN)

```
WRITE request                                   READ request (same caller)
  │                                               │
  ├─ statements on the primary (observed)         ├─ Redis: rw:<caller> → required LSN
  ├─ COMMIT  (rolled back? nothing is recorded)   ├─ replica: pg_last_wal_replay_lsn()
  ├─ cache invalidation (on_commit, as before)    │      (cached per worker; re-checked only
  ├─ SELECT pg_current_wal_lsn()  → LSN           │       when it looks behind)
  └─ Redis: SET rw:<caller> = max(LSN) EX TTL     ├─ replayed ≥ required → replica
                                                  └─ otherwise           → primary
```

* **Who the caller is** (from headers only, no database query):
  `user:<id>` from a valid Bearer token; else `guest:<sha256>` of
  `X-Guest-Session`; else `session:<sha256>` of the Django session cookie
  (admin). A session-authenticated writer is marked under its user id too.
  Anonymous callers without any of these don't write anything that is read
  back, so they have no marker and read the replica.
* **Only after commit.** `ReadReplicaMiddleware` watches statements on the
  primary connection: a write inside `atomic()` counts through `on_commit`
  (discarded on rollback, including a rolled-back savepoint), an
  autocommit write counts at once. The LSN is read after the response,
  i.e. after every commit of the request.
* **TTL is not proof.** `RECENT_WRITE_TTL` (30 s) is only how long the
  marker is remembered. Whether the replica has the write is decided by
  comparing LSNs. Once the marker expires, a replica that is still behind
  can only be serving because it is within `REPLICA_MAX_LAG`; the TTL must
  exceed `REPLICA_MAX_LAG + REPLICA_HEALTH_INTERVAL` (system check
  `common.W002`).

### Cache and replica together

The existing cache-aside flow is unchanged (invalidation after commit,
rebuild mutex, negative cache, jitter). With a replica, refills have one
more hazard: right after an invalidation, a refill could read the replica
before it replayed the write and store the old document for minutes. Two
measures prevent that:

1. **Gated refills.** Every committed catalog change raises a shared
   marker, `pixelforge:rw:catalog`, to the primary's LSN *before* the cache
   keys are deleted. Refills (`catalog.cache`) read the replica only if it
   has replayed that LSN, otherwise the primary.
2. **Fenced store.** The marker also gets a new random suffix on every
   change. A refill remembers its value before loading and stores the
   result only if it is unchanged (atomic compare-and-set in Lua,
   `cache.get_or_set(..., fence=...)`). A refill that raced a write is
   returned to its caller but never cached
   (`cache_fenced_skips_total`).

A cache hit never touches either database. Writes never read or write the
cache except to invalidate it.

### Failures

| What fails | Behaviour |
|------------|-----------|
| Replica unreachable | Each worker checks it at most every `REPLICA_HEALTH_INTERVAL` seconds; after an error it reads only the primary for `REPLICA_FAILURE_COOLDOWN` seconds (one WARNING per worker). A GET that fails *during* a replica query is re-run once on the primary (`db_replica_retries_total`), so the client doesn't see the error. |
| Replica lagging more than `REPLICA_MAX_LAG` | All reads go to the primary until it catches up (WARNING, `db_replica_lagging_total`). |
| Replica alias pointing at a server that isn't a standby | Not used (ERROR log, status `not_standby`). |
| Redis unavailable | Markers can't be read or written: every caller with an identity, and every cache refill, reads the primary. Anonymous search still uses the replica. Nothing is served that might miss a caller's write. |
| No replica configured | Every query uses the primary, exactly as before. |

Writes can't reach the replica: the router sends every write and
migration to the primary (`allow_migrate` is primary-only), the replica
connection refuses write statements (`ReplicaWriteError`), and it is opened
with `default_transaction_read_only=on` (a hot standby is read-only
anyway).

### Monitoring

`GET /api/health/database/`:

```json
{"status": "healthy",
 "primary": {"status": "ok"},
 "replica": {"status": "ok", "replay_lsn": "0/31CC398", "primary_lsn": "0/31CC398",
             "lag_bytes": 0, "lag_seconds": 0.0, "max_lag_seconds": 10.0},
 "reads": "replica",
 "routing_metrics": {"db_replica_reads_total": 24, "db_primary_reads_total": 12, ...}}
```

`replica.status` is `ok`, `lagging`, `unavailable`, `not_standby`,
`not_configured` or `disabled`; anything but ok/not_configured/disabled
makes the overall status `degraded` (still 200). Only the primary being
down is `unhealthy` (503). `/api/health/` reports the same replica status
under `checks.database.replica`. Lag comes from `pg_current_wal_lsn()` on
the primary and `pg_last_wal_replay_lsn()` /
`pg_last_xact_replay_timestamp()` on the replica: no extra privileges are
needed. Logger: `apps.db`. Counters are per worker.

### Docker

`docker compose up` starts `postgres` (primary; `wal_level=replica`,
`wal_keep_size=256MB`) and `postgres-replica` (hot standby, cloned with
`pg_basebackup --write-recovery-conf` on first start). A primary volume
created **before** the replica existed doesn't have the replication role
yet; add it once, then (re)start the replica:

```bash
docker compose up -d postgres
docker compose exec postgres sh /docker-entrypoint-initdb.d/10-replication.sh
docker compose up -d
```

To simulate lag locally, pause and resume WAL replay on the replica:

```bash
docker compose exec postgres-replica psql -U pixelforge -c "SELECT pg_wal_replay_pause()"
docker compose exec postgres-replica psql -U pixelforge -c "SELECT pg_wal_replay_resume()"
```

### Testing

```bash
uv run manage.py test apps.common.tests_db_routing   # routing, read-your-writes, cache + replica,
                                                     # failures, rollback, concurrency (Redis tests skip
                                                     # without REDIS_TEST_URL)
```

The test runner adds a `replica` alias that mirrors the test database and
keeps routing off for every other test. Routing tests turn it on, run real
queries through both connections and simulate the replication state
(replay LSN, lag, outage) with `apps.common.testing.FakeReplica`.

### Limitations

* **Read-your-writes is per caller.** Another user (or another device of a
  guest) may see a change up to the replication lag later. That is the
  contract of asynchronous replication.
* **Lag in seconds** is `now() - pg_last_xact_replay_timestamp()` whenever
  the replica is behind by any bytes. Right after a long idle period it
  overstates lag (reads use the primary for one check interval), and it
  relies on synchronized clocks (NTP).
* **Health and replay positions are cached per worker**
  (`REPLICA_HEALTH_INTERVAL`). A replica that dies between checks costs the
  first failing GET a retry on the primary, and a non-GET can't be retried
  (but non-GETs never read the replica).
* **Markers live in Redis** under `allkeys-lru`. An evicted marker before its
  TTL behaves like an expired one (bounded by `REPLICA_MAX_LAG`). Give
  Redis enough memory, or move markers to a non-evicting instance.
* **Counters are per worker** and reset on restart.
* **Reader pool health is per worker.** With a load-balanced reader
  endpoint, Django's health state describes the server its own connection
  reached; the load balancer's checks gate the rest (see
  [High availability](#high-availability)).

### Production recommendations

* Run the replica on separate hardware/zone; monitor `pg_stat_replication`
  on the primary and alert on `replay_lag` and on `/api/health/database/`
  being `degraded`.
* Connect the app to the replica with a **read-only database role**.
* Put **PgBouncer** in front of both (transaction pooling), and keep
  `CONN_MAX_AGE` compatible with it.
* Use a **replication slot** (or a WAL archive) so the primary keeps WAL
  while the replica is down; watch slot disk usage.
* Keep `RECENT_WRITE_TTL > REPLICA_MAX_LAG + REPLICA_HEALTH_INTERVAL`.
* Let the infrastructure handle failover and point Django at stable
  endpoints (`DB_WRITER_ENDPOINT`, `DB_READER_ENDPOINT`): see
  [High availability](#high-availability).

## High availability

The single-primary setup above keeps working as is. For automatic failover,
`docker-compose.ha.yml` runs a self-managed HA cluster locally. Django's part
doesn't change: it only knows two **logical endpoints** and never decides,
or even knows, which PostgreSQL node is primary.

```
                              Django (any number of workers)
                                 │                      │
                  DB_WRITER_ENDPOINT              DB_READER_ENDPOINT
                    haproxy:5000                     haproxy:5001
                         │                               │
                  ┌──────┴───────────────────────────────┴──────┐
                  │ HAProxy: asks every node's Patroni REST API  │
                  │  /primary → writer pool (exactly one node)    │
                  │  /replica?lag=1MB → reader pool (round robin) │
                  └──────┬──────────────┬───────────┬────────────┘
                         ▼              ▼           ▼            ▼
                       pg2            pg1         pg3          pg4      (any node can be primary)
                     PRIMARY ──WAL──► replica   replica      replica
                         ▲              ▲           ▲            ▲
                         └──── Patroni agents: leader key in etcd ───┘
                                          │
                                 etcd1  etcd2  etcd3   (quorum 2 of 3)

   reconciler: watches the cluster, keeps 1 primary + DESIRED_REPLICAS healthy replicas
```

### Components

| Component | Role |
|-----------|------|
| **Patroni** (one agent per PostgreSQL node) | Runs PostgreSQL, races for the **leader key** in etcd, promotes/demotes its own node, rewinds or re-clones a returning node. |
| **etcd** (3 members) | Consensus store. A write to the leader key needs a quorum (2 of 3), so only one side of any partition can hold it. |
| **HAProxy** | The stable **writer endpoint** (`:5000`: the single node whose Patroni API answers `/primary` with 200) and **reader endpoint** (`:5001`: running replicas at most 1 MB behind). Nodes are discovered by Docker DNS (network alias `pgnode`), so new nodes join without config changes. Servers start *fully down* and need two passing checks, so a restarted HAProxy never routes before it knows who is who. When a node stops being primary, its client sessions are cut (`on-marked-down shutdown-sessions`). |
| **Reconciler** (`docker/ha-reconciler/reconcile.sh`) | Desired state: provisions a replacement node when fewer than `DESIRED_REPLICAS` replica nodes run, reinitialises a replica stuck unhealthy, retires surplus nodes it created. Logs every transition as JSON. Never promotes anything. |
| **Django** | Writes to the writer endpoint, reads through the reader endpoint, keeps its LSN-based read-your-writes and cache fencing, reports what it sees in `/api/health/database/`. |

### Writer endpoint

`DB_WRITER_ENDPOINT=haproxy:5000` replaces the host/port of the primary
(credentials still come from `DB_PRIMARY_*`/`DATABASE_URL`). HAProxy routes
it to whichever node holds the leader key. After a failover Django's
existing connections are cut, `CONN_HEALTH_CHECKS` reconnects, and the same
endpoint now reaches the new primary. Django's configuration never changes.

### Reader endpoint

`DB_READER_ENDPOINT=haproxy:5001` is one reader alias; HAProxy spreads new
connections round robin over healthy replicas and drops a replica that is
down, lagging (Patroni: more than 1 MB of WAL) or promoted. On top of that,
Django still checks the server behind its own connection (lag in seconds,
standby status, replay LSN) and closes a connection that reached an
unhealthy server, so the next one goes elsewhere. Without a load balancer,
use the [Django-side reader pool](#reader-pool-several-replicas) instead.

### Automatic failover and promotion

1. **Detection.** The primary's Patroni renews the leader key every
   `loop_wait` (5 s). If the primary dies, the key expires after `ttl`
   (20 s). A slow loop or a brief network blip doesn't trigger anything: only
   an expired key does, so there are no false-positive promotions from a
   single missed check.
2. **Eligibility.** Each replica's Patroni checks itself: running, not tagged
   `nofailover`, and not more than `maximum_lag_on_failover` (1 MB) behind
   the last known primary position.
3. **Selection by WAL position.** Before taking the key, a candidate asks
   every other member for its WAL position (received/replayed LSN) and backs
   off if anyone is ahead. The most up-to-date eligible replica wins: with
   R1 = 1000, R2 = 1200, R3 = 1190, R2 is promoted.
4. **Promotion.** The winner takes the leader key atomically in etcd (one
   winner), runs `pg_promote()`, and starts a new timeline.
5. **Writer endpoint follows.** HAProxy's next checks (every 2 s) mark the
   new node UP on `:5000`; the remaining replicas are re-pointed by their
   Patroni agents to stream from the new primary.

Measured locally (`scripts/ha/scenarios.sh`): SIGKILL of the primary → new
primary elected after 18 s (dominated by `ttl`), writer endpoint switched
3 s later, replacement replica provisioned within seconds. A clean stop
(`docker stop`) releases the key immediately and fails over in a few
seconds. In a partition, the cut-off primary stopped accepting writes after
4–8 s and the majority elected a new primary after 19–21 s: the old one is
gone well before the new one exists.

### Split-brain protection

* **One leader key, written with quorum.** etcd accepts a key write only
  with a majority (2 of 3). Two nodes can't both hold the key.
* **The primary demotes itself without the key.** If the primary can't renew
  the key within `retry_timeout` (it lost etcd, or it is partitioned), its
  Patroni restarts PostgreSQL read-only *before* the key can expire and be
  taken by another node (`loop_wait + retry_timeout < ttl`).
* **The writer endpoint only trusts Patroni.** HAProxy routes writes to a
  node only while its Patroni says it is the primary, and cuts sessions as
  soon as it isn't.
* **Old primaries never return as primaries.** A node that comes back sees
  another leader in etcd and starts as a replica.
* **Fencing:** in production, enable Patroni's **watchdog** (`/dev/watchdog`
  or `softdog`): if the Patroni process itself hangs, the kernel resets the
  node before the key expires. Containers can't use a watchdog
  (`watchdog: mode: off` locally): this is the one guarantee the local setup
  doesn't have.

### Replacement replicas

The reconciler counts running node containers. When a node is gone (e.g.
the old primary after a failover: 1 primary + 2 replicas), it starts a new
one (`pg-r<timestamp>`) from the same image, network and settings; Patroni
clones it from the current primary with `pg_basebackup`, and HAProxy finds it
through DNS. It waits while there is no primary (never adds nodes mid-
failover). When the old node returns and the cluster has more healthy
replicas than desired, it retires one of the nodes it created (container and
its volume). In production this is the orchestrator's or the managed
service's job (Kubernetes operator such as CloudNativePG/Zalando, an
autoscaling group, RDS/Aurora), never Django's.

### Old primary recovery (rejoin)

```
old primary restarts ─► Patroni: leader key held by pg3, timeline 2
        │
        ├─ pg_rewind onto the new timeline (needs data checksums / wal_log_hints: both on)
        │     └─ diverged WAL that never reached the new primary is discarded
        ├─ rewind impossible? remove_data_directory_on_rewind_failure /
        │  remove_data_directory_on_diverged_timelines → re-clone from the primary
        └─ start as a read-only standby following the new primary
```

It is read-only throughout (`pg_is_in_recovery() = true`) and joins the
reader pool once it streams and has caught up.

### Read-your-writes and the cache across a failover

Nothing changes in the mechanism: after a commit Django records
`pg_current_wal_lsn()` **of whatever the writer endpoint reaches**, and a
read uses a replica only if `pg_last_wal_replay_lsn()` on that replica is at
least that LSN. Timelines continue the same LSN sequence, so positions from
before and after a promotion compare correctly. Cases:

* The write reached the new primary (it replicated before the failure):
  replicas following the new primary pass the check once they replay it.
* The write was lost (asynchronous replication: committed on the old primary,
  never received by the promoted one): the caller's marker can stay ahead of
  every replica for up to `RECENT_WRITE_TTL`, so that caller reads the
  primary. No stale replica is used, but the lost write is lost; that is
  the price of asynchronous replication (see synchronous mode below).
* The cache fence (`pixelforge:rw:catalog`) keeps working the same way:
  refills read only from a replica that has replayed the last catalog
  commit, and a refill that raced a commit is not stored.

### Local Docker

```bash
docker compose -f docker-compose.ha.yml up -d --build    # ~1 min to elect and clone
open http://127.0.0.1:8404/                              # HAProxy stats
curl -s 127.0.0.1:8008/cluster | jq                      # Patroni's view
curl -s 127.0.0.1:8001/api/health/database/ | jq .data   # Django's view
docker compose -f docker-compose.ha.yml logs -f reconciler
scripts/ha/scenarios.sh                                  # every failure scenario, asserted
```

| Port (localhost) | What |
|------------------|------|
| 8001 | Django (`HA_BACKEND_PORT`) |
| 5000 / 5001 | writer / reader endpoint (`HA_WRITER_PORT`, `HA_READER_PORT`) |
| 8008 | Patroni REST API of any live node (`HA_PATRONI_API_PORT`) |
| 8404 | HAProxy stats (`HA_STATS_PORT`) |

Useful commands:

```bash
docker compose -f docker-compose.ha.yml exec pg1 patronictl -c /etc/patroni/patroni.yml list
docker compose -f docker-compose.ha.yml exec pg1 patronictl -c /etc/patroni/patroni.yml switchover   # planned
docker kill pixelforge-ha-pg2-1                          # crash a node (use the current leader)
```

`scripts/ha/scenarios.sh` runs: normal operation, replica failure and
recovery, read-after-write and replica lag (WAL receiving frozen with
SIGSTOP: `pg_wal_replay_pause()` doesn't work here, Patroni resumes paused
replay), Redis down, primary crash with one replica deliberately behind,
old-primary rejoin and a network partition. It asserts every outcome and
leaves the cluster at 1 + 3.

**Existing data.** The HA stack has its own project name and volumes;
`docker-compose.yml` and `pixelforge_postgres_data` are untouched. To copy
the existing database in (read-only on the source, refuses a non-empty
target):

```bash
docker compose up -d postgres
docker compose -f docker-compose.ha.yml up -d --build etcd1 etcd2 etcd3 pg1 pg2 pg3 pg4 haproxy reconciler
SOURCE_URL=postgres://pixelforge:pixelforge@127.0.0.1:5433/pixelforge scripts/ha/import_existing_data.sh
docker compose -f docker-compose.ha.yml up -d
```

**Cleanup** (deletes the HA cluster's data only):

```bash
docker compose -f docker-compose.ha.yml down -v --remove-orphans
docker volume rm $(docker volume ls -q --filter label=com.pixelforge.ha.provisioned-by=reconciler)
```

### Local development vs production

| | Local (`docker-compose.ha.yml`) | Production |
|-|---|---|
| Failover | Patroni + etcd + HAProxy on one Docker host: shows the mechanism, not real fault isolation (one machine, one Docker daemon) | **Managed PostgreSQL** (Aurora/RDS Multi-AZ, Cloud SQL HA, Azure Flexible Server): writer and reader endpoints included; or **Patroni + etcd/Consul + HAProxy/PgBouncer** across 3 zones |
| Fencing | none (no watchdog in containers) | Patroni watchdog; managed services fence internally |
| Replacement nodes | reconciler script with the Docker socket | Kubernetes operator (CloudNativePG, Zalando, Crunchy), ASG, or the managed service |
| Secrets | dev defaults in the compose file | secret manager; TLS on every hop |
| Data loss on failover | async: up to `maximum_lag_on_failover` | consider Patroni `synchronous_mode: true` (or Aurora's storage-level replication) for zero loss, at the cost of commit latency |

Docker Compose itself provides no database failover: everything here comes
from Patroni and etcd, which are the same tools used in production
self-managed clusters, run on one host for development.

### HA limitations

* **One host.** All nodes, etcd members and HAProxy share one machine; a
  host failure takes everything down. HAProxy is a single instance (in
  production run two, with a virtual IP or DNS).
* **No watchdog** locally (see fencing).
* **Asynchronous replication:** a failover can lose the last commits that
  hadn't reached the promoted replica (bounded by `maximum_lag_on_failover`).
* **Writes fail during a failover** (about `ttl`): Django returns errors for
  writes in that window; reads keep working from the replicas.
* **The reconciler is a script**, not an operator: it handles the scenarios
  above, not every edge case (e.g. a node whose disk is full).

## L7 load balancer

nginx (`nginx:1.30.5-alpine`, service `lb`) is an HTTP load balancer in
front of the Django instances. It has nothing to do with the HAProxy in
`docker-compose.ha.yml`, which is a TCP proxy for PostgreSQL (writer `:5000`,
readers `:5001`); the two can run side by side.

```
client ──HTTP/HTTPS──► nginx :80 / :443 ──HTTP──► backend-1:8000  (Gunicorn, Django)
                         │  least_conn           ► backend-2:8000
                         │  /media/ served from the shared media volume
```

**Routing.** `BACKEND_UPSTREAMS` (`backend-1:8000,backend-2:8000`) lists the
instances; `nginx/entrypoint.d/40-pixelforge-config.sh` renders them into
the upstream block at container start. Requests go to the instance with the
fewest active connections (`least_conn`). Server names are re-resolved
through Docker's DNS every 10 s (`resolve`), so a restarted instance with a
new IP is found again. The frontend is unchanged: it still calls
`http://localhost:8000`.

**Health and failure handling.** Open-source nginx has no active health
checks, so unhealthy instances are detected passively, from real traffic:

* a connection error, a timeout or a 502/503/504 counts as a failure and the
  request is retried on the next instance (`proxy_next_upstream`, at most 3
  tries within 15 s). POST/PUT/PATCH/DELETE are retried only when the
  request never reached the first instance (connection refused/timeout), so
  a checkout is never sent twice;
* after `NGINX_MAX_FAILS` (3) failures within `NGINX_FAIL_TIMEOUT` (10 s) the
  instance is skipped for 10 s, then tried again.

Docker's own health check on every instance is `GET /api/health/` (503 only
when the PostgreSQL primary is unreachable, 200 when degraded) and on nginx a
local-only `/nginx-health`. For active checks in production, use nginx Plus
(`health_check uri=/api/health/`), HAProxy/Envoy, or the cloud load
balancer's target health checks on the same endpoint.

| Setting (env on `lb`) | Default | |
|-|-|-|
| `BACKEND_UPSTREAMS` | `backend-1:8000,backend-2:8000` | instances, comma or space separated |
| `NGINX_MAX_FAILS` / `NGINX_FAIL_TIMEOUT` | `3` / `10s` | passive health check |
| `NGINX_PROXY_CONNECT_TIMEOUT` | `3s` | connect to an instance |
| `NGINX_PROXY_READ_TIMEOUT` | `65s` | read/send timeout (Gunicorn's `--timeout` is 60 s) |
| `NGINX_CLIENT_MAX_BODY_SIZE` | `25m` | uploads |
| `NGINX_TLS_CERT` / `NGINX_TLS_KEY` | `/etc/nginx/certs/tls.crt` / `.key` | HTTPS on :443 (host `8443`) when both exist |
| `NGINX_REDIRECT_HTTPS`, `NGINX_HSTS` | `false` | redirect HTTP to HTTPS; send HSTS |
| `LB_HTTP_PORT`, `LB_HTTPS_PORT` | `8000`, `8443` | host ports (Compose) |

**Forwarded headers and trust.** nginx sends the original `Host` (with port,
so `build_absolute_uri` gives the right image URLs), `X-Forwarded-For`
(client chain + peer), `X-Forwarded-Proto` (`http`/`https`), `X-Real-IP` and
`X-Request-ID` (the client's if valid, else nginx's `$request_id`).
Django believes them only from `TRUSTED_PROXIES` (IPs, CIDRs or hostnames;
`lb` in Compose): `apps.common.proxy.TrustedProxyMiddleware`, first in
`MIDDLEWARE`, sets `REMOTE_ADDR` to the nearest untrusted address of the
`X-Forwarded-For` chain (read right to left) and keeps `X-Forwarded-Proto`
for `SECURE_PROXY_SSL_HEADER`. From any other peer it deletes every
forwarded header, so a client that reaches an instance directly can't fake
its IP (rate limits) or claim HTTPS. DRF's throttling uses that
`REMOTE_ADDR` (`NUM_PROXIES = 0`). With `TRUSTED_PROXIES` empty (runserver,
the single-instance mode) nothing is trusted, as before.

If another load balancer (e.g. a cloud ALB) terminates TLS in front of
nginx, add its address range to `TRUSTED_PROXIES` and change
`X-Forwarded-Proto $scheme` to `$http_x_forwarded_proto` in
`nginx/templates/proxy.conf.template` (only when nginx is reachable from
that load balancer alone).

**TLS.** nginx terminates TLS (TLS 1.2/1.3, HTTP/2) when a certificate is
mounted in `nginx/certs/` (see `nginx/certs/README.md` for a self-signed one);
the instances get plain HTTP on the private `edge` network. In production
also set `SECURE_SSL_REDIRECT=True`, `SESSION_COOKIE_SECURE=True`,
`CSRF_COOKIE_SECURE=True`, `CSRF_TRUSTED_ORIGINS=https://your.domain`,
`ALLOWED_HOSTS=your.domain`, `NGINX_REDIRECT_HTTPS=true` and `NGINX_HSTS=true`
(or `SECURE_HSTS_SECONDS`).

**Streaming and SSE.** The application doesn't stream today. Responses are
buffered by nginx (a Gunicorn worker is freed as soon as the response is
written); a streaming or SSE view opts out per response with the header
`X-Accel-Buffering: no`, and needs a `location` with a longer
`proxy_read_timeout` for long-lived streams.

**Stateless instances, no sticky sessions.** Any instance can serve any
request: Django sessions are in PostgreSQL, the cache and the
read-your-writes markers in Redis (shared; with Redis down every read uses
the primary), uploaded media on one volume shared by the instances, nginx
and the image workers (object storage in production). In-process counters
(cache and routing metrics in the health endpoints) are per instance; the
health response names the answering `instance`.

**Network exposure.** Only nginx publishes ports on all interfaces. The
instances have no host ports; PostgreSQL (`5433`, `5434`) and Kafka's host
listener (`9094`) are bound to `127.0.0.1`; Redis and Kafka's internal
listener aren't published. nginx is only on the `edge` network and can't
reach PostgreSQL, Redis or Kafka.

**More instances:** add a `backend-3` service (copy `backend-2`, change
`hostname`/`INSTANCE_NAME`) and set `BACKEND_UPSTREAMS=backend-1:8000,backend-2:8000,backend-3:8000`.

**Checks.** `scripts/lb/check_lb.sh` validates the nginx config, checks that
both instances serve requests, stops `backend-2` and requires every request
(GET and POST) to succeed, restarts it and waits until it rejoins, and checks
that no internal port is published publicly. `LB_TEST_URL=http://localhost:8000
uv run manage.py test apps.common.tests_lb` checks distribution, identical
responses, headers and that `/nginx-health` isn't public.

## Background tasks (Kafka)

### What runs in the background

| Event | Written by (same transaction as) | Topic, key | Consumer groups → task |
|-|-|-|-|
| `order.placed` | checkout (`orders.services.place_order`) | `order-events`, order number | notifications → order email; audit |
| `payment.succeeded` / `payment.failed` / `payment.refunded` | verified payment status change (`payments.services.apply_result`) | `payment-events`, order number | notifications → customer email, staff alert (refund needed / review); audit |
| `inventory.low_stock` | a new `LowStockAlert` (`catalog.inventory`) | `inventory-events`, variant id | notifications → staff alert; audit |
| `notification.password_reset_requested`, `notification.password_changed` | forgot/reset password views | `notification-events`, user id | notifications → email (the reset token is generated by the worker, never stored in the event) |
| `image.uploaded` | admin image upload | `image-processing`, file path | images → WebP variants (320/800/1600 px, no EXIF/GPS) in `ProcessedImage` |
| `audit.login_succeeded`, `audit.login_failed`, `audit.product_*` | best effort, after commit (no outbox) | `audit-events`, aggregate id | audit → `AuditEvent` |

**Not moved:** checkout, stock reservation (`InventoryManager`), payment
signature checks, provider inquiries and payment/order state transitions
stay synchronous, inside their transactions, exactly as before. The events
are written *by* those transactions; the work they trigger is optional
follow-up (emails, images, audit). A request returns as soon as its
transaction (with its outbox rows) has committed.

Event payloads carry identifiers and non-sensitive facts (order number,
status, amount); workers load everything else from PostgreSQL. No
credentials, tokens, provider responses, transaction references, addresses
or card data are put in events.

### Kafka client and broker

* **Client: [confluent-kafka](https://github.com/confluentinc/confluent-kafka-python) 2.15.1**
  (librdkafka 2.15). Maintained by Confluent, ships manylinux wheels (no
  compiler in the image), supports the idempotent producer, `acks=all`,
  manual offset commits, pause/resume/seek (used for retry scheduling) and
  the Admin API (topic creation, consumer-group offsets for lag). kafka-python
  has no idempotent producer and aiokafka is asyncio-only.
* **Broker: `apache/kafka:4.3.1`** in KRaft mode (no ZooKeeper; Kafka 4 is
  KRaft-only), one combined broker/controller locally. Topics are created
  only by `kafka_create_topics` (`auto.create.topics.enable=false`).
* No Celery: each consumer group is a plain management command
  (`kafka_worker <group>`) that owns its Kafka consumer.

### Transactional outbox

The failure this prevents: Django commits an order, then crashes before
publishing its Kafka message; the order exists but its email is never sent.

```
request ── BEGIN ─ INSERT order … ─ INSERT apps_outbox_event (pending) ─ COMMIT ──► response
                                                   │
outbox_publisher (separate process) ───────────────┘
  1. claim due pending rows: SELECT … FOR UPDATE SKIP LOCKED + lease (locked_until), COMMIT
  2. produce each row (idempotent producer, acks=all), flush
  3. delivery callback OK  → status=published, published_at, partition/offset
     error / no ack       → attempts+1, available_at = now + backoff (1s, 2s, … 60s), stays pending
     permanent error      → status=failed (message too large, not authorised) for an operator
```

* The order and its outbox row commit or roll back together: a rolled-back
  checkout has no event, a committed one always has one.
* **Publishing is not atomic with the commit.** It happens later, at least
  once. If the publisher dies after Kafka acknowledged a row but before
  marking it, the row is published again: consumers deduplicate on the
  `event_id`. Kafka producer idempotence only removes duplicates from the
  producer's own internal retries.
* No transaction is held open while talking to Kafka. Several publishers can
  run (`SKIP LOCKED`); a dead publisher's claims expire after
  `OUTBOX_LEASE_SECONDS` and are taken over, so nothing pending is lost
  across restarts.
* While Kafka is down, requests keep succeeding (`/api/health/` reports
  `degraded`), rows accumulate, and they are published when the broker is
  back (tested: three password-reset events written during a broker outage
  were published within 8 s of its restart, one email each).
* **Best-effort events:** audit records of logins and admin edits use
  `outbox.audit()`, which skips the outbox: after the commit they are handed
  to an in-memory Kafka producer and are lost if Kafka is down or the
  instance dies first (counted as `audit_dropped_total`). Only for records
  nobody acts on.
* Rows: `OutboxEvent` (`apps_outbox_event`): `id` (message id), `event_id`,
  `kind` (`event`, or `forward` for a worker's retry/DLQ message), `event_type`,
  `topic`, `key`, `aggregate_type`, `aggregate_id`, `payload` (the exact
  message), `headers`, `status`, `attempts`, `available_at`, `locked_until`,
  `last_error`, `created_at`, `published_at`, `kafka_partition`/`offset`.
  `manage.py outbox cleanup` deletes published rows after
  `OUTBOX_RETENTION_DAYS` (run it daily).

### Topics and partitions

| Topic | Partitions (dev) | Retention | Key |
|-|-|-|-|
| `order-events`, `payment-events` | 3 | 7 days | order number |
| `inventory-events` | 3 | 7 days | variant id |
| `notification-events` | 3 | 7 days | user id |
| `image-processing` | 3 | 7 days | file path |
| `audit-events` | 3 | 7 days | aggregate id |
| `retry-events` | 3 | 7 days | the original key |
| `dead-letter-events` | 1 | 30 days | the original key |

One topic per stream, not per task or entity. The key keeps one order's
events in one partition, in order; **Kafka orders messages only within a
partition**, never across partitions or topics, and a retried event leaves
its partition (later events for the same key can be processed first), so
tasks load the current state from PostgreSQL instead of relying on order.
Names can be namespaced with `KAFKA_TOPIC_PREFIX` or overridden per topic
(`KAFKA_TOPIC_ORDER_EVENTS=…`).

Production tuning: replication factor 3 and `min.insync.replicas=2`
(`KAFKA_TOPIC_REPLICATION_FACTOR`, `KAFKA_TOPIC_MIN_INSYNC_REPLICAS`) on at
least 3 brokers; partitions = the most workers of one group you want working
in parallel (6–12 for orders/payments is plenty here; partitions can be
added later, which remaps keys, but never removed); retention longer than
the longest outage you want to recover from without replays (and shorter
than `EVENTS_PROCESSED_RETENTION_DAYS`); a longer DLQ retention.

### Consumer groups

| Group (`kafka_worker <name>`) | Kafka group id | Topics | Compose |
|-|-|-|-|
| `notifications` | `pixelforge-notifications` | order, payment, inventory, notification events + `retry-events` | `worker-notifications` × `NOTIFICATION_WORKERS` (2) |
| `images` | `pixelforge-images` | `image-processing` + `retry-events` | `worker-images` |
| `audit` | `pixelforge-audit` | order, payment, inventory, audit events + `retry-events` | `worker-audit` |
| `dlq-archiver` | `pixelforge-dlq-archiver` | `dead-letter-events` | `worker-dlq` |

Every group receives every message of its topics (the notifications and
audit groups both see each `order.placed`); inside a group Kafka gives each
partition to one worker, so workers of a group share the load (at most one
busy worker per partition; extras stand by and take over on failure). A
worker that stops (or doesn't poll for `KAFKA_MAX_POLL_INTERVAL_MS`) leaves
the group and its partitions move to the others, from the last committed
offset.

### Retries and backoff

A task fails → it is classified and either retried, or dead-lettered:

| Class | Examples | Outcome |
|-|-|-|
| retryable | `RetryableError`, `TaskTimeout`, connection/timeout errors, `OperationalError`, SMTP disconnects, `TaskInProgress` | retry, until `max_attempts` |
| non-retryable | `NonRetryableError`, invalid message, missing object, refused recipient, `KeyError`/`TypeError`/`ValueError` | dead letter at once |
| unknown | anything else | retried while `EVENTS_RETRY_UNKNOWN_ERRORS` (default) |

Extra classes come from `EVENTS_RETRYABLE_EXCEPTIONS` /
`EVENTS_NON_RETRYABLE_EXCEPTIONS` (dotted paths). Delay after attempt *n*:
`min(max_delay, base × multiplier^(n-1))` ± `jitter` (a fraction, so retries
of many messages that failed together are spread out), capped at
`max_delay`. Defaults: 5 attempts in total, 1 s base, ×2, ±20 %, 300 s max,
so about 1 s, 2 s, 4 s, 8 s and then the DLQ. A task may run at most
`EVENTS_TASK_TIMEOUT` (60 s; image processing 120 s) before it is
interrupted and retried (`KAFKA_MAX_POLL_INTERVAL_MS` must exceed it).
Tasks can override the policy: `@task("images", "image.uploaded", timeout=120)`.

**Delayed delivery without sleeping.** A retry is a message on
`retry-events` with `failure.next_retry_at`, `failure.attempt` and
`failure.target_group`. Each group also consumes `retry-events`; messages for
other groups are skipped. A retry that isn't due yet is not slept on: the
worker pauses that partition, rewinds it to the message and resumes it when
it's due, while its other partitions keep flowing. The offset is committed
only after the retry runs. Limitation: a not-yet-due retry holds back the
retries behind it in the same partition (at most `EVENTS_RETRY_MAX_DELAY`).

A retry message keeps the original event untouched and adds `failure`:
`target_group`, `consumer_group`, `task`, `event_type`, `entity_id`,
`attempt`, `max_attempts`, `original_topic`/`partition`/`offset` (of the first
delivery), `error_category` (`transient`, `timeout`, `permanent`,
`invalid_message`, `unknown`), `error_type`, `error_message` (sanitized: no
emails, credentials, URLs with passwords or long tokens), `first_failed_at`,
`last_failed_at`, `next_retry_at`, `correlation_id` (the request id) and
`replay_count`.

**The original message is acknowledged only after its retry or dead letter
is durable.** The worker produces it and waits for Kafka's acknowledgement;
if Kafka can't take it, the message is written to the outbox
(`kind=forward`) and published later by the publisher; if PostgreSQL is
down as well, nothing is committed: the partition is rewound and paused with
a backoff (`EVENTS_FORWARD_BACKOFF`, up to 60 s) and the message is processed
again. A failed task is never dropped.

### Dead letters and replay

After the last attempt (or at once for non-retryable errors and malformed
messages) the message goes to `dead-letter-events` with
`failure.reason` (`retries_exhausted`, `non_retryable`, `invalid_message`)
and `dead_lettered_at`. The `dlq-archiver` group stores each one in
`DeadLetterEvent` (one row per group and event; the topic keeps the raw
messages for 30 days). Nothing is replayed automatically; replay is an
operator action after fixing the cause:

```bash
docker compose exec backend-1 python manage.py kafka_dlq list                    # pending dead letters
docker compose exec backend-1 python manage.py kafka_dlq show 12                 # full message
docker compose exec backend-1 python manage.py kafka_dlq replay 12 13 --note "SMTP fixed"
docker compose exec backend-1 python manage.py kafka_dlq replay --group notifications --all --limit 20
docker compose exec backend-1 python manage.py kafka_dlq discard 14 --note "order cancelled"
```

A replay writes a retry message (attempts start again, `replay_count` + 1,
only for the failed group) to the outbox in the same transaction that marks
the row `replayed`. One command replays at most 100 rows, and an event can
be replayed at most `EVENTS_MAX_DLQ_REPLAYS` (3) times, so a poison message
can't loop. If it fails again it comes back as `pending`. Pending dead
letters make `/api/health/events/` report `degraded`.

### Idempotency

Delivery is **at least once**; duplicates come from publisher restarts,
consumer rebalances, redeliveries after a crash and replays. Business
effects are made idempotent, not "exactly once":

1. `ProcessedEvent (consumer_group, event_id)` (unique) is written after a
   task succeeds and before its offset is committed. A redelivered event
   whose record exists is acknowledged without running the task. A crash
   between the database commit and the Kafka commit therefore only causes a
   skipped redelivery.
2. Each task's effect has its own unique key: `Notification.dedup_key`
   (kind + event id + recipient), `AuditEvent.event_id`,
   `ProcessedImage.source_path`, `DeadLetterEvent (group, event_id)`. This
   covers a crash after the effect but before step 1's record.
3. Emails are claimed before sending (`status=sending` with a lease): two
   workers handling the same event at once can't both send; the second gets
   `TaskInProgress` and retries.

**Limits.** If a worker dies after the SMTP server accepted an email but
before marking it sent, the email is sent again once the claim expires
(SMTP has no idempotency key). Ordering per key is best effort (see
[Topics](#topics-and-partitions)). The DLQ archiver deduplicates on the
message id (an event can be dead-lettered again after a replay).
`ProcessedEvent` rows are kept `EVENTS_PROCESSED_RETENTION_DAYS` (14), longer
than topic retention (`manage.py outbox cleanup` removes older ones).

### Payment events

The JazzCash/Easypaisa flow is unchanged: signatures are checked and the
provider's inquiry API is the authority, inside the same locked
transactions. A redirect alone never marks a payment paid. When a verified
result changes the payment's status, `apply_result` writes
`payment.succeeded`/`failed`/`refunded` to the outbox in that transaction,
so the event exists exactly when the change committed; an unverified PAID
is refused and logged. Duplicate callbacks and IPN retries don't change the
status again and so write no second event; a duplicate Kafka delivery is
deduplicated by the consumers. Payloads: payment id, order number, user id,
method, status, previous status, amount, currency, order status and the
`needs_refund`/`needs_review` flags.

### Inline mode (no Kafka)

Without `KAFKA_BOOTSTRAP_SERVERS` (`EVENTS_BACKEND=inline`: local runserver,
`docker compose up backend`, `docker-compose.ha.yml`, the test suite) the
same outbox rows are written and processed in-process right after the
commit by the same tasks, so emails still go out. A failed task leaves its
row pending with a backoff; `manage.py outbox_publisher` (which also works in
inline mode) retries it. Inline mode has no retry topic or DLQ.

### Observability

* **`GET /api/health/events/`** (public, counts only): `outbox` (pending,
  pending forwards, retrying, failed, `oldest_pending_age_seconds`),
  `kafka` (brokers, missing topics, DLQ topic size, per consumer group: state,
  members, lag in total and per topic), `workers` (per process role: live and
  stale members, partition assignments, summed counters, per-task count,
  failures, failure rate, average and max latency), `dead_letters` (pending
  per group), and `problems` (why it's `degraded`). Always 200: a degraded
  pipeline doesn't take an instance out of the load balancer. The Kafka part
  is cached `EVENTS_HEALTH_CACHE_SECONDS` per instance.
* **`GET /api/health/`** has a compact `checks.events` (backend, outbox,
  Kafka reachability) and turns `degraded` when Kafka is unreachable or the
  outbox backlog is older than `EVENTS_OUTBOX_MAX_AGE`.
* **Heartbeats:** workers and the publisher update `ConsumerHeartbeat` every
  `EVENTS_HEARTBEAT_INTERVAL` with their counters; the Compose health checks
  run `manage.py events_healthcheck`.
* **Counters** (per process, Prometheus-style names): `outbox_published_total`,
  `outbox_publish_failures_total`, `events_processed_total`,
  `events_duplicates_total`, `events_failed_total`, `events_retried_total`,
  `events_dead_lettered_total`, `events_timeouts_total`,
  `events_forward_fallbacks_total`, `events_forward_blocked_total`,
  `audit_dropped_total`, ….
* **Logs:** structured `event=<name> key=value` lines on `apps.events`
  (`outbox_published`, `event_processed`, `event_failed`,
  `event_retry_scheduled`, `event_dead_lettered`, `partition_blocked`,
  `partitions_assigned`, …) with ids, attempts, durations, sanitized errors
  and the `correlation_id`, never payloads. `LOG_FORMAT=json` prints one JSON
  object per line. The request id travels from nginx (`X-Request-ID`) into
  the event and every worker log line about it.

### Operations and troubleshooting

| Symptom | Check | Fix |
|-|-|-|
| `outbox backlog` in health | `manage.py outbox status`; `docker compose logs outbox-publisher` | Kafka down: it drains by itself when the broker is back. Publisher down: `docker compose up -d outbox-publisher` |
| `N outbox row(s) failed permanently` | `last_error` in the admin (Events → Outbox events) | fix the cause, then `manage.py outbox requeue --all-failed` |
| `<group>: no live worker` | `docker compose ps`, `logs worker-<group>` | restart the worker; check `KAFKA_BOOTSTRAP_SERVERS` |
| consumer lag grows | `/api/health/events/` lag per topic; task latency | scale the group (`NOTIFICATION_WORKERS=3`, up to the partition count) or fix a slow dependency |
| `partition_blocked` logs | Kafka and PostgreSQL reachability from the worker | recovers by itself; messages are redelivered |
| dead letters pending | `manage.py kafka_dlq list` / `show` | fix, then `kafka_dlq replay`, or `discard` |
| `missing topics` | `docker compose logs kafka-init` | `docker compose run --rm kafka-init` |
| a worker runs a task twice in its logs | `event_duplicate` lines | expected after rebalances; effects are deduplicated |

### Testing the pipeline

* Unit tests (no broker): retry policy and classification, sanitizing,
  time limits, outbox commit/rollback, inline mode, the publisher with a fake
  producer (acknowledgement, transient and permanent failures, leases), the
  worker with fake clients (commit after success, duplicates, retry metadata
  and backoff, exhaustion → DLQ, non-retryable → DLQ, deferred retries pause
  instead of sleeping, Kafka down → outbox fallback, Kafka and PostgreSQL down
  → no commit, invalid messages), DLQ archive/replay limits, notifications
  (dedup, claims), order/payment/low-stock/password/image events, proxy trust
  and health. They run in the normal suite.
* Integration tests with the real broker (`apps/events/tests/test_kafka_integration.py`,
  own topics per test): outbox → publisher → worker; duplicate delivery;
  retry topic backoff then success; exhaustion → DLQ → archive → replay;
  worker crash after the database commit (redelivery deduplicated); crash
  before processing (redelivered and processed); partitions shared by two
  workers of a group; publisher recovery after Kafka was unreachable; DLQ
  publish failure → outbox → delivered later; verified payment → email.

```bash
docker compose up -d kafka                                    # broker on 127.0.0.1:9094
uv run manage.py test apps.events.tests.test_kafka_integration
```

### Production recommendations

* **Kafka:** 3+ brokers (or a managed service: MSK, Confluent Cloud, Aiven),
  replication factor 3, `min.insync.replicas=2`, `SASL_SSL` with SCRAM or mTLS
  (`KAFKA_SECURITY_PROTOCOL`, `KAFKA_SASL_*`, `KAFKA_SSL_CA_LOCATION`), ACLs:
  the web instances may only write the outbox (they don't talk to Kafka
  except best-effort audit events), the publisher may write the topics, each
  worker may read its topics, commit its group and write
  `retry-events`/`dead-letter-events`. Never publish the broker port.
* **Workers:** scale each group independently, up to its partition count;
  give them `stop_grace_period` > `EVENTS_TASK_TIMEOUT`. Run one publisher
  (or two for availability, accepting best-effort ordering per key).
* **Database:** schedule `manage.py outbox cleanup` daily; watch the outbox
  backlog age, consumer lag, DLQ size and task failure rates (alert on
  `problems` in `/api/health/events/`).
* **Email:** a transactional email provider; its API's idempotency key (if
  any) can close the SMTP duplicate window described above.
* **Media:** object storage (S3, GCS) instead of a shared volume, so web
  instances and image workers can run on different hosts.
* **Load balancer:** two nginx instances (or a cloud load balancer) with
  active health checks on `/api/health/`, TLS certificates from your CA/ACME,
  request rate limits, and `TRUSTED_PROXIES` set to their exact addresses.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `DB_PRIMARY_HOST`, `DB_PRIMARY_PORT`, `DB_PRIMARY_NAME`, `DB_PRIMARY_USER`, `DB_PRIMARY_PASSWORD` | unset | The primary, when `DB_PRIMARY_HOST` is set (else `DATABASE_URL`) |
| `DATABASE_URL` | `postgres://postgres:postgres@localhost:5432/pixelforge` | The primary, when `DB_PRIMARY_HOST` is unset |
| `DB_REPLICA_HOST`, `DB_REPLICA_PORT`, `DB_REPLICA_NAME`, `DB_REPLICA_USER`, `DB_REPLICA_PASSWORD` | unset | The read replica, when `DB_REPLICA_HOST` is set; unset parts default to the primary's |
| `DATABASE_REPLICA_URL` | unset | The read replica as a URL, when `DB_REPLICA_HOST` is unset. No replica configured = every query uses the primary |
| `DB_WRITER_ENDPOINT` | unset | `host[:port]` of a stable writer endpoint (e.g. `haproxy:5000`, an Aurora cluster endpoint); replaces the primary's host/port |
| `DB_READER_ENDPOINT` | unset | `host[:port]` of a load-balanced reader endpoint (alias `replica`); credentials from `DB_REPLICA_*` or the primary's |
| `READ_REPLICA_COUNT` | `0` | Size of a Django-side reader pool: `DB_REPLICA_<i>_HOST/PORT/NAME/USER/PASSWORD` or `DATABASE_REPLICA_<i>_URL` for i = 1..N (takes precedence over the two above) |
| `FAILOVER_ENABLED` | `False` | The writer is an HA cluster: health reports its failover state from `DB_HA_STATUS_URL`, and each worker logs when the writer endpoint reaches a new server. Django never promotes anything. (In `docker-compose.ha.yml` the reconciler also uses it: `false` puts Patroni in maintenance mode.) |
| `DB_HA_STATUS_URL` | unset | Patroni REST API (`/cluster`, `/history`) for the health endpoint, e.g. `http://haproxy:8008` |
| `DB_DESIRED_REPLICA_COUNT` | `READ_REPLICA_COUNT` or number of reader aliases | Replicas the infrastructure should keep; below it the cluster is `degraded` |
| `READ_REPLICA_ENABLED` | `True` | `False` keeps every read on the primary |
| `READ_REPLICA_APPS` | `catalog` | Apps whose models may be read from the replica |
| `RECENT_WRITE_TTL` | `30` | Seconds a caller's last write LSN is remembered (not a replication guarantee) |
| `REPLICA_MAX_LAG` | `10` | Seconds of lag after which every read uses the primary |
| `REPLICA_HEALTH_INTERVAL` | `5` | Seconds between replica health/lag checks (per worker) |
| `REPLICA_FAILURE_COOLDOWN` | `30` | Seconds a worker reads only the primary after a replica error |
| `REPLICA_CONNECT_TIMEOUT` | `2` | Replica connect timeout (seconds) |
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
| `REDIS_URL`, `CACHE_*`, `NEGATIVE_CACHE_*` | see [Cache configuration](#cache-configuration) | Redis cache |
| `INSTANCE_NAME` | host name | Instance name in `/api/health/` (several run behind the load balancer) |
| `TRUSTED_PROXIES` | empty | IPs, CIDRs or hostnames whose `X-Forwarded-*` headers are believed (`lb` in Compose); enables `SECURE_PROXY_SSL_HEADER` |
| `TRUSTED_PROXY_DNS_TTL` | `30` | Seconds a resolved proxy hostname is cached |
| `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS` | `False`/`0` | HTTPS hardening behind the TLS-terminating proxy |
| `CSRF_TRUSTED_ORIGINS` | empty | e.g. `https://shop.example` for the admin over HTTPS |
| `LOG_FORMAT` | `verbose` | `json`: one JSON object per log line |
| `EMAIL_TIMEOUT` | `10` | SMTP timeout (seconds) in the workers |
| `ALERT_ROLES`, `ALERT_EMAILS` | `admin,inventory_manager` / empty | Recipients of staff alerts (low stock, refunds, payments to review) |
| `KAFKA_BOOTSTRAP_SERVERS` | empty | Brokers (`kafka:9092` in Compose). Empty = inline mode |
| `EVENTS_BACKEND` | `kafka` if brokers are set, else `inline` | See [Inline mode](#inline-mode-no-kafka) |
| `KAFKA_SECURITY_PROTOCOL`, `KAFKA_SASL_MECHANISM`, `KAFKA_SASL_USERNAME`, `KAFKA_SASL_PASSWORD`, `KAFKA_SSL_CA_LOCATION` | `PLAINTEXT` / empty | Broker security (production: `SASL_SSL`) |
| `KAFKA_CLIENT_ID`, `KAFKA_CONSUMER_GROUP_PREFIX` | `pixelforge` | Client id prefix; group ids are `<prefix>-<group>` |
| `KAFKA_TOPIC_PREFIX`, `KAFKA_TOPIC_<NAME>` | empty / the logical name | Topic namespacing and per-topic names (e.g. `KAFKA_TOPIC_ORDER_EVENTS`) |
| `KAFKA_TOPIC_PARTITIONS`, `KAFKA_TOPIC_REPLICATION_FACTOR`, `KAFKA_TOPIC_MIN_INSYNC_REPLICAS` | `3`, `1`, `1` | Used by `kafka_create_topics` |
| `KAFKA_TOPIC_RETENTION_HOURS`, `KAFKA_DLQ_RETENTION_HOURS` | `168`, `720` | Topic retention |
| `KAFKA_DELIVERY_TIMEOUT_MS`, `KAFKA_ADMIN_TIMEOUT` | `30000`, `3` | Producer delivery bound; admin/health call timeout (seconds) |
| `KAFKA_MAX_POLL_INTERVAL_MS`, `KAFKA_SESSION_TIMEOUT_MS` | `300000`, `45000` | Consumer liveness (must exceed the task timeout) |
| `OUTBOX_BATCH_SIZE`, `OUTBOX_POLL_INTERVAL`, `OUTBOX_LEASE_SECONDS` | `100`, `1.0`, `60` | Publisher batch, idle poll, claim lease |
| `OUTBOX_RETRY_BASE_DELAY`, `OUTBOX_RETRY_MAX_DELAY` | `1`, `60` | Publisher backoff after a failed publish |
| `OUTBOX_RETENTION_DAYS`, `EVENTS_PROCESSED_RETENTION_DAYS` | `7`, `14` | `outbox cleanup`: published rows, idempotency records |
| `EVENTS_RETRY_MAX_ATTEMPTS`, `EVENTS_RETRY_BASE_DELAY`, `EVENTS_RETRY_MULTIPLIER`, `EVENTS_RETRY_MAX_DELAY`, `EVENTS_RETRY_JITTER` | `5`, `1`, `2`, `300`, `0.2` | Task retry policy |
| `EVENTS_RETRYABLE_EXCEPTIONS`, `EVENTS_NON_RETRYABLE_EXCEPTIONS`, `EVENTS_RETRY_UNKNOWN_ERRORS` | empty, empty, `True` | Error classification |
| `EVENTS_TASK_TIMEOUT` | `60` | Seconds a task may run |
| `EVENTS_FORWARD_BACKOFF`, `EVENTS_FORWARD_BACKOFF_MAX` | `5`, `60` | Backoff when a failure can be recorded neither in Kafka nor PostgreSQL |
| `EVENTS_MAX_DLQ_REPLAYS` | `3` | Replays per dead letter |
| `EVENTS_INLINE_DISPATCH` | `True` | Inline mode: process rows right after commit |
| `EVENTS_HEARTBEAT_INTERVAL`, `EVENTS_HEARTBEAT_STALE_AFTER` | `10`, `60` | Worker/publisher heartbeats |
| `EVENTS_HEALTH_CACHE_SECONDS`, `EVENTS_OUTBOX_MAX_AGE`, `EVENTS_MAX_CONSUMER_LAG` | `10`, `60`, `1000` | Health caching and thresholds |
| `BACKEND_UPSTREAMS`, `NGINX_*`, `LB_HTTP_PORT`, `LB_HTTPS_PORT` | see [L7 load balancer](#l7-load-balancer) | Load balancer (env of the `lb` service) |
| `NOTIFICATION_WORKERS`, `GUNICORN_WORKERS` | `2`, `3` | Compose: notification workers, Gunicorn workers per instance |

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
   `confirmed` (once), and writes `payment.succeeded` to the outbox in the
   same transaction (the confirmation email is sent by a worker; see
   [Payment events](#payment-events)). The browser lands on
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
| `kafka_create_topics [--wait S] [--dry-run]` | Create the Kafka topics (idempotent) |
| `outbox_publisher [--once]` | Publish the outbox to Kafka (inline mode: process it in-process) |
| `kafka_worker <group>` | Consumer worker: `notifications`, `images`, `audit`, `dlq-archiver` |
| `kafka_dlq list\|show\|replay\|discard` | Dead letters (see [Dead letters and replay](#dead-letters-and-replay)) |
| `outbox status\|requeue\|cleanup` | Outbox backlog, requeue failed rows, delete old published rows and idempotency records |
| `events_healthcheck worker\|outbox-publisher` | Container health check (fresh heartbeat on this host) |

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
The old services' outbox tables aren't copied (the current outbox,
`apps_outbox_event`, starts empty). Django
groups/permissions aren't copied either, since their content-type ids
differ per database; re-run `setup_rbac`.
