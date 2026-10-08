"""Read replica health, replay position and lag, checked per worker and per
replica alias (``settings.READ_REPLICA_ALIASES``: ``replica`` for one
replica or reader endpoint, ``replica_1..N`` for a pool).

A replica is *usable* for reads when it answers, is a hot standby, and is
at most ``REPLICA_MAX_LAG`` seconds behind the primary. Each alias is
checked at most every ``REPLICA_HEALTH_INTERVAL`` seconds per worker
(lazily, on the next routed read); after an error it isn't retried for
``REPLICA_FAILURE_COOLDOWN`` seconds, so an outage costs one timeout and one
log line per worker per cooldown, like apps.common.cache.client. An
unusable alias is out of the reader pool until a later check finds it
healthy again.

PostgreSQL functions used (all read-only, no special privileges)::

    replica:  pg_is_in_recovery()             standby or not
              pg_last_wal_replay_lsn()        WAL position replayed so far
              pg_last_xact_replay_timestamp() commit time of the last replayed transaction
    primary:  pg_current_wal_lsn()            current WAL write position

Lag in bytes is ``primary LSN - replay LSN``. Lag in seconds is 0 when the
replica has replayed everything, else ``now() - pg_last_xact_replay_timestamp()``
on the replica. That figure overstates lag right after a long idle period
(the last replayed transaction is old), which only sends reads elsewhere
for one check interval, and it depends on the servers' clocks being in sync
(NTP).

The replay position used for read-your-writes is cached per *physical
connection*, not per alias: behind a load-balanced reader endpoint a new
connection can reach a different server, so a position seen on one
connection says nothing about the next.
"""

import threading
import time
from dataclasses import dataclass, replace

from django.conf import settings
from django.db import DatabaseError, connections

from apps.common.db import PRIMARY, metrics

OK = "ok"
LAGGING = "lagging"
UNAVAILABLE = "unavailable"
NOT_STANDBY = "not_standby"  # answers, but isn't replaying WAL from a primary
NOT_CONFIGURED = "not_configured"
DISABLED = "disabled"  # configured, but READ_REPLICA_ENABLED=False
UNKNOWN = "unknown"  # not checked yet

# Replica states that still serve every request (from elsewhere), but are out of the pool.
DEGRADED = (UNAVAILABLE, LAGGING, NOT_STANDBY)


@dataclass(frozen=True)
class ReplicaState:
    status: str
    checked_at: float = 0.0  # time.monotonic() of the last check
    replay_lsn: int | None = None
    primary_lsn: int | None = None
    lag_bytes: int | None = None
    lag_seconds: float | None = None
    error: str = ""


_states = {}
_check_locks = {}
_locks_guard = threading.Lock()


# ---------------------------------------------------------------------------
# LSNs
# ---------------------------------------------------------------------------


def parse_lsn(text):
    """``'16/B374D848'`` -> int; None stays None."""
    if text is None:
        return None
    high, _, low = str(text).partition("/")
    return (int(high, 16) << 32) | int(low, 16)


def format_lsn(value):
    if value is None:
        return None
    return f"{value >> 32:X}/{value & 0xFFFFFFFF:X}"


def primary_lsn():
    """The writer's current WAL position. Raises DatabaseError."""
    with connections[PRIMARY].cursor() as cursor:
        cursor.execute("SELECT pg_current_wal_lsn()::text")
        return parse_lsn(cursor.fetchone()[0])


def _probe(alias):
    """Query the replica, then the primary. Raises DatabaseError if the replica fails."""
    with connections[alias].cursor() as cursor:
        cursor.execute(
            "SELECT pg_is_in_recovery(), pg_last_wal_replay_lsn()::text,"
            " EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp())::float8"
        )
        in_recovery, replay, replay_age = cursor.fetchone()
    try:
        current = primary_lsn()
    except DatabaseError:
        current = None  # the primary's own health check reports this
    return {
        "in_recovery": in_recovery,
        "replay_lsn": parse_lsn(replay),
        "replay_age": replay_age,
        "primary_lsn": current,
    }


