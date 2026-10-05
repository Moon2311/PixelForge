from django.db import connection
from rest_framework import status
from rest_framework.views import APIView

from apps.common.custom_response import CustomResponse


class HealthCheckView(APIView):
    """GET /api/health/ — application and PostgreSQL health."""

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

        if health["status"] != "healthy":
            return CustomResponse.failed_response(
                "Service unhealthy", data=health, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return CustomResponse.successful_response(health, "Service healthy")


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
