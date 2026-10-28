"""Django settings for the PixelForge modular monolith.

One Django project serves every module (authentication, catalog, search,
cart, orders, payments) from one PostgreSQL primary. Several identical
instances can run behind the L7 load balancer; background work goes through
the transactional outbox to Kafka workers (apps.events).
"""

import socket
from pathlib import Path

import environ
from corsheaders.defaults import default_headers
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

# SECURITY WARNING: override SECRET_KEY and DEBUG in production.
SECRET_KEY = env.str(
    "SECRET_KEY",
    default="django-insecure-dev-only-change-me-jtw7@j7(&j(jjr&^7-$88w_+&g%_61*x*taw",
)
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])

# Name of this application instance (shown by /api/health/; several run
# behind the load balancer). Defaults to the container/host name.
INSTANCE_NAME = env.str("INSTANCE_NAME", default="") or socket.gethostname()


# ---------------------------------------------------------------------------
# Reverse proxy / load balancer. See README "L7 load balancer".
#
# X-Forwarded-For/-Proto/-Host are believed only from TRUSTED_PROXIES (IPs,
# CIDRs or hostnames, e.g. "lb" in Docker Compose); from anyone else they are
# removed before Django sees them. Empty (the default, e.g. runserver) trusts
# no one: REMOTE_ADDR and the request scheme are used as they are.
# ---------------------------------------------------------------------------

TRUSTED_PROXIES = env.list("TRUSTED_PROXIES", default=[])
# Seconds a resolved proxy hostname is cached (containers can change IP).
TRUSTED_PROXY_DNS_TTL = env.int("TRUSTED_PROXY_DNS_TTL", default=30)
# HTTPS detection behind the TLS-terminating proxy. Safe because the header
# is stripped from untrusted clients (apps.common.proxy).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if TRUSTED_PROXIES else None
USE_X_FORWARDED_HOST = False  # the proxy passes the original Host header itself
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=False)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=False)
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "corsheaders",
    "rest_framework",
    # PixelForge modules
    "apps.common",
    "apps.authentication",
    "apps.catalog",
    "apps.search",
    "apps.cart",
    "apps.orders",
    "apps.payments",
    "apps.events",
    "apps.notifications",
]

