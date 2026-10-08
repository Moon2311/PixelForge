"""Read-only view of the database cluster for health checks and logs.

Django never changes the cluster: promotion, rejoin and replacement belong
to the HA layer (Patroni + etcd + HAProxy locally, or a managed service).
This module only observes:

* the **writer** through the writer endpoint (``default``): is it really a
  primary, its timeline and WAL position, and which server answered;
* the **replicas the writer streams to** (``pg_stat_replication``), which
  covers every replica behind a load-balanced reader endpoint (needs the
  ``pg_monitor`` role to see LSNs and lag);
* optionally the **HA manager's own view** (``DB_HA_STATUS_URL``, a Patroni
  REST API): leader, members, maintenance mode and failover history.

Nothing here returns credentials, connection strings or passwords: only
node names, server addresses, roles, states, LSNs and lag.
"""

import json
import urllib.error
import urllib.request

from django.conf import settings
from django.db import DatabaseError, connections
from django.db.backends.signals import connection_created
from django.dispatch import receiver

from apps.common.db import PRIMARY, metrics, replica

_HA_TIMEOUT = 2.0
_last_writer = {}


def _fetch(sql):
    with connections[PRIMARY].cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def writer_info():
    """Role, timeline and WAL position of whatever the writer endpoint reaches."""
    try:
        (in_recovery, lsn, server), = _fetch(
            "SELECT pg_is_in_recovery(),"
            " CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_current_wal_lsn()::text END,"
            " host(inet_server_addr())"
        )
    except DatabaseError as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {str(exc).strip()}"}
    info = {
        "status": "ok" if not in_recovery else "read_only",
        # A writer endpoint that reaches a standby is mid-failover or misrouted.
        "role": "replica" if in_recovery else "primary",
        "wal_lsn": lsn,
        "server": server,
    }
    try:
        info["timeline"] = _fetch("SELECT timeline_id FROM pg_control_checkpoint()")[0][0]
    except DatabaseError:
        info["timeline"] = None  # needs pg_monitor / superuser
    return info


def streaming_replicas():
    """Replicas connected to the writer, from ``pg_stat_replication``."""
    try:
        rows = _fetch(
            "SELECT application_name, state, sync_state, replay_lsn::text,"
            " pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint,"
            " EXTRACT(EPOCH FROM replay_lag)::float8"
            " FROM pg_stat_replication ORDER BY application_name"
        )
    except DatabaseError:
        return None
    replicas = []
    for name, state, sync_state, replay_lsn, lag_bytes, lag_seconds in rows:
        if lag_seconds is None and lag_bytes == 0:
            lag_seconds = 0.0  # caught up and idle: PostgreSQL reports no lag time
        healthy = state == "streaming" and lag_seconds is not None and lag_seconds <= settings.REPLICA_MAX_LAG
        replicas.append({
            "name": name,
            "state": state,
            "sync_state": sync_state,
            "replay_lsn": replay_lsn,
            "lag_bytes": lag_bytes,
            "lag_seconds": None if lag_seconds is None else round(lag_seconds, 3),
            "healthy": healthy,
        })
    return replicas


def _get_json(path):
    url = settings.DB_HA_STATUS_URL.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=_HA_TIMEOUT) as response:
        return json.load(response)


def failover_state():
    """The HA manager's view (Patroni REST API), or why it isn't available."""
    if not settings.FAILOVER_ENABLED:
        return {"enabled": False}
    if not settings.DB_HA_STATUS_URL:
        return {"enabled": True, "state": "unknown", "error": "DB_HA_STATUS_URL is not set"}
    try:
        cluster = _get_json("/cluster")
        history = _get_json("/history")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"enabled": True, "state": "unknown", "error": f"HA status unavailable: {type(exc).__name__}"}
    members = cluster.get("members", [])
    leader = next((m for m in members if m.get("role") == "leader" and m.get("state") == "running"), None)
    if cluster.get("pause"):
        current = "paused"  # maintenance mode: no automatic failover
    elif leader is None:
        current = "no_leader"  # primary lost; election/promotion in progress
    else:
        current = "stable"
    last = history[-1] if history else None
    return {
        "enabled": True,
        "state": current,
        "leader": leader.get("name") if leader else None,
        "timeline": leader.get("timeline") if leader else None,
        "members": [
            {key: m.get(key) for key in ("name", "role", "state", "timeline", "lag")}
            for m in members
        ],
        "promotions": len(history),
        "last_promotion": {"timeline": last[0] + 1, "at": last[3] if len(last) > 3 else None,
                           "new_leader": last[4] if len(last) > 4 else None} if last else None,
    }


def snapshot():
    """Everything /api/health/database/ reports about the cluster."""
    writer = writer_info()
    pool = replica.health()
    streaming = streaming_replicas() if writer.get("role") == "primary" else None
    ha = failover_state()
    if ha.get("leader"):
        writer["node"] = ha["leader"]

    if streaming is not None:
        replica_count = len(streaming)
        healthy_count = sum(1 for r in streaming if r["healthy"])
    else:  # no pg_monitor, or the writer isn't reachable: the reader pool is all we know
        replica_count = len(pool["members"])
        healthy_count = pool.get("healthy_count", 0)

    if writer["status"] == "unavailable":
        cluster_status = "failover_in_progress" if ha.get("state") == "no_leader" else "unhealthy"
    elif writer["role"] != "primary":
        cluster_status = "failover_in_progress"
    elif healthy_count < settings.DB_DESIRED_REPLICA_COUNT or pool["status"] in replica.DEGRADED:
        cluster_status = "degraded"
    else:
        cluster_status = "healthy"

    return {
        "cluster_status": cluster_status,
        "writer": writer,
        "writer_role": writer.get("role"),
        "replicas": streaming,
        "replica_count": replica_count,
        "healthy_replica_count": healthy_count,
        "desired_replica_count": settings.DB_DESIRED_REPLICA_COUNT,
        "replication_lag": {r["name"]: r["lag_seconds"] for r in streaming or []},
        "reader_pool": pool,
        "failover_state": ha,
        "gauges": {
            "db_replica_healthy": healthy_count,
            "db_replica_lag": {m["alias"]: m.get("lag_seconds") for m in pool["members"]},
            "db_promotion_total": ha.get("promotions"),
        },
    }


@receiver(connection_created)
def _watch_writer(sender, connection, **kwargs):
    """In HA mode, notice (per worker) when a new writer connection reaches a
    different server than the last one: the endpoint followed a failover."""
    if connection.alias != PRIMARY or not settings.FAILOVER_ENABLED:
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT host(inet_server_addr()), pg_is_in_recovery()")
            server, in_recovery = cursor.fetchone()
    except DatabaseError:
        return
    previous = _last_writer.get("server")
    _last_writer["server"] = server
    if previous is not None and previous != server:
        metrics.incr("db_failover_total")
        metrics.event("warning", "writer_endpoint_changed", previous_server=previous, server=server,
                      role="replica" if in_recovery else "primary")
