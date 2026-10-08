from django.db import connection
from rest_framework import status
from rest_framework.views import APIView

from apps.common.cache import client as cache_client
from apps.common.cache import metrics as cache_metrics
from apps.common.custom_response import CustomResponse
from apps.common.db import cluster, replica
from apps.common.db import metrics as db_metrics

# Replica states that still serve every request (from the primary), but slower.
_REPLICA_DEGRADED = replica.DEGRADED


def _primary_check():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as e:
        return {"status": "error", "message": str(e)}
    return {"status": "ok"}


class HealthCheckView(APIView):
    """GET /api/health/ — application, PostgreSQL and Redis health.

    PostgreSQL (the primary) down is "unhealthy" (503). Redis down, or the
    read replica unavailable or lagging, is only "degraded" (200): reads
    fall back to the primary and every request still works.
    ``cache_metrics`` are counters of the worker that answered.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        health = {"status": "healthy", "service": "pixelforge", "checks": {"django": {"status": "ok"}}}
        health["checks"]["database"] = _primary_check()
        if health["checks"]["database"]["status"] != "ok":
            health["status"] = "unhealthy"
        replica_health = replica.health()
        health["checks"]["database"]["replica"] = replica_health
        if replica_health["status"] in _REPLICA_DEGRADED and health["status"] == "healthy":
            health["status"] = "degraded"

        redis_status = cache_client.ping()
        health["checks"]["redis"] = {"status": redis_status, "cache_metrics": cache_metrics.snapshot()}
        if redis_status == "error" and health["status"] == "healthy":
            health["status"] = "degraded"

        if health["status"] == "unhealthy":
            return CustomResponse.failed_response(
                "Service unhealthy", data=health, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return CustomResponse.successful_response(health, f"Service {health['status']}")


class DatabaseHealthView(APIView):
    """GET /api/health/database/ — writer, reader pool and cluster.

    Kept from the one-replica version: ``status`` (healthy/degraded/
    unhealthy), ``primary``, ``replica`` (the reader pool summarised like a
    single replica, with every member under ``replica.members``), ``reads``
    and ``routing_metrics``.

    Cluster view: ``cluster_status`` (healthy, degraded,
    failover_in_progress, unhealthy), ``writer``/``writer_role`` (what the
    writer endpoint reaches right now), ``replicas`` (streaming from the
    writer, with lag), ``replica_count``, ``healthy_replica_count``,
    ``desired_replica_count``, ``replication_lag`` and ``failover_state``
    (the HA manager's view when FAILOVER_ENABLED). No credentials or
    connection strings. 503 only when no primary is reachable.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        primary = _primary_check()
        cluster_view = cluster.snapshot()
        replica_health = cluster_view.pop("reader_pool")
        gauges = cluster_view.pop("gauges")
        if primary["status"] != "ok" or cluster_view["writer_role"] != "primary":
            overall = "unhealthy"
        elif replica_health["status"] in _REPLICA_DEGRADED or cluster_view["cluster_status"] != "healthy":
            overall = "degraded"
        else:
            overall = "healthy"
        data = {
            "status": overall,
            **cluster_view,
            "primary": primary,
            "replica": replica_health,
            "reads": "replica" if replica_health["status"] == replica.OK else "primary",
            "routing_metrics": {**db_metrics.snapshot(), **gauges},
        }
        if overall == "unhealthy":
            return CustomResponse.failed_response(
                "No writable primary", data=data, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return CustomResponse.successful_response(data, f"Database {overall}")


class ApiRootView(APIView):
    """GET / — service info and the available API prefixes."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return CustomResponse.successful_response({
            "service": "pixelforge",
            "endpoints": {
                "health": "/api/health/",
                "database_health": "/api/health/database/",
                "auth": "/api/auth/",
                "catalog": "/api/catalog/",
                "search": "/api/search/products/?q=",
                "products": "/api/products/",
                "categories": "/api/categories/",
                "brands": "/api/brands/",
                "banners": "/api/banners/",
                "cart": "/api/cart/",
                "admin": "/admin/",
            },
        }, "PixelForge API")