MIDDLEWARE = [
    # First: everything below sees the real client address and scheme, and
    # forwarded headers from anyone but a trusted proxy are dropped.
    "apps.common.proxy.TrustedProxyMiddleware",
    "apps.common.proxy.RequestIDMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Read routing (primary/replica); before sessions/auth so their writes are seen.
    "apps.common.db.middleware.ReadReplicaMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# ---------------------------------------------------------------------------
# Database — a writer (the PostgreSQL primary: every write) and optional
# readers (hot-standby replicas). See README "Read replica" and "High
# availability". Never log these settings (they hold passwords).
#
#   writer:  DB_WRITER_ENDPOINT (host:port of a stable endpoint that always
#            reaches the current primary, e.g. HAProxy in front of Patroni),
#            else DB_PRIMARY_HOST/..., else DATABASE_URL.
#   readers, first match wins:
#     READ_REPLICA_COUNT=N   N replicas DB_REPLICA_<i>_HOST/... (or
#                            DATABASE_REPLICA_<i>_URL); Django spreads reads
#                            over the healthy ones (aliases replica_1..N).
#     DB_READER_ENDPOINT     one load-balanced reader endpoint (alias replica).
#     DB_REPLICA_HOST/...    one replica (alias replica), as before.
#     DATABASE_REPLICA_URL
# Unset name/user/password parts default to the writer's.
# ---------------------------------------------------------------------------


def _database(prefix, url_var, url_default="", defaults=None):
    """DB_<prefix>_HOST/PORT/NAME/USER/PASSWORD when DB_<prefix>_HOST is set,
    else the URL in ``url_var``, else None. Unset parts come from ``defaults``."""
    defaults = defaults or {}
    host = env.str(f"DB_{prefix}_HOST", default="")
    if host:
        return {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": host,
            "PORT": env.str(f"DB_{prefix}_PORT", default=str(defaults.get("PORT") or 5432)),
            "NAME": env.str(f"DB_{prefix}_NAME", default=defaults.get("NAME", "pixelforge")),
            "USER": env.str(f"DB_{prefix}_USER", default=defaults.get("USER", "postgres")),
            "PASSWORD": env.str(f"DB_{prefix}_PASSWORD", default=defaults.get("PASSWORD", "")),
        }
    url = env.str(url_var, default=url_default)
    return environ.Env.db_url_config(url) if url else None


def _endpoint(name):
    """DB_<name>_ENDPOINT ("host" or "host:port") as {"HOST", "PORT"}, or None."""
    value = env.str(f"DB_{name}_ENDPOINT", default="").strip()
    if not value:
        return None
    host, sep, port = value.rpartition(":")
    if not sep:
        host, port = value, "5432"
    return {"HOST": host, "PORT": port}


_primary = _database("PRIMARY", "DATABASE_URL", "postgres://postgres:postgres@localhost:5432/pixelforge")
_primary.update(_endpoint("WRITER") or {})

READ_REPLICA_COUNT = env.int("READ_REPLICA_COUNT", default=0)
_replicas = {}
if READ_REPLICA_COUNT > 0:
    for _i in range(1, READ_REPLICA_COUNT + 1):
        _config = _database(f"REPLICA_{_i}", f"DATABASE_REPLICA_{_i}_URL", defaults=_primary)
        if _config is None:
            raise ImproperlyConfigured(
                f"READ_REPLICA_COUNT={READ_REPLICA_COUNT} but neither DB_REPLICA_{_i}_HOST "
                f"nor DATABASE_REPLICA_{_i}_URL is set"
            )
        _replicas[f"replica_{_i}"] = _config
elif _endpoint("READER"):
    _replicas["replica"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.str("DB_REPLICA_NAME", default=_primary.get("NAME", "pixelforge")),
        "USER": env.str("DB_REPLICA_USER", default=_primary.get("USER", "postgres")),
        "PASSWORD": env.str("DB_REPLICA_PASSWORD", default=_primary.get("PASSWORD", "")),
        **_endpoint("READER"),
    }
elif _legacy_replica := _database("REPLICA", "DATABASE_REPLICA_URL", defaults=_primary):
    _replicas["replica"] = _legacy_replica

_connection = {
    "CONN_MAX_AGE": env.int("DATABASE_CONN_MAX_AGE", default=600),
    "CONN_HEALTH_CHECKS": True,
}

# Seconds to wait for a replica connection before reading from the primary.
REPLICA_CONNECT_TIMEOUT = env.int("REPLICA_CONNECT_TIMEOUT", default=2)

DATABASES = {
    # The writer: every write, every migration, and every read that needs it.
    "default": {
        **_primary,
        **_connection,
        "OPTIONS": {**_primary.get("OPTIONS", {}), "connect_timeout": 10},
    },
}
for _alias, _config in _replicas.items():
    DATABASES[_alias] = {
        **_config,
        **_connection,
        "OPTIONS": {
            **_config.get("OPTIONS", {}),
            "connect_timeout": REPLICA_CONNECT_TIMEOUT,
            # A hot standby is read-only anyway; this also protects a
            # replica alias that is (mis)configured to point at a primary.
            "options": "-c default_transaction_read_only=on",
        },
        # Tests use the primary's test database through this alias.
        "TEST": {"MIRROR": "default"},
    }
# The reader pool: aliases replica-eligible reads may use.
READ_REPLICA_ALIASES = list(_replicas)

# True when the writer endpoint is an HA cluster with automatic failover
# (Patroni + etcd + HAProxy in docker-compose.ha.yml, or a managed service).
# Django never promotes anything; this only adds the cluster's view to
# /api/health/database/ (from DB_HA_STATUS_URL, a Patroni REST endpoint).
FAILOVER_ENABLED = env.bool("FAILOVER_ENABLED", default=False)
DB_HA_STATUS_URL = env.str("DB_HA_STATUS_URL", default="")
# Replicas the infrastructure keeps running (reported by health checks).
DB_DESIRED_REPLICA_COUNT = env.int("DB_DESIRED_REPLICA_COUNT", default=READ_REPLICA_COUNT or len(_replicas))

DATABASE_ROUTERS = ["apps.common.db.router.PrimaryReplicaRouter"]

# False keeps every read on the primary even when a replica is configured.
READ_REPLICA_ENABLED = env.bool("READ_REPLICA_ENABLED", default=True)
# Apps whose models may be read from the replica (catalog: products,
# categories, brands, banners, reviews; also everything search reads).
READ_REPLICA_APPS = env.list("READ_REPLICA_APPS", default=["catalog"])
# How long (seconds) a caller's last write is remembered in Redis. Reads
# use the replica again only once it has replayed that write (LSN check);
# the TTL is not proof that it has. Must exceed REPLICA_MAX_LAG +
# REPLICA_HEALTH_INTERVAL (checked by common.W002).
RECENT_WRITE_TTL = env.int("RECENT_WRITE_TTL", default=30)
# Seconds the replica may be behind before all reads go to the primary.
REPLICA_MAX_LAG = env.float("REPLICA_MAX_LAG", default=10.0)
# Seconds between replica health/lag checks (per worker).
REPLICA_HEALTH_INTERVAL = env.float("REPLICA_HEALTH_INTERVAL", default=5.0)
# Seconds a worker reads only from the primary after a replica error.
REPLICA_FAILURE_COOLDOWN = env.int("REPLICA_FAILURE_COOLDOWN", default=30)


# ---------------------------------------------------------------------------
# Cache — Redis, shared by every Gunicorn worker. PostgreSQL stays the source
# of truth: the cache only holds copies of read data and may be lost at any
# time. See README "Caching".
# ---------------------------------------------------------------------------

# Docker Compose sets redis://redis:6379/1; never log this URL (it may hold a password).
REDIS_URL = env.str("REDIS_URL", default="redis://127.0.0.1:6379/1")
# Short timeouts: a slow or unreachable Redis must not slow requests down.
REDIS_SOCKET_TIMEOUT = env.float("REDIS_SOCKET_TIMEOUT", default=0.25)

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "SOCKET_CONNECT_TIMEOUT": REDIS_SOCKET_TIMEOUT,
            "SOCKET_TIMEOUT": REDIS_SOCKET_TIMEOUT,
            # Django/DRF internals (e.g. throttling) carry on without Redis.
            # Not logged per call (a traceback per request during an outage):
            # apps.common.cache logs outages once per cooldown and
            # /api/health/ reports them.
            "IGNORE_EXCEPTIONS": True,
        },
    }
}

