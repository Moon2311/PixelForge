"""Per-worker read-routing counters (reset when the worker restarts), shown by
/api/health/database/, and structured routing events. Same conventions as
apps.common.cache.metrics.

Events are logged on ``apps.db`` as ``event=<name> key=value ...`` (and the
same fields in ``extra`` for JSON log formatters). They never contain
credentials: only aliases, statuses, LSNs, lag and error messages, which
psycopg2 limits to host/port/user.
"""

import logging
import threading
from collections import Counter, defaultdict

logger = logging.getLogger("apps.db")

COUNTERS = (
    "db_replica_reads_total",            # replica-eligible reads sent to a replica
    "db_primary_reads_total",            # replica-eligible reads sent to the primary
    "db_read_your_writes_primary_total", # ...because no replica had replayed the caller's write
    "db_replica_errors_total",           # failed replica probes or queries
    "db_replica_lagging_total",          # health checks that found a replica too far behind
    "db_replica_retries_total",          # GETs re-run on the primary after a replica failure
    "db_recent_write_marks_total",       # read-your-writes markers stored after a commit
    "db_writer_routing_total",           # writes routed (always to the writer)
    "db_failover_total",                 # writer endpoint seen reaching a new server (HA mode)
)
# Counters with one value per target: reads per replica alias (or "primary").
LABELED = ("db_reader_routing_total",)

_counts = Counter()
_labeled = defaultdict(Counter)
_lock = threading.Lock()


def incr(name, amount=1, label=None):
    with _lock:
        if label is None:
            _counts[name] += amount
        else:
            _labeled[name][label] += amount


def snapshot():
    with _lock:
        data = {name: _counts[name] for name in COUNTERS}
        data.update({name: dict(_labeled[name]) for name in LABELED})
        return data


def reset():
    with _lock:
        _counts.clear()
        _labeled.clear()


def event(level, name, **fields):
    """Log a structured routing event, e.g. ``event=replica_removed replica=replica_2``."""
    text = " ".join(f"{key}={value}" for key, value in fields.items())
    getattr(logger, level)("event=%s %s", name, text, extra={"event": name, **fields})
