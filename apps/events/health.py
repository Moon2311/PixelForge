"""Health of the asynchronous pipeline, for /api/health/ and /api/health/events/.

Reports counts, ages, lag and states only: no payloads, keys, credentials
or broker addresses. The Kafka part (metadata, group offsets, lag) is cached
per process for EVENTS_HEALTH_CACHE_SECONDS so health probes don't hammer
the brokers, and every Kafka call is bounded by KAFKA_ADMIN_TIMEOUT.
"""

import threading
import time

from confluent_kafka import ConsumerGroupTopicPartitions, TopicPartition
from confluent_kafka.admin import OffsetSpec
from django.conf import settings
from django.db import DatabaseError
from django.db.models import Count, Min, Q
from django.utils import timezone

from . import kafka, registry, topics
from .models import ConsumerHeartbeat, DeadLetterEvent, OutboxEvent

HEALTHY = "healthy"
DEGRADED = "degraded"
DISABLED = "disabled"

_cache = {"at": 0.0, "value": None}
_cache_lock = threading.Lock()


def outbox():
    now = timezone.now()
    try:
        stats = OutboxEvent.objects.aggregate(
            pending=Count("pk", filter=Q(status=OutboxEvent.STATUS_PENDING)),
            pending_forwards=Count("pk", filter=Q(status=OutboxEvent.STATUS_PENDING, kind=OutboxEvent.KIND_FORWARD)),
            retrying=Count("pk", filter=Q(status=OutboxEvent.STATUS_PENDING, attempts__gt=0)),
            failed=Count("pk", filter=Q(status=OutboxEvent.STATUS_FAILED)),
            oldest=Min("created_at", filter=Q(status=OutboxEvent.STATUS_PENDING)),
        )
    except DatabaseError as exc:
        return {"status": "error", "message": type(exc).__name__}
    oldest = stats.pop("oldest")
    stats["oldest_pending_age_seconds"] = round((now - oldest).total_seconds(), 1) if oldest else 0.0
    stats["status"] = "ok"
    return stats


def dead_letters():
    try:
        rows = (DeadLetterEvent.objects.filter(status=DeadLetterEvent.STATUS_PENDING)
                .values("consumer_group").annotate(count=Count("pk")))
        by_group = {row["consumer_group"]: row["count"] for row in rows}
    except DatabaseError as exc:
        return {"status": "error", "message": type(exc).__name__}
    return {"status": "ok", "pending": sum(by_group.values()), "pending_by_group": by_group}


def heartbeats():
    """Live/stale processes per role, with their counters summed."""
    stale_after = settings.EVENTS_HEARTBEAT_STALE_AFTER
    now = timezone.now()
    roles = {}
    try:
        rows = list(ConsumerHeartbeat.objects.all())
    except DatabaseError as exc:
        return {"status": "error", "message": type(exc).__name__}
    for row in rows:
        age = (now - row.last_seen).total_seconds()
        role = roles.setdefault(row.role, {"live": 0, "stale": 0, "members": [], "counters": {}, "tasks": {}})
        live = age <= stale_after
        role["live" if live else "stale"] += 1
        role["members"].append({
            "member": row.member, "live": live, "last_seen_seconds_ago": round(age, 1),
            "assignment": row.assignment,
        })
        if live:
            for name, value in (row.stats or {}).get("counters", {}).items():
                role["counters"][name] = role["counters"].get(name, 0) + value
            for name, value in (row.stats or {}).get("tasks", {}).items():
                merged = role["tasks"].setdefault(name, {"count": 0, "failures": 0, "max_ms": 0.0, "_total": 0.0})
                merged["count"] += value["count"]
                merged["failures"] += value["failures"]
                merged["max_ms"] = max(merged["max_ms"], value["max_ms"])
                merged["_total"] += value["avg_ms"] * value["count"]
    for role in roles.values():
        for stats in role["tasks"].values():
            total = stats.pop("_total")
            stats["avg_ms"] = round(total / stats["count"], 1) if stats["count"] else 0.0
            stats["failure_rate"] = round(stats["failures"] / stats["count"], 4) if stats["count"] else 0.0
    return {"status": "ok", "roles": roles}