# apps.common.cache (cache-aside with stampede, penetration and avalanche protection)
CACHE_ENABLED = env.bool("CACHE_ENABLED", default=True)
CACHE_KEY_PREFIX = env.str("CACHE_KEY_PREFIX", default="pixelforge")
# TTLs in seconds; each entry lives base TTL + random(0, jitter).
CACHE_DEFAULT_TTL = env.int("CACHE_DEFAULT_TTL", default=300)
CACHE_TTL_JITTER = env.int("CACHE_TTL_JITTER", default=30)
# "Does not exist" markers (cache penetration).
NEGATIVE_CACHE_TTL = env.int("NEGATIVE_CACHE_TTL", default=60)
NEGATIVE_CACHE_TTL_JITTER = env.int("NEGATIVE_CACHE_TTL_JITTER", default=15)
# Rebuild mutex (cache stampede). The lock outlives the slowest rebuild;
# waiters poll with backoff (retry delay doubling up to the max delay) and
# load from PostgreSQL themselves after the wait timeout.
CACHE_LOCK_TTL = env.float("CACHE_LOCK_TTL", default=5.0)
CACHE_LOCK_WAIT_TIMEOUT = env.float("CACHE_LOCK_WAIT_TIMEOUT", default=2.0)
CACHE_LOCK_RETRY_DELAY = env.float("CACHE_LOCK_RETRY_DELAY", default=0.05)
CACHE_LOCK_RETRY_MAX_DELAY = env.float("CACHE_LOCK_RETRY_MAX_DELAY", default=0.2)
# After a Redis error, each worker skips Redis for this many seconds.
CACHE_FAILURE_COOLDOWN = env.int("CACHE_FAILURE_COOLDOWN", default=30)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Lifetime of the Bearer access token issued at login (seconds).
ACCESS_TOKEN_MAX_AGE = env.int("ACCESS_TOKEN_MAX_AGE", default=60 * 15)
# Lifetime of the refresh token used to get a new access token (seconds).
REFRESH_TOKEN_MAX_AGE = env.int("REFRESH_TOKEN_MAX_AGE", default=60 * 60 * 24 * 7)

