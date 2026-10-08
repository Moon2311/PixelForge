"""Root URL configuration: every module is mounted from this one process."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.common.views import ApiRootView, DatabaseHealthView, HealthCheckView

urlpatterns = [
    path("", ApiRootView.as_view(), name="api-root"),
    path("admin/", admin.site.urls),
    path("api/health/", HealthCheckView.as_view(), name="health-check"),
    path("api/health/database/", DatabaseHealthView.as_view(), name="database-health"),
    path("api/auth/", include("apps.authentication.urls")),
    path("api/catalog/", include("apps.catalog.urls")),
    path("api/search/", include("apps.search.urls")),
    path("api/cart/", include("apps.cart.urls")),
    path("api/orders/", include("apps.orders.urls")),
    path("api/payments/", include("apps.payments.urls")),
    # Flat product/category/brand/banner endpoints used by the storefront
    path("api/", include("apps.catalog.legacy_urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