def _replay_lsn(alias):
    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT pg_last_wal_replay_lsn()::text")
        return parse_lsn(cursor.fetchone()[0])


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


def aliases():
    """Configured reader aliases, in settings order."""
    return [alias for alias in settings.READ_REPLICA_ALIASES if alias in connections.settings]


def configured():
    """At least one reader alias exists and read routing is enabled."""
    return settings.READ_REPLICA_ENABLED and bool(aliases())


def usable_aliases():
    return [alias for alias in aliases() if usable(alias)]


# ---------------------------------------------------------------------------
# State per alias
# ---------------------------------------------------------------------------


def _lock(alias):
    with _locks_guard:
        return _check_locks.setdefault(alias, threading.Lock())


def _current(alias):
    return _states.get(alias, ReplicaState(UNKNOWN))


def _default(alias):
    """``alias``, or the first configured reader (the only one in a one-replica setup)."""
    if alias is not None:
        return alias
    configured_aliases = aliases()
    return configured_aliases[0] if configured_aliases else None


def lag_seconds(alias):
    """Last measured lag of ``alias`` (no new check)."""
    return _current(alias).lag_seconds


def state(alias=None):
    """The alias's state, re-checked when the last check is older than the
    health interval (or the failure cooldown after an error)."""
    alias = _default(alias)
    if alias not in connections.settings or alias not in settings.READ_REPLICA_ALIASES:
        return ReplicaState(NOT_CONFIGURED)
    if not settings.READ_REPLICA_ENABLED:
        return ReplicaState(DISABLED)
    current = _current(alias)
    wait = (
        settings.REPLICA_FAILURE_COOLDOWN
        if current.status == UNAVAILABLE
        else settings.REPLICA_HEALTH_INTERVAL
    )
    if current.status == UNKNOWN or time.monotonic() - current.checked_at >= wait:
        current = check(alias)
    return current


def usable(alias=None):
    return state(alias).status == OK


def check(alias=None):
    """Probe ``alias`` now and update its state. Concurrent callers in the
    same worker don't probe it twice: they get the state as it is."""
    alias = _default(alias)
    if alias is None:
        return ReplicaState(NOT_CONFIGURED)
    lock = _lock(alias)
    if not lock.acquire(blocking=False):
        return _current(alias)
    try:
        try:
            probe = _probe(alias)
        except DatabaseError as exc:
            return _failed(alias, "health check", exc)
        new = _set(alias, _evaluate(probe))
        if probe["replay_lsn"] is not None:
            _remember_replay(alias, probe["replay_lsn"])  # same connection the reads use
        if new.status != OK:
            # Behind a reader endpoint the next connection may reach a healthy server.
            _close(alias)
        return new
    finally:
        lock.release()


def _evaluate(probe):
    now = time.monotonic()
    if not probe["in_recovery"]:
        return ReplicaState(NOT_STANDBY, now, error="the replica database is not in recovery (not a standby)")
    replay, current = probe["replay_lsn"], probe["primary_lsn"]
    lag_bytes = max(0, current - replay) if current is not None and replay is not None else None
    if lag_bytes == 0:
        lag_seconds = 0.0
    else:
        lag_seconds = max(0.0, float(probe["replay_age"])) if probe["replay_age"] is not None else None
    status = OK
    if lag_seconds is None or lag_seconds > settings.REPLICA_MAX_LAG:
        status = LAGGING
        metrics.incr("db_replica_lagging_total")
    return ReplicaState(status, now, replay, current, lag_bytes, lag_seconds)


def _set(alias, new):
    old = _current(alias)
    _states[alias] = new
    if new.status != old.status:
        if new.status == OK:
            if old.status != UNKNOWN:
                metrics.event("info", "replica_rejoined", replica=alias, previous=old.status)
        elif new.status == LAGGING:
            metrics.event(
                "warning", "replica_removed", replica=alias, reason=LAGGING,
                lag_seconds="?" if new.lag_seconds is None else round(new.lag_seconds, 2),
                max_lag_seconds=settings.REPLICA_MAX_LAG,
            )
        elif new.status == NOT_STANDBY:
            metrics.event("error", "replica_removed", replica=alias, reason=NOT_STANDBY)
    return new


