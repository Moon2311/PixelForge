"""Per-process event counters and structured event logs.

Same conventions as apps.common.cache.metrics / apps.common.db.metrics:
Prometheus-style names, reset when the process restarts. Workers and the
publisher copy a snapshot into their ``ConsumerHeartbeat`` row so the web
instances' health endpoint can report them.

Events are logged on ``apps.events`` as ``event=<name> key=value ...`` with
the same fields in ``extra`` (LOG_FORMAT=json emits them as JSON). Fields are
identifiers, counts and sanitized errors only, never payloads.
"""

import logging
import threading
from collections import Counter, defaultdict

logger = logging.getLogger("apps.events")

COUNTERS = (
    "outbox_enqueued_total",
    "outbox_published_total",
    "outbox_publish_failures_total",
    "outbox_permanent_failures_total",
    "events_consumed_total",
    "events_processed_total",
    "events_duplicates_total",
    "events_ignored_total",
    "events_failed_total",
    "events_retried_total",
    "events_dead_lettered_total",
    "events_timeouts_total",
    "events_forward_fallbacks_total",   # retry/DLQ message stored in the outbox instead
    "events_forward_blocked_total",     # neither Kafka nor PostgreSQL took it: redelivered later
    "events_retry_deferred_total",      # retry not due yet: partition paused
    "audit_published_total",
    "audit_dropped_total",
    "inline_dispatched_total",
    "inline_failures_total",
)

_extra_counters = []
_counts = Counter()
_latency = defaultdict(lambda: {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "failures": 0})
_lock = threading.Lock()


def register(*names):
    """Report more counters in ``snapshot()`` (other apps' pipeline metrics,
    e.g. apps.payments.metrics), so worker heartbeats carry them too."""
    with _lock:
        _extra_counters.extend(n for n in names if n not in COUNTERS and n not in _extra_counters)


def incr(name, amount=1):
    with _lock:
        _counts[name] += amount


def observe(task, duration_ms, failed=False):
    with _lock:
        stats = _latency[task]
        stats["count"] += 1
        stats["total_ms"] += duration_ms
        stats["max_ms"] = max(stats["max_ms"], duration_ms)
        if failed:
            stats["failures"] += 1


def snapshot():
    with _lock:
        tasks = {
            task: {
                "count": s["count"],
                "failures": s["failures"],
                "failure_rate": round(s["failures"] / s["count"], 4) if s["count"] else 0.0,
                "avg_ms": round(s["total_ms"] / s["count"], 1) if s["count"] else 0.0,
                "max_ms": round(s["max_ms"], 1),
            }
            for task, s in _latency.items()
        }
        names = (*COUNTERS, *_extra_counters)
        return {"counters": {name: _counts[name] for name in names}, "tasks": tasks}


def reset():
    with _lock:
        _counts.clear()
        _latency.clear()


_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def event(level, name, **fields):
    text = " ".join(f"{key}={value}" for key, value in fields.items())
    extra = {(f"field_{key}" if key in _RESERVED else key): value for key, value in fields.items()}
    getattr(logger, level)("event=%s %s", name, text, extra={"event": name, **extra})