# Checkout: shown on the checkout page for Bank Deposit and Pickup.
BANK_DEPOSIT_INSTRUCTIONS = env.str("BANK_DEPOSIT_INSTRUCTIONS", default="")
PICKUP_LOCATION = env.str("PICKUP_LOCATION", default="")

PASSWORD_RESET_TIMEOUT = env.int("PASSWORD_RESET_TIMEOUT", default=3600)

# ---------------------------------------------------------------------------
# Online payments (apps.payments) — a provider is offered at checkout only
# when all of its credentials are set. See README "Online payments".
# ---------------------------------------------------------------------------

# Storefront page the customer lands on after paying; "?payment_id=<id>" is appended.
PAYMENT_RESULT_URL = env.str("PAYMENT_RESULT_URL", default="http://localhost:5173/payment/result")
# How long a payment attempt stays open at the provider (minutes).
PAYMENT_EXPIRY_MINUTES = env.int("PAYMENT_EXPIRY_MINUTES", default=30)
# Timeout for server-to-server provider calls (seconds).
PAYMENT_PROVIDER_TIMEOUT = env.int("PAYMENT_PROVIDER_TIMEOUT", default=20)

# Fault tolerance (README "Payment outages"). Every status inquiry goes
# through a per-provider circuit breaker; verification requests are queued
# for the `payments` consumer group (topic payment-processing).
# Consecutive gateway failures (timeouts, connection errors, HTTP 5xx/429)
# that open the circuit.
PAYMENT_CIRCUIT_FAILURE_THRESHOLD = env.int("PAYMENT_CIRCUIT_FAILURE_THRESHOLD", default=5)
# Seconds the circuit stays open (calls fail fast) before probing again.
PAYMENT_CIRCUIT_RECOVERY_SECONDS = env.float("PAYMENT_CIRCUIT_RECOVERY_SECONDS", default=30.0)
# Successful half-open probes that close the circuit again; one probe at a time.
PAYMENT_CIRCUIT_HALF_OPEN_SUCCESSES = env.int("PAYMENT_CIRCUIT_HALF_OPEN_SUCCESSES", default=2)
# A provider is asked about the same payment at most this often (status
# polling, reconciliation); provider notifications always trigger a check.
PAYMENT_RECHECK_SECONDS = env.float("PAYMENT_RECHECK_SECONDS", default=10.0)
# Verification task retries (payments consumer group): attempts including
# the first, then the dead-letter topic. Backoff base * 2^(n-1) +/- jitter,
# at most the max delay, and never sooner than an open circuit's recovery.
PAYMENT_VERIFY_MAX_ATTEMPTS = env.int("PAYMENT_VERIFY_MAX_ATTEMPTS", default=8)
PAYMENT_VERIFY_RETRY_BASE_DELAY = env.float("PAYMENT_VERIFY_RETRY_BASE_DELAY", default=5.0)
PAYMENT_VERIFY_RETRY_MAX_DELAY = env.float("PAYMENT_VERIFY_RETRY_MAX_DELAY", default=300.0)
# Reconciliation (`manage.py payments_reconcile`): every interval, re-check
# PROCESSING/UNKNOWN payments not verified for PAYMENT_RECONCILE_AFTER
# seconds and PENDING ones that expired that long ago.
PAYMENT_RECONCILE_INTERVAL = env.float("PAYMENT_RECONCILE_INTERVAL", default=60.0)
PAYMENT_RECONCILE_AFTER = env.float("PAYMENT_RECONCILE_AFTER", default=120.0)
PAYMENT_RECONCILE_BATCH = env.int("PAYMENT_RECONCILE_BATCH", default=200)
# An UNKNOWN payment still unresolved after this many seconds is flagged
# for manual review (needs_review, ERROR log); it is never failed or retried
# with a new charge automatically.
PAYMENT_UNKNOWN_ESCALATE_AFTER = env.float("PAYMENT_UNKNOWN_ESCALATE_AFTER", default=3600.0)

