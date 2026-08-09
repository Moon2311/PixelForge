from django.urls import include, path

from apps.views import (
    BannerDetailView,
    BannersView,
    BrandDetailView,
    BrandsView,
    CategoryDetailView,
    CategoriesView,
    HealthCheckView,
    InventoryLogsView,
    ListProductsView,
    LowStockAlertResolveView,
    LowStockAlertsView,
    ProductDetailView,
    ProductMetaView,
    RecentlyViewedView,
    RecommendedProductsView,
    StockHistoryView,
    StockUpdateView,
)
from apps.search_views import (
    SearchAutocompleteView,
    SearchBrandsView,
    SearchCategoriesView,
    SearchProductsView,
)

urlpatterns = [
    path("health/", HealthCheckView.as_view(), name="health-check"),

    # Catalog API (PostgreSQL-backed)
    path("catalog/", include("apps.catalog_urls")),

    # Search API (Elasticsearch-backed)
    path("search/products/", SearchProductsView.as_view(), name="search-products"),
    path("search/autocomplete/", SearchAutocompleteView.as_view(), name="search-autocomplete"),
    path("search/categories/", SearchCategoriesView.as_view(), name="search-categories"),
    path("search/brands/", SearchBrandsView.as_view(), name="search-brands"),

    # Legacy ES-backed endpoints (kept for backward compatibility)
    path("products/", ListProductsView.as_view(), name="list-products"),
    path("products/meta/", ProductMetaView.as_view(), name="products-meta"),
    path("products/recommended/", RecommendedProductsView.as_view(), name="products-recommended"),
    path("products/recently-viewed/", RecentlyViewedView.as_view(), name="products-recently-viewed"),
    path("products/inventory/logs/", InventoryLogsView.as_view(), name="inventory-logs"),
    path(
        "products/inventory/low-stock/",
        LowStockAlertsView.as_view(),
        name="low-stock-alerts",
    ),
    path(
        "products/inventory/low-stock/<int:alert_id>/resolve/",
        LowStockAlertResolveView.as_view(),
        name="low-stock-resolve",
    ),
    path("products/<int:product_id>/stock/history/", StockHistoryView.as_view(), name="stock-history"),
    path("products/<int:product_id>/stock/", StockUpdateView.as_view(), name="stock-update"),
    path("products/<int:product_id>/", ProductDetailView.as_view(), name="product-detail"),
    path("categories/", CategoriesView.as_view(), name="list-categories"),
    path("categories/<int:doc_id>/", CategoryDetailView.as_view(), name="category-detail"),
    path("brands/", BrandsView.as_view(), name="list-brands"),
    path("brands/<int:doc_id>/", BrandDetailView.as_view(), name="brand-detail"),
    path("banners/", BannersView.as_view(), name="list-banners"),
    path("banners/<int:doc_id>/", BannerDetailView.as_view(), name="banner-detail"),
]
