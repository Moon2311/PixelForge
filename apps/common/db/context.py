"""Per-request routing state and the read decision (writer or which replica).

A replica-eligible read (see ``router``) goes to a replica only when all
of these hold, otherwise to the primary (the writer):

1. at least one replica is configured and enabled;
2. the primary connection is not inside a transaction (reads in
   ``atomic()``, including ``select_for_update``, must see the
   transaction's own writes and lock rows on the primary);
3. it runs inside a request (management commands and shells read the
   primary) whose method is safe (GET/HEAD/OPTIONS): a POST/PUT/PATCH/DELETE
   validates against the data it is about to change;
4. no ``use_primary()`` block is active;
5. a replica is healthy and within ``REPLICA_MAX_LAG`` (the reader pool);
6. that replica has replayed the caller's last committed write, and any
   LSN a ``require_lsn()`` block asks for (catalog cache refills). If that
   LSN can't be known (Redis down), the primary.

Reader endpoint: among the usable replicas one is picked per request and
kept for the rest of it (a request reads from one server). The pick is
random, weighted towards low lag (weight ``1 / (1 + lag_seconds)``), so
load spreads over the pool and a slower replica gets less of it. A caller
with a pending write gets the first replica in that order that has
replayed it; only if none has does it read the primary.
"""

import contextlib
import contextvars
import random
from dataclasses import dataclass

from django.db import connections

from apps.common.db import PRIMARY, consistency, metrics, replica

_UNSET = object()


@dataclass
class RequestState:
    pinned: bool = False  # every read on the primary (unsafe method or a retry)
    identity: tuple[str, str] | None = None  # read-your-writes key (consistency.request_key)
    used_replica: str | None = None  # the replica alias this request read from
    wrote: bool = False  # a write to the primary committed during the request
    _required_lsn: object = _UNSET

    def note_write(self):
        self.wrote = True

    def required_lsn(self):
        """LSN this caller must see: 0 = none, None = unknown. Looked up once per request."""
        if self._required_lsn is _UNSET:
            self._required_lsn = consistency.recent_write_lsn(self.identity) if self.identity else 0
        return self._required_lsn


_request = contextvars.ContextVar("db_request_state", default=None)
_min_lsn = contextvars.ContextVar("db_min_lsn", default=0)
_force_primary = contextvars.ContextVar("db_force_primary", default=False)


def current():
    return _request.get()


@contextlib.contextmanager
def request_scope(state):
    token = _request.set(state)
    try:
        yield state
    finally:
        _request.reset(token)


@contextlib.contextmanager
def use_primary():
    """Send every read in the block to the primary."""
    token = _force_primary.set(True)
    try:
        yield
    finally:
        _force_primary.reset(token)


@contextlib.contextmanager
def require_lsn(lsn):
    """Reads in the block may use the replica only once it has replayed
    ``lsn`` (int; 0 = no requirement; None = unknown, i.e. primary)."""
    outer = _min_lsn.get()
    combined = None if lsn is None or outer is None else max(lsn, outer)
    token = _min_lsn.set(combined)
    try:
        yield
    finally:
        _min_lsn.reset(token)


def _weight(alias):
    lag = replica.lag_seconds(alias) or 0.0
    return 1.0 / (1.0 + lag)


def _candidates(state):
    """Usable replicas, this request's replica first, then a lag-weighted
    random order (Efraimidis-Spirakis: key = random() ** (1 / weight))."""
    usable = replica.usable_aliases()
    ordered = sorted(usable, key=lambda alias: -(random.random() ** (1.0 / _weight(alias))))
    if state.used_replica in ordered:
        ordered.remove(state.used_replica)
        ordered.insert(0, state.used_replica)
    return ordered


def _decide():
    if not replica.configured() or connections[PRIMARY].in_atomic_block:
        return PRIMARY
    state = _request.get()
    if state is None or state.pinned or _force_primary.get():
        return PRIMARY
    required, extra = state.required_lsn(), _min_lsn.get()
    if required is None or extra is None:
        return PRIMARY
    required = max(required, extra)
    candidates = _candidates(state)
    if not candidates:
        return PRIMARY
    for alias in candidates:
        if not required or replica.has_replayed(alias, required):
            state.used_replica = alias
            return alias
    metrics.incr("db_read_your_writes_primary_total")
    return PRIMARY


def choose_read_db():
    db = _decide()
    if replica.configured():
        metrics.incr("db_primary_reads_total" if db == PRIMARY else "db_replica_reads_total")
        metrics.incr("db_reader_routing_total", label="primary" if db == PRIMARY else db)
    return db