# JazzCash Online Payment Gateway (HTTP POST page redirection + Payment Inquiry).
JAZZCASH_ENVIRONMENT = env.str("JAZZCASH_ENVIRONMENT", default="sandbox")  # sandbox | production
JAZZCASH_MERCHANT_ID = env.str("JAZZCASH_MERCHANT_ID", default="")
JAZZCASH_PASSWORD = env.str("JAZZCASH_PASSWORD", default="")
JAZZCASH_INTEGRITY_SALT = env.str("JAZZCASH_INTEGRITY_SALT", default="")
# Public HTTPS URL of /api/payments/jazzcash/callback/ (registered with JazzCash).
JAZZCASH_RETURN_URL = env.str("JAZZCASH_RETURN_URL", default="")
JAZZCASH_VERSION = env.str("JAZZCASH_VERSION", default="1.1")
# Empty lets the customer choose on JazzCash; or MWALLET, MIGS, OTC.
JAZZCASH_TXN_TYPE = env.str("JAZZCASH_TXN_TYPE", default="")
# Sandbox URLs are built in; production URLs are issued by JazzCash.
JAZZCASH_CHECKOUT_URL = env.str("JAZZCASH_CHECKOUT_URL", default="")
JAZZCASH_INQUIRY_URL = env.str("JAZZCASH_INQUIRY_URL", default="")

# Easypaisa / Easypay (hosted checkout + inquire-transaction REST API).
EASYPAISA_ENVIRONMENT = env.str("EASYPAISA_ENVIRONMENT", default="sandbox")  # sandbox | production
EASYPAISA_STORE_ID = env.str("EASYPAISA_STORE_ID", default="")
EASYPAISA_HASH_KEY = env.str("EASYPAISA_HASH_KEY", default="")
# Partner account used for the REST API's Credentials header.
EASYPAISA_USERNAME = env.str("EASYPAISA_USERNAME", default="")
EASYPAISA_PASSWORD = env.str("EASYPAISA_PASSWORD", default="")
EASYPAISA_ACCOUNT_NUM = env.str("EASYPAISA_ACCOUNT_NUM", default="")
# Public HTTPS URL of /api/payments/easypaisa/callback/ (the postBackURL).
EASYPAISA_RETURN_URL = env.str("EASYPAISA_RETURN_URL", default="")
# MA_PAYMENT_METHOD (mobile account), OTC_PAYMENT_METHOD, CC_PAYMENT_METHOD.
EASYPAISA_PAYMENT_METHOD = env.str("EASYPAISA_PAYMENT_METHOD", default="MA_PAYMENT_METHOD")
EASYPAISA_CHECKOUT_URL = env.str("EASYPAISA_CHECKOUT_URL", default="")
EASYPAISA_CONFIRM_URL = env.str("EASYPAISA_CONFIRM_URL", default="")
EASYPAISA_INQUIRY_URL = env.str("EASYPAISA_INQUIRY_URL", default="")

