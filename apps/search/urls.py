from django.urls import path

from apps.search.views import (
    SearchAutocompleteView,
    SearchBrandsView,
    SearchCategoriesView,
    SearchProductsView,
)

urlpatterns = [
    path("products/", SearchProductsView.as_view(), name="search-products"),
    path("autocomplete/", SearchAutocompleteView.as_view(), name="search-autocomplete"),
    path("categories/", SearchCategoriesView.as_view(), name="search-categories"),
    path("brands/", SearchBrandsView.as_view(), name="search-brands"),
]