def _close(alias):
    try:
        connections[alias].close()
    except DatabaseError:
        pass


def _failed(alias, operation, exc):
    """Take ``alias`` out of the pool for the failure cooldown."""
    metrics.incr("db_replica_errors_total")
    _close(alias)  # the next attempt reconnects
    # psycopg2 messages name host/port/user at most, never the password.
    message = f"{type(exc).__name__}: {str(exc).strip()}"
    if _current(alias).status != UNAVAILABLE:
        metrics.event(
            "warning", "replica_removed", replica=alias, reason=UNAVAILABLE, operation=operation,
            error=message, retry_after_seconds=settings.REPLICA_FAILURE_COOLDOWN,
        )
    return _set(alias, ReplicaState(UNAVAILABLE, time.monotonic(), error=message))


# ---------------------------------------------------------------------------
# Replay position (read-your-writes)
# ---------------------------------------------------------------------------


def _cached_replay(alias):
    """Replay LSN last seen on this thread's *current* connection for ``alias``."""
    wrapper = connections[alias]
    seen = getattr(wrapper, "_pixelforge_replay", None)
    if seen is None or wrapper.connection is None or seen[0] is not wrapper.connection:
        return None
    return seen[1]


def _remember_replay(alias, lsn):
    wrapper = connections[alias]
    if wrapper.connection is not None:
        wrapper._pixelforge_replay = (wrapper.connection, lsn)


def has_replayed(alias, lsn):
    """True once the server behind this thread's ``alias`` connection has
    replayed WAL up to ``lsn``.

    The cached position can only be older than the real one (replay moves
    forward on one server), so a cached "yes" is always right; a "no" is
    re-checked with one query on the same connection the reads will use.
    """
    cached = _cached_replay(alias)
    if cached is not None and cached >= lsn:
        return True
    try:
        replay = _replay_lsn(alias)
    except DatabaseError as exc:
        _failed(alias, "replay check", exc)
        return False
    if replay is None:
        return False
    _remember_replay(alias, replay)
    current = _current(alias)
    if current.replay_lsn is None or replay > current.replay_lsn:
        _states[alias] = replace(current, replay_lsn=replay)
    return replay >= lsn


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def _describe(alias, current):
    data = {"alias": alias, "status": current.status}
    if current.status in (OK, LAGGING):
        data.update({
            "replay_lsn": format_lsn(current.replay_lsn),
            "primary_lsn": format_lsn(current.primary_lsn),
            "lag_bytes": current.lag_bytes,
            "lag_seconds": None if current.lag_seconds is None else round(current.lag_seconds, 3),
            "max_lag_seconds": settings.REPLICA_MAX_LAG,
        })
    if current.error:
        data["error"] = current.error
    return data


def health():
    """A fresh check of every reader alias, for the health endpoints.

    ``members``: one entry per alias. The top-level fields summarise the
    pool like a single replica did before (the best member's details), so
    a one-replica setup reports exactly what it always has.
    """
    if not aliases():
        return {"status": NOT_CONFIGURED, "members": []}
    if not settings.READ_REPLICA_ENABLED:
        return {"status": DISABLED, "members": [_describe(a, ReplicaState(DISABLED)) for a in aliases()]}
    members = [_describe(alias, check(alias)) for alias in aliases()]
    healthy = [m for m in members if m["status"] == OK]
    best = min(healthy, key=lambda m: m["lag_seconds"] or 0.0) if healthy else members[0]
    summary = {key: value for key, value in best.items() if key != "alias"}
    summary["members"] = members
    summary["healthy_count"] = len(healthy)
    return summary


def reset():
    """Forget the cached state (tests)."""
    _states.clear()
