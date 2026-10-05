"""Search API views.

Provides dedicated search endpoints for the product catalog.
These endpoints query the PostgreSQL catalog directly (see
apps/search_features.py).

All endpoints are read-only and public-facing.
"""

import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.common.custom_response import CustomResponse

from apps.search.services import (
    CategorySearch,
    ProductAutocomplete,
    ProductSearch,
    BrandSearch,
)

logger = logging.getLogger("catalog.search")


class SearchProductsView(APIView):
    """Full-featured product search endpoint.

    GET /api/search/products/

    Supports:
    - Keyword search across name, SKU, descriptions, brand, category,
      specifications and tags
    - Category, brand, subcategory filtering
    - Price range filtering
    - Attribute filtering
    - Availability filtering
    - Rating filtering
    - Sorting (relevance, price, rating, etc.)
    - Pagination
    - Facets/aggregations
    """

    permission_classes = [AllowAny]

    def get(self, request):
        query = request.query_params.get("q") or request.query_params.get("search")

        # Build filters from query params
        filters = {}

        # Category filters
        category_id = request.query_params.get("category_id")
        if category_id:
            filters["category_id"] = int(category_id)

        category_slug = request.query_params.get("category")
        if category_slug:
            filters["category_slug"] = category_slug

        # Subcategory filters
        subcategory_id = request.query_params.get("subcategory_id")
        if subcategory_id:
            filters["subcategory_id"] = int(subcategory_id)

        subcategory_slug = request.query_params.get("subcategory")
        if subcategory_slug:
            filters["subcategory_slug"] = subcategory_slug

        # Brand filters
        brand_id = request.query_params.get("brand_id")
        if brand_id:
            filters["brand_id"] = int(brand_id)

        brand_slug = request.query_params.get("brand")
        if brand_slug:
            filters["brand_slug"] = brand_slug

        # Price range
        min_price = request.query_params.get("min_price")
        if min_price:
            try:
                filters["min_price"] = float(min_price)
            except (ValueError, TypeError):
                pass

        max_price = request.query_params.get("max_price")
        if max_price:
            try:
                filters["max_price"] = float(max_price)
            except (ValueError, TypeError):
                pass

        # Availability
        in_stock = request.query_params.get("in_stock")
        if in_stock is not None:
            filters["in_stock"] = in_stock.lower() in ("true", "1", "yes")

        # Featured
        is_featured = request.query_params.get("is_featured")
        if is_featured is not None:
            filters["is_featured"] = is_featured.lower() in ("true", "1", "yes")

        # Rating
        min_rating = request.query_params.get("min_rating")
        if min_rating:
            try:
                filters["min_rating"] = float(min_rating)
            except (ValueError, TypeError):
                pass

        # Sort
        sort = request.query_params.get("ordering", "-relevance")

        # Pagination
        try:
            page = int(request.query_params.get("page", 1))
        except (ValueError, TypeError):
            page = 1

        try:
            page_size = int(request.query_params.get("page_size", 20))
        except (ValueError, TypeError):
            page_size = 20

        # Facets
        facets = request.query_params.get("facets", "true").lower() in ("true", "1", "yes")

        # Execute search
        try:
            search = ProductSearch()

            result = search.search(
                query=query,
                filters=filters,
                sort=sort,
                page=page,
                page_size=page_size,
                facets=facets,
            )

            return CustomResponse.successful_response(result)
        except Exception as e:
            logger.error(f"Search error: {e}", exc_info=True)
            return CustomResponse.failed_response(
                "Search error", data={"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class SearchAutocompleteView(APIView):
    """Autocomplete suggestions for products.

    GET /api/search/autocomplete/

    Returns product name suggestions based on prefix.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        prefix = request.query_params.get("q") or request.query_params.get("prefix", "")

        if not prefix or len(prefix) < 2:
            return CustomResponse.successful_response({"suggestions": []})

        try:
            autocomplete = ProductAutocomplete()

            suggestions = autocomplete.suggest(prefix)

            return CustomResponse.successful_response({"suggestions": suggestions})
        except Exception as e:
            logger.error(f"Autocomplete error: {e}", exc_info=True)
            return CustomResponse.failed_response(
                "Autocomplete error", data={"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class SearchCategoriesView(APIView):
    """Search categories.

    GET /api/search/categories/

    Returns filtered categories with product counts.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        query = request.query_params.get("q")

        try:
            search = CategorySearch()

            categories = search.search(query)

            return CustomResponse.successful_response({"categories": categories})
        except Exception as e:
            logger.error(f"Category search error: {e}", exc_info=True)
            return CustomResponse.failed_response(
                "Search error", data={"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class SearchBrandsView(APIView):
    """Search brands.

    GET /api/search/brands/

    Returns filtered brands with product counts.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        query = request.query_params.get("q")

        try:
            search = BrandSearch()

            brands = search.search(query)

            return CustomResponse.successful_response({"brands": brands})
        except Exception as e:
            logger.error(f"Brand search error: {e}", exc_info=True)
            return CustomResponse.failed_response(
                "Search error", data={"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
