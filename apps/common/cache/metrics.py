"""In-process cache counters.

Counters are per Gunicorn worker (each worker is its own process) and reset
when the worker restarts. The names follow Prometheus conventions so they
can be exported as-is if a metrics endpoint is added later.
"""

import threading
from collections import Counter

COUNTERS = (
    "cache_hits_total",
    "cache_misses_total",
    "cache_negative_hits_total",
    "cache_sets_total",
    "cache_deletes_total",
    "cache_lock_acquired_total",
    "cache_lock_contention_total",
    "cache_lock_timeout_total",
    "cache_rebuild_total",
    "cache_fenced_skips_total",
    "cache_errors_total",
)

_counts = Counter()
_lock = threading.Lock()


def incr(name, amount=1):
    with _lock:
        _counts[name] += amount


def snapshot():
    """Current value of every counter (0 for ones never incremented)."""
    with _lock:
        return {name: _counts[name] for name in COUNTERS}


def reset():
    with _lock:
        _counts.clear()