# Frontend page that receives the reset link; "?uid=<uid>&token=<token>" is appended.
PASSWORD_RESET_URL = env.str(
    "PASSWORD_RESET_URL", default="http://localhost:3000/reset-password"
)

# Email — defaults to printing messages to the console for local dev.
EMAIL_BACKEND = env.str(
    "EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = env.str("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env.str("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env.str("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default="PixelForge <no-reply@pixelforge.com>")
# Seconds an SMTP connection may block (emails are sent by background workers).
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=10)
# Who receives operational alerts (low stock, payments needing a refund):
# users holding these roles, plus these addresses.
ALERT_ROLES = env.list("ALERT_ROLES", default=["admin", "inventory_manager"])
ALERT_EMAILS = env.list("ALERT_EMAILS", default=[])


# ---------------------------------------------------------------------------
# Asynchronous events (apps.events): transactional outbox -> Kafka -> workers.
# See README "Background tasks (Kafka)". Never log these settings (SASL
# passwords).
#
# EVENTS_BACKEND
#   "kafka"   business transactions write outbox rows; `outbox_publisher`
#             publishes them to Kafka and `kafka_worker <group>` processes them.
#   "inline"  no Kafka (the default when KAFKA_BOOTSTRAP_SERVERS is unset): the
#             same outbox rows are processed in-process right after the
#             transaction commits, as before Kafka (single-backend development).
# ---------------------------------------------------------------------------

KAFKA_BOOTSTRAP_SERVERS = env.str("KAFKA_BOOTSTRAP_SERVERS", default="")
EVENTS_BACKEND = env.str("EVENTS_BACKEND", default="kafka" if KAFKA_BOOTSTRAP_SERVERS else "inline")
if EVENTS_BACKEND not in ("kafka", "inline"):
    raise ImproperlyConfigured("EVENTS_BACKEND must be 'kafka' or 'inline'")
if EVENTS_BACKEND == "kafka" and not KAFKA_BOOTSTRAP_SERVERS:
    raise ImproperlyConfigured("EVENTS_BACKEND=kafka needs KAFKA_BOOTSTRAP_SERVERS")
KAFKA_CLIENT_ID = env.str("KAFKA_CLIENT_ID", default="pixelforge")
# PLAINTEXT (local Docker network), SSL, SASL_SSL (production).
KAFKA_SECURITY_PROTOCOL = env.str("KAFKA_SECURITY_PROTOCOL", default="PLAINTEXT")
KAFKA_SASL_MECHANISM = env.str("KAFKA_SASL_MECHANISM", default="")
KAFKA_SASL_USERNAME = env.str("KAFKA_SASL_USERNAME", default="")
KAFKA_SASL_PASSWORD = env.str("KAFKA_SASL_PASSWORD", default="")
KAFKA_SSL_CA_LOCATION = env.str("KAFKA_SSL_CA_LOCATION", default="")
# Seconds an admin/metadata call (health checks, topic creation) may take.
KAFKA_ADMIN_TIMEOUT = env.float("KAFKA_ADMIN_TIMEOUT", default=3.0)
# Producer: acks=all + idempotence are always on; this bounds one delivery.
KAFKA_DELIVERY_TIMEOUT_MS = env.int("KAFKA_DELIVERY_TIMEOUT_MS", default=30000)
# Consumers: a worker that doesn't poll for this long leaves its group.
# Must exceed EVENTS_TASK_TIMEOUT.
KAFKA_MAX_POLL_INTERVAL_MS = env.int("KAFKA_MAX_POLL_INTERVAL_MS", default=300000)
KAFKA_SESSION_TIMEOUT_MS = env.int("KAFKA_SESSION_TIMEOUT_MS", default=45000)
# Consumer group ids are "<prefix>-<group>", e.g. pixelforge-notifications.
KAFKA_CONSUMER_GROUP_PREFIX = env.str("KAFKA_CONSUMER_GROUP_PREFIX", default="pixelforge")

