"""Django settings for the PixelForge modular monolith.

One Django project serves every module (authentication, catalog, search,
cart) from a single process and a single PostgreSQL database.
"""

from pathlib import Path

import environ
from corsheaders.defaults import default_headers

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

# SECURITY WARNING: override SECRET_KEY and DEBUG in production.
SECRET_KEY = env.str(
    "SECRET_KEY",
    default="django-insecure-dev-only-change-me-jtw7@j7(&j(jjr&^7-$88w_+&g%_61*x*taw",
)
DEBUG = env.bool("DEBUG", default=True)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])


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
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
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
# Database — one PostgreSQL database for every module
# ---------------------------------------------------------------------------

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://postgres:postgres@localhost:5432/pixelforge",
    ),
}
DATABASES["default"].update({
    "CONN_MAX_AGE": env.int("DATABASE_CONN_MAX_AGE", default=600),
    "CONN_HEALTH_CHECKS": True,
    "OPTIONS": {"connect_timeout": 10},
})


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
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "verbose",
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
