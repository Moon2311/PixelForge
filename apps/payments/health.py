"""Payment pipeline health, for /api/health/payments/.

Counts, ages and states only. The payments consumer group's Kafka lag and
the outbox backlog are in /api/health/events/ (shared pipeline).
"""

from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError
from django.db.models import Count, F, Min, Q, Sum
from django.utils import timezone

from apps.events import health as events_health
from apps.events import registry

from .models import GatewayCircuit, Payment, PaymentNotification
from .services import circuit

HEALTHY = events_health.HEALTHY
DEGRADED = events_health.DEGRADED
RECONCILER_ROLE = "payments-reconciler"


def payments():
    now = timezone.now()
    cutoff = now - timedelta(seconds=settings.PAYMENT_RECONCILE_AFTER)
    open_q = Q(status__in=Payment.ACTIVE_STATUSES)
    try:
        by_status = dict(
            Payment.objects.filter(open_q | Q(updated_at__gte=now - timedelta(hours=24)))
            .values_list("status").annotate(n=Count("pk"))
        )
        stats = Payment.objects.aggregate(
            unknown=Count("pk", filter=Q(status=Payment.STATUS_UNKNOWN)),
            oldest_unknown=Min("unknown_since", filter=Q(status=Payment.STATUS_UNKNOWN)),
            escalated=Count("pk", filter=Q(status=Payment.STATUS_UNKNOWN, escalated_at__isnull=False)),
            awaiting_reconciliation=Count("pk", filter=Q(
                status__in=[Payment.STATUS_PROCESSING, Payment.STATUS_UNKNOWN],
            ) & (Q(last_checked_at__lte=cutoff) | Q(last_checked_at__isnull=True, created_at__lte=cutoff))),
            needs_review=Count("pk", filter=Q(metadata__needs_review=True,
                                              updated_at__gte=now - timedelta(days=7))),
            needs_refund=Count("pk", filter=Q(metadata__needs_refund=True,
                                              updated_at__gte=now - timedelta(days=7))),
        )
        duplicates = PaymentNotification.objects.filter(
            last_received_at__gte=now - timedelta(hours=24),
        ).aggregate(n=Sum(F("receive_count") - 1))["n"] or 0
    except DatabaseError as exc:
        return {"status": "error", "message": type(exc).__name__}
    oldest = stats.pop("oldest_unknown")
    return {
        "status": "ok",
        # Open payments, plus payments that changed in the last 24 hours.
        "by_status": by_status,
        **stats,
        "oldest_unknown_age_seconds": round((now - oldest).total_seconds(), 1) if oldest else 0.0,
        "duplicate_notifications_24h": duplicates,
    }


def circuits():
    try:
        rows = {row.provider: row for row in GatewayCircuit.objects.all()}
    except DatabaseError as exc:
        return {"status": "error", "message": type(exc).__name__}
    result = {}
    for method, _ in Payment.METHOD_CHOICES:
        row = rows.get(method)
        result[method] = {
            "state": circuit.state(method),
            "consecutive_failures": row.consecutive_failures if row else 0,
            "opened_at": row.opened_at.isoformat() if row and row.opened_at else None,
            "last_failure_at": row.last_failure_at.isoformat() if row and row.last_failure_at else None,
        }
    return {"status": "ok", "providers": result}


def snapshot():
    workers = events_health.heartbeats()
    roles = workers.get("roles", {}) if workers.get("status") == "ok" else {}
    worker = roles.get(registry.group_id("payments"), {})
    reconciler = roles.get(RECONCILER_ROLE, {})
    data = {
        "payments": payments(),
        "circuits": circuits(),
        "worker": {"live": worker.get("live", 0), "stale": worker.get("stale", 0),
                   "counters": {k: v for k, v in worker.get("counters", {}).items() if k.startswith("payment")},
                   "tasks": worker.get("tasks", {})},
        "reconciler": {"live": reconciler.get("live", 0), "stale": reconciler.get("stale", 0),
                       "counters": {k: v for k, v in reconciler.get("counters", {}).items()
                                    if k.startswith("payment")}},
    }
    data["problems"] = _problems(data)
    data["status"] = DEGRADED if data["problems"] else HEALTHY
    return data


def _problems(data):
    problems = []
    pay = data["payments"]
    if pay.get("status") == "error":
        problems.append("payments unreadable")
    else:
        if pay["unknown"]:
            problems.append(f"{pay['unknown']} payment(s) UNKNOWN awaiting confirmation "
                            f"(oldest {pay['oldest_unknown_age_seconds']}s)")
        if pay["escalated"]:
            problems.append(f"{pay['escalated']} UNKNOWN payment(s) escalated for manual review")
    for method, info in (data["circuits"].get("providers") or {}).items():
        if info["state"] != GatewayCircuit.CLOSED:
            problems.append(f"{method} circuit {info['state']}")
    from apps.events import kafka

    if kafka.enabled():
        if not data["worker"]["live"]:
            problems.append("payments: no live worker")
        if not data["reconciler"]["live"]:
            problems.append("no live payments reconciler")
    return problems