# Topic names (logical name -> Kafka topic). KAFKA_TOPIC_PREFIX namespaces
# every topic (e.g. "staging.") on a shared cluster.
KAFKA_TOPIC_PREFIX = env.str("KAFKA_TOPIC_PREFIX", default="")
KAFKA_TOPICS = {
    name: KAFKA_TOPIC_PREFIX + env.str(f"KAFKA_TOPIC_{name.upper().replace('-', '_')}", default=name)
    for name in (
        "order-events",
        "payment-events",
        "payment-processing",
        "inventory-events",
        "notification-events",
        "image-processing",
        "audit-events",
        "retry-events",
        "dead-letter-events",
    )
}
# Topic creation (`manage.py kafka_create_topics`). One broker locally;
# production: 3 brokers, replication factor 3, min.insync.replicas 2.
KAFKA_TOPIC_PARTITIONS = env.int("KAFKA_TOPIC_PARTITIONS", default=3)
KAFKA_TOPIC_REPLICATION_FACTOR = env.int("KAFKA_TOPIC_REPLICATION_FACTOR", default=1)
KAFKA_TOPIC_MIN_INSYNC_REPLICAS = env.int("KAFKA_TOPIC_MIN_INSYNC_REPLICAS", default=1)
KAFKA_TOPIC_RETENTION_HOURS = env.int("KAFKA_TOPIC_RETENTION_HOURS", default=24 * 7)
KAFKA_DLQ_RETENTION_HOURS = env.int("KAFKA_DLQ_RETENTION_HOURS", default=24 * 30)

# Outbox publisher.
OUTBOX_BATCH_SIZE = env.int("OUTBOX_BATCH_SIZE", default=100)
OUTBOX_POLL_INTERVAL = env.float("OUTBOX_POLL_INTERVAL", default=1.0)
# A claimed row is retried by another publisher once its lease runs out.
OUTBOX_LEASE_SECONDS = env.int("OUTBOX_LEASE_SECONDS", default=60)
OUTBOX_RETRY_BASE_DELAY = env.float("OUTBOX_RETRY_BASE_DELAY", default=1.0)
OUTBOX_RETRY_MAX_DELAY = env.float("OUTBOX_RETRY_MAX_DELAY", default=60.0)
# Published rows older than this are removed by `outbox_cleanup`.
OUTBOX_RETENTION_DAYS = env.int("OUTBOX_RETENTION_DAYS", default=7)
# ProcessedEvent (idempotency) rows are kept this long; must exceed the
# topics' retention so a redelivered message is still recognised.
EVENTS_PROCESSED_RETENTION_DAYS = env.int("EVENTS_PROCESSED_RETENTION_DAYS", default=14)
# In inline mode, process outbox rows right after their transaction commits.
EVENTS_INLINE_DISPATCH = env.bool("EVENTS_INLINE_DISPATCH", default=True)

