from django.db import connection
from rest_framework import status
from rest_framework.views import APIView

from apps.common.cache import client as cache_client, metrics as cache_metrics
from apps.common.custom_response import CustomResponse


class HealthCheckView(APIView):
    """GET /api/health/ — application, PostgreSQL and Redis health.

    PostgreSQL down is "unhealthy" (503). Redis down is only "degraded"
    (200): it is a cache, and every request still works without it.
    ``cache_metrics`` are counters of the worker that answered.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        health = {"status": "healthy", "service": "pixelforge", "checks": {"django": {"status": "ok"}}}
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            health["checks"]["database"] = {"status": "ok"}
        except Exception as e:
            health["status"] = "unhealthy"
            health["checks"]["database"] = {"status": "error", "message": str(e)}

        redis_status = cache_client.ping()
        health["checks"]["redis"] = {"status": redis_status, "cache_metrics": cache_metrics.snapshot()}
        if redis_status == "error" and health["status"] == "healthy":
            health["status"] = "degraded"

        if health["status"] == "unhealthy":
            return CustomResponse.failed_response(
                "Service unhealthy", data=health, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return CustomResponse.successful_response(health, f"Service {health['status']}")


class ApiRootView(APIView):
    """GET / — service info and the available API prefixes."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return CustomResponse.successful_response({
            "service": "pixelforge",
            "endpoints": {
                "health": "/api/health/",
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