def _kafka_view():
    client = kafka.admin()
    timeout = settings.KAFKA_ADMIN_TIMEOUT
    metadata = client.list_topics(timeout=timeout)
    present = set(metadata.topics)
    expected = list(settings.KAFKA_TOPICS.values())
    view = {
        "status": "ok",
        "brokers": len(metadata.brokers),
        "controller_id": getattr(metadata, "controller_id", None),
        "missing_topics": [t for t in expected if t not in present],
    }
    partitions = {
        (topic, p): None
        for topic in expected if topic in present
        for p in metadata.topics[topic].partitions
    }
    ends, starts = {}, {}
    if partitions:
        for spec, target in ((OffsetSpec.latest(), ends), (OffsetSpec.earliest(), starts)):
            futures = client.list_offsets({TopicPartition(t, p): spec for t, p in partitions},
                                          request_timeout=timeout)
            for tp, future in futures.items():
                target[(tp.topic, tp.partition)] = future.result(timeout=timeout).offset

    dlq = topics.name(topics.DEAD_LETTER_EVENTS)
    view["dlq_topic_messages"] = sum(
        max(0, ends.get(key, 0) - starts.get(key, 0)) for key in partitions if key[0] == dlq
    )

    groups = {}
    ids = {registry.group_id(name): name for name in registry.GROUPS}
    described = client.describe_consumer_groups(list(ids), request_timeout=timeout)
    # The client lists one group's offsets per request.
    offsets = {
        gid: client.list_consumer_group_offsets([ConsumerGroupTopicPartitions(gid)], request_timeout=timeout)[gid]
        for gid in ids
    }
    for gid, name in ids.items():
        group = registry.GROUPS[name]
        info = {"group_id": gid, "state": "unknown", "members": 0, "lag": 0, "lag_by_topic": {}}
        try:
            description = described[gid].result(timeout=timeout)
            info["state"] = getattr(description.state, "name", str(description.state)).lower()
            info["members"] = len(description.members)
        except Exception as exc:
            info["state"] = f"error: {type(exc).__name__}"
        committed = {}
        try:
            result = offsets[gid].result(timeout=timeout)
            committed = {(tp.topic, tp.partition): tp.offset for tp in result.topic_partitions or []}
        except Exception as exc:
            info["offsets_error"] = type(exc).__name__  # lag then counts from the oldest retained message
        subscribed = [topics.name(t) for t in group.topics]
        if group.retries:
            subscribed.append(topics.name(topics.RETRY_EVENTS))
        for key in partitions:
            if key[0] not in subscribed:
                continue
            position = committed.get(key, -1)
            # No committed offset yet: everything retained is still to do.
            position = position if position is not None and position >= 0 else starts.get(key, 0)
            lag = max(0, ends.get(key, 0) - position)
            info["lag"] += lag
            info["lag_by_topic"][key[0]] = info["lag_by_topic"].get(key[0], 0) + lag
        groups[name] = info
    view["consumer_groups"] = groups
    return view


def kafka_view(force=False):
    if not kafka.enabled():
        return {"status": DISABLED}
    now = time.monotonic()
    with _cache_lock:
        if not force and _cache["value"] is not None and now - _cache["at"] < settings.EVENTS_HEALTH_CACHE_SECONDS:
            return _cache["value"]
    try:
        value = _kafka_view()
    except Exception as exc:  # broker unreachable, timeouts
        value = {"status": "error", "message": type(exc).__name__}
    with _cache_lock:
        _cache.update(at=time.monotonic(), value=value)
    return value


def summary():
    """Compact view for /api/health/ (cheap: cached Kafka check, one query)."""
    data = {"backend": settings.EVENTS_BACKEND, "outbox": outbox()}
    if kafka.enabled():
        view = kafka_view()
        data["kafka"] = {"status": view["status"], **({"brokers": view["brokers"]} if "brokers" in view else {})}
    problems = _problems(data, None, None)
    data["status"] = DEGRADED if problems else HEALTHY
    return data


def snapshot(force=False):
    data = {
        "backend": settings.EVENTS_BACKEND,
        "outbox": outbox(),
        "dead_letters": dead_letters(),
        "workers": heartbeats(),
        "kafka": kafka_view(force=force),
    }
    data["problems"] = _problems(data, data["workers"], data["kafka"])
    data["status"] = DEGRADED if data["problems"] else HEALTHY
    return data


def _problems(data, workers, view):
    problems = []
    box = data["outbox"]
    if box.get("status") == "error":
        problems.append("outbox unreadable")
    else:
        if box["failed"]:
            problems.append(f"{box['failed']} outbox row(s) failed permanently")
        if box["oldest_pending_age_seconds"] > settings.EVENTS_OUTBOX_MAX_AGE:
            problems.append(f"outbox backlog: oldest pending row is {box['oldest_pending_age_seconds']}s old")
    kafka_status = (data.get("kafka") or {}).get("status")
    if kafka_status == "error":
        problems.append("Kafka unreachable (events wait in the outbox)")
    if view and view.get("status") == "ok":
        if view["missing_topics"]:
            problems.append(f"missing topics: {', '.join(view['missing_topics'])}")
        for name, group in view["consumer_groups"].items():
            if group["lag"] > settings.EVENTS_MAX_CONSUMER_LAG:
                problems.append(f"{name}: consumer lag {group['lag']}")
    if workers and workers.get("status") == "ok" and kafka.enabled():
        roles = workers["roles"]
        for name in registry.GROUPS:
            gid = registry.group_id(name)
            if not roles.get(gid, {}).get("live"):
                problems.append(f"{name}: no live worker")
        if not roles.get("outbox-publisher", {}).get("live"):
            problems.append("no live outbox publisher")
    dead = data.get("dead_letters") or {}
    if dead.get("pending"):
        problems.append(f"{dead['pending']} dead letter(s) awaiting review")
    return problems


def reset():
    with _cache_lock:
        _cache.update(at=0.0, value=None)