# Background task retries (per attempt n = 1, 2, ...: delay = base *
# multiplier^(n-1), +/- jitter, at most the max delay). EVENTS_RETRY_MAX_ATTEMPTS
# counts every attempt, including the first; then the message goes to the DLQ.
EVENTS_RETRY_MAX_ATTEMPTS = env.int("EVENTS_RETRY_MAX_ATTEMPTS", default=5)
EVENTS_RETRY_BASE_DELAY = env.float("EVENTS_RETRY_BASE_DELAY", default=1.0)
EVENTS_RETRY_MULTIPLIER = env.float("EVENTS_RETRY_MULTIPLIER", default=2.0)
EVENTS_RETRY_MAX_DELAY = env.float("EVENTS_RETRY_MAX_DELAY", default=300.0)
EVENTS_RETRY_JITTER = env.float("EVENTS_RETRY_JITTER", default=0.2)  # fraction of the delay
# Extra exception classes (dotted paths) to retry or to dead-letter at once;
# unknown exceptions are retried unless EVENTS_RETRY_UNKNOWN_ERRORS is False.
EVENTS_RETRYABLE_EXCEPTIONS = env.list("EVENTS_RETRYABLE_EXCEPTIONS", default=[])
EVENTS_NON_RETRYABLE_EXCEPTIONS = env.list("EVENTS_NON_RETRYABLE_EXCEPTIONS", default=[])
EVENTS_RETRY_UNKNOWN_ERRORS = env.bool("EVENTS_RETRY_UNKNOWN_ERRORS", default=True)
# Seconds one task may run before it is abandoned and retried.
EVENTS_TASK_TIMEOUT = env.float("EVENTS_TASK_TIMEOUT", default=60.0)
# Seconds a worker backs off a partition when it can't record a failure
# (Kafka and PostgreSQL both unavailable); the message is redelivered.
EVENTS_FORWARD_BACKOFF = env.float("EVENTS_FORWARD_BACKOFF", default=5.0)
EVENTS_FORWARD_BACKOFF_MAX = env.float("EVENTS_FORWARD_BACKOFF_MAX", default=60.0)
# A dead-lettered event may be replayed at most this many times.
EVENTS_MAX_DLQ_REPLAYS = env.int("EVENTS_MAX_DLQ_REPLAYS", default=3)
# Workers and the publisher record a heartbeat this often (seconds).
EVENTS_HEARTBEAT_INTERVAL = env.float("EVENTS_HEARTBEAT_INTERVAL", default=10.0)
# /api/health/events/ caches its Kafka view for this long (seconds).
EVENTS_HEALTH_CACHE_SECONDS = env.float("EVENTS_HEALTH_CACHE_SECONDS", default=10.0)
# Health thresholds: backlog age / heartbeat age (seconds), lag (messages).
EVENTS_OUTBOX_MAX_AGE = env.float("EVENTS_OUTBOX_MAX_AGE", default=60.0)
EVENTS_HEARTBEAT_STALE_AFTER = env.float("EVENTS_HEARTBEAT_STALE_AFTER", default=60.0)
EVENTS_MAX_CONSUMER_LAG = env.int("EVENTS_MAX_CONSUMER_LAG", default=1000)


# ---------------------------------------------------------------------------
# Internationalization / static / media
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploaded product images
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5 MB per image

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Django REST framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.authentication.authentication.BearerTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.common.pagination.CustomPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    # Every error is answered as apps.common.custom_response.CustomResponse.
    "EXCEPTION_HANDLER": "apps.common.exceptions.custom_exception_handler",
    # Throttling identifies clients by REMOTE_ADDR, which
    # apps.common.proxy has already set from a trusted proxy's
    # X-Forwarded-For (a client-sent X-Forwarded-For is never used).
    "NUM_PROXIES": 0,
    "DEFAULT_THROTTLE_RATES": {
        "password_reset": env.str("PASSWORD_RESET_THROTTLE_RATE", default="5/hour"),
    },
}

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = (*default_headers, "x-guest-session")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
        },
        # One JSON object per line, with the structured ``extra`` fields
        # (event=..., topic=...) that apps.events and apps.db attach.
        "json": {"()": "apps.common.logging.JsonFormatter"},
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            # LOG_FORMAT=json for log shippers; verbose text by default.
            "formatter": env.str("LOG_FORMAT", default="verbose"),
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "catalog": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "cart": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # DEBUG also logs every cache hit/miss/set.
        "apps.cache": {
            "handlers": ["console"],
            "level": env.str("CACHE_LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
    },
}

# The test suite runs without Redis; cache tests opt in (apps/common/test_runner.py).
TEST_RUNNER = "apps.common.test_runner.TestRunner"
