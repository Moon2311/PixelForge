"""Payment pipeline counters and structured logs.

Counters live in ``apps.events.metrics`` (registered there), so the payment
worker's heartbeat carries them to /api/health/events/ and
/api/health/payments/ like every other pipeline counter. Gateway latency is
recorded per ``gateway.<provider>.inquiry``.

Logs go to ``apps.payments`` as ``event=<name> key=value ...`` with the same
fields in ``extra`` (LOG_FORMAT=json emits them as JSON). Fields are ids,
statuses, counts and sanitized errors only: never provider responses,
hashes, credentials or customer data.
"""

import logging

from apps.events import metrics as pipeline

logger = logging.getLogger("apps.payments")

COUNTERS = (
    "payment_attempts_created_total",
    "payment_status_paid_total",
    "payment_status_failed_total",
    "payment_status_unknown_total",
    "payment_gateway_calls_total",
    "payment_gateway_failures_total",
    "payment_gateway_rejected_total",       # refused by an open circuit (nothing sent)
    "payment_circuit_opened_total",
    "payment_circuit_closed_total",
    "payment_verifications_requested_total",
    "payment_verifications_throttled_total",
    "payment_verifications_deferred_total",  # not queued: circuit open (reconciliation retries)
    "payment_duplicate_notifications_total",
    "payment_invalid_notifications_total",
    "payment_duplicate_results_total",       # a verified result that changed nothing
    "payment_reconciled_total",
    "payment_escalated_total",
)
pipeline.register(*COUNTERS)

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def incr(name, amount=1):
    pipeline.incr(name, amount)


def observe_gateway(provider, duration_ms, failed=False):
    pipeline.observe(f"gateway.{provider.lower()}.inquiry", duration_ms, failed=failed)


def event(level, name, **fields):
    text = " ".join(f"{key}={value}" for key, value in fields.items())
    extra = {(f"field_{key}" if key in _RESERVED else key): value for key, value in fields.items()}
    getattr(logger, level)("event=%s %s", name, text, extra={"event": name, **extra})
