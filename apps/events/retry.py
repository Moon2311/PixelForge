"""Retry policy and error classification for background tasks.

A failed attempt is classified:

* non-retryable (``NonRetryableError``, data errors such as a missing
  object or a refused recipient, invalid messages) -> dead letter at once;
* retryable (``RetryableError``, timeouts, connection/database/SMTP
  transient errors) -> retried after a backoff until ``max_attempts``,
  then dead-lettered;
* anything else -> retried when EVENTS_RETRY_UNKNOWN_ERRORS (the default).

Backoff for attempt n (n attempts made so far, n >= 1)::

    delay = min(max_delay, base_delay * multiplier ** (n - 1))
    delay = delay * uniform(1 - jitter, 1 + jitter), capped at max_delay

so with the defaults (1s base, x2, 20% jitter) the retries come after about
1s, 2s, 4s, 8s. The jitter spreads retries of many messages that failed
together (e.g. while SMTP was down) instead of retrying them all at once.
"""

import random
import re
import smtplib
from dataclasses import dataclass, field, replace
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError, InterfaceError, OperationalError
from django.utils.module_loading import import_string

from .envelope import InvalidMessage

CATEGORY_TIMEOUT = "timeout"
CATEGORY_TRANSIENT = "transient"
CATEGORY_PERMANENT = "permanent"
CATEGORY_INVALID = "invalid_message"
CATEGORY_UNKNOWN = "unknown"


class RetryableError(Exception):
    """Raise from a task for a failure that may succeed later."""


class NonRetryableError(Exception):
    """Raise from a task for a failure that retrying can't fix."""


class TaskTimeout(RetryableError):
    """The task ran longer than its policy's ``timeout``."""


class TaskInProgress(RetryableError):
    """Another consumer is processing the same work right now (its claim
    hasn't expired); try again later."""


DEFAULT_NON_RETRYABLE = (
    NonRetryableError,
    InvalidMessage,
    ObjectDoesNotExist,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    KeyError,
    TypeError,
    ValueError,
)
DEFAULT_RETRYABLE = (
    RetryableError,
    TimeoutError,
    ConnectionError,
    OperationalError,
    InterfaceError,
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    OSError,
    DatabaseError,
)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    multiplier: float = 2.0
    max_delay: float = 300.0
    jitter: float = 0.2
    timeout: float = 60.0
    retryable: tuple = field(default=DEFAULT_RETRYABLE)
    non_retryable: tuple = field(default=DEFAULT_NON_RETRYABLE)
    retry_unknown: bool = True

    @classmethod
    def from_settings(cls, **overrides):
        policy = cls(
            max_attempts=max(1, settings.EVENTS_RETRY_MAX_ATTEMPTS),
            base_delay=settings.EVENTS_RETRY_BASE_DELAY,
            multiplier=settings.EVENTS_RETRY_MULTIPLIER,
            max_delay=settings.EVENTS_RETRY_MAX_DELAY,
            jitter=settings.EVENTS_RETRY_JITTER,
            timeout=settings.EVENTS_TASK_TIMEOUT,
            retryable=DEFAULT_RETRYABLE + _classes(settings.EVENTS_RETRYABLE_EXCEPTIONS),
            non_retryable=DEFAULT_NON_RETRYABLE + _classes(settings.EVENTS_NON_RETRYABLE_EXCEPTIONS),
            retry_unknown=settings.EVENTS_RETRY_UNKNOWN_ERRORS,
        )
        return replace(policy, **overrides) if overrides else policy

    def delay(self, attempt, rng=random):
        """Seconds to wait after ``attempt`` failed attempts (>= 1)."""
        attempt = max(1, attempt)
        base = min(self.max_delay, self.base_delay * self.multiplier ** (attempt - 1))
        if self.jitter:
            base *= rng.uniform(1 - self.jitter, 1 + self.jitter)
        return max(0.0, min(self.max_delay, base))

    def next_retry_at(self, now, attempt, rng=random):
        return now + timedelta(seconds=self.delay(attempt, rng))

    def is_retryable(self, exc):
        # Explicit task decisions first, then the configured lists. Non-
        # retryable wins for classes in both lists (e.g. SMTPRecipientsRefused
        # is an OSError).
        if isinstance(exc, RetryableError):
            return True
        if isinstance(exc, self.non_retryable):
            return False
        if isinstance(exc, self.retryable):
            return True
        return self.retry_unknown

    def category(self, exc):
        if isinstance(exc, (TaskTimeout, TimeoutError)):
            return CATEGORY_TIMEOUT
        if isinstance(exc, InvalidMessage):
            return CATEGORY_INVALID
        if isinstance(exc, RetryableError) or (
            not isinstance(exc, self.non_retryable) and isinstance(exc, self.retryable)
        ):
            return CATEGORY_TRANSIENT
        if isinstance(exc, self.non_retryable):
            return CATEGORY_PERMANENT
        return CATEGORY_UNKNOWN


def _classes(paths):
    return tuple(import_string(path) for path in paths if path)


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_SECRET_FIELD = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|salt|hash)\b\s*[=:]\s*\S+")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_URL_CREDENTIALS = re.compile(r"://[^/\s:@]+:[^/\s@]+@")


def sanitize_error(exc, limit=300):
    """``Type: message`` without email addresses, credentials or long tokens,
    for retry/DLQ metadata and logs (which operators and other services read)."""
    text = f"{type(exc).__name__}: {exc}"
    text = _URL_CREDENTIALS.sub("://<redacted>@", text)
    text = _SECRET_FIELD.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _EMAIL.sub("<email>", text)
    text = _LONG_TOKEN.sub("<redacted>", text)
    return text[:limit]
