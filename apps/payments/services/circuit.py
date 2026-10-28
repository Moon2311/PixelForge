"""Per-provider circuit breaker for the provider APIs (``GatewayCircuit``).

The state is a PostgreSQL row per provider, so every web instance, payment
worker and the reconciler share it (Redis is a best-effort cache here and
may be down). Transitions run in short ``select_for_update`` transactions;
no transaction is open while the provider is called.

    CLOSED --(PAYMENT_CIRCUIT_FAILURE_THRESHOLD consecutive failures)--> OPEN
    OPEN --(PAYMENT_CIRCUIT_RECOVERY_SECONDS elapsed)--> HALF_OPEN
    HALF_OPEN: one probe at a time (leased for the provider timeout + 5s, so
        a probe whose process died doesn't block the circuit for ever)
    HALF_OPEN --(PAYMENT_CIRCUIT_HALF_OPEN_SUCCESSES successes)--> CLOSED
    HALF_OPEN --(probe fails)--> OPEN

Only failures meaning "the provider is unavailable" count
(``ProviderError.trips_circuit``: timeouts, connection errors, HTTP
5xx/429). A declined payment is an answer, not a failure; a bad answer or
an authentication error is an error the circuit can't help with.

Usage::

    call = circuit.before_call(provider)   # raises CircuitOpen
    try:
        result = do_the_call()
    except ProviderError as exc:
        circuit.record_failure(call, exc)
        raise
    circuit.record_success(call)
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.events.retry import sanitize_error

from .. import metrics
from ..models import GatewayCircuit
from .base import CircuitOpen


@dataclass(frozen=True)
class Call:
    provider: str
    probe: bool  # admitted as the half-open probe


def _row(provider, lock=False):
    GatewayCircuit.objects.get_or_create(provider=provider)
    query = GatewayCircuit.objects.select_for_update() if lock else GatewayCircuit.objects
    return query.get(provider=provider)


def _recovery():
    return timedelta(seconds=settings.PAYMENT_CIRCUIT_RECOVERY_SECONDS)


def _probe_lease():
    return timedelta(seconds=settings.PAYMENT_PROVIDER_TIMEOUT + 5)


def state(provider):
    """CLOSED, OPEN or HALF_OPEN, as a caller would see it now (an OPEN
    circuit whose recovery time has passed is HALF_OPEN)."""
    row = GatewayCircuit.objects.filter(provider=provider).first()
    if row is None:
        return GatewayCircuit.CLOSED
    if row.state == GatewayCircuit.OPEN and row.opened_at and timezone.now() >= row.opened_at + _recovery():
        return GatewayCircuit.HALF_OPEN
    return row.state


def is_open(provider):
    return state(provider) == GatewayCircuit.OPEN


def before_call(provider):
    """Admit a call, or raise ``CircuitOpen`` (nothing must be sent)."""
    # Fast path, no lock: the common case is a closed circuit.
    row = GatewayCircuit.objects.filter(provider=provider).first()
    if row is None or row.state == GatewayCircuit.CLOSED:
        return Call(provider, probe=False)

    now = timezone.now()
    with transaction.atomic():
        row = _row(provider, lock=True)
        if row.state == GatewayCircuit.CLOSED:
            return Call(provider, probe=False)
        if row.state == GatewayCircuit.OPEN:
            reopen_at = row.opened_at + _recovery()
            if now < reopen_at:
                metrics.incr("payment_gateway_rejected_total")
                raise CircuitOpen(f"{provider} circuit open", retry_after=(reopen_at - now).total_seconds())
            row.state = GatewayCircuit.HALF_OPEN
            row.half_open_successes = 0
            row.probe_until = None
            _log_transition(provider, GatewayCircuit.OPEN, GatewayCircuit.HALF_OPEN)
        if row.probe_until and row.probe_until > now:
            metrics.incr("payment_gateway_rejected_total")
            raise CircuitOpen(f"{provider} circuit half-open, probe in flight",
                              retry_after=(row.probe_until - now).total_seconds())
        row.probe_until = now + _probe_lease()
        row.save()
    return Call(provider, probe=True)


def record_success(call):
    row = GatewayCircuit.objects.filter(provider=call.provider).first()
    if row is None or (row.state == GatewayCircuit.CLOSED and row.consecutive_failures == 0):
        return  # nothing to change: no write on the hot path
    with transaction.atomic():
        row = _row(call.provider, lock=True)
        if row.state == GatewayCircuit.CLOSED:
            row.consecutive_failures = 0
        elif row.state == GatewayCircuit.HALF_OPEN and call.probe:
            row.half_open_successes += 1
            row.probe_until = None
            if row.half_open_successes >= settings.PAYMENT_CIRCUIT_HALF_OPEN_SUCCESSES:
                row.state = GatewayCircuit.CLOSED
                row.consecutive_failures = 0
                row.half_open_successes = 0
                row.opened_at = None
                metrics.incr("payment_circuit_closed_total")
                _log_transition(call.provider, GatewayCircuit.HALF_OPEN, GatewayCircuit.CLOSED)
        row.save()


def record_failure(call, exc):
    if not getattr(exc, "trips_circuit", False):
        if call.probe:
            # The provider answered (e.g. a bad answer): release the probe
            # without counting it either way.
            GatewayCircuit.objects.filter(provider=call.provider).update(probe_until=None)
        return
    now = timezone.now()
    with transaction.atomic():
        row = _row(call.provider, lock=True)
        row.last_failure = sanitize_error(exc, limit=255)
        row.last_failure_at = now
        previous = row.state
        if row.state == GatewayCircuit.HALF_OPEN or (
            row.state == GatewayCircuit.CLOSED
            and row.consecutive_failures + 1 >= settings.PAYMENT_CIRCUIT_FAILURE_THRESHOLD
        ):
            row.state = GatewayCircuit.OPEN
            row.opened_at = now
            row.half_open_successes = 0
            row.probe_until = None
        row.consecutive_failures += 1
        row.save()
    if previous != row.state:
        metrics.incr("payment_circuit_opened_total")
        _log_transition(call.provider, previous, row.state, failures=row.consecutive_failures,
                        error=row.last_failure)


def reset(provider):
    """Close the circuit by hand (operators, tests)."""
    GatewayCircuit.objects.update_or_create(
        provider=provider,
        defaults={"state": GatewayCircuit.CLOSED, "consecutive_failures": 0, "half_open_successes": 0,
                  "opened_at": None, "probe_until": None},
    )


def _log_transition(provider, old, new, **fields):
    level = "error" if new == GatewayCircuit.OPEN else "warning"
    metrics.event(level, "payment_circuit_transition", provider=provider, from_state=old, to_state=new, **fields)
