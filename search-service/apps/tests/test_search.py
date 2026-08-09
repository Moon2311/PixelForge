"""Search functionality tests.

Tests verify:
    - ProductSearch builds correct query structure
    - Filter clauses work for all filter types
    - Sort clause handles all sort options
    - Aggregation definitions are correct
    - Pagination calculates correctly
    - Autocomplete returns correct format
    - CategorySearch and BrandSearch query structure
"""

from unittest.mock import MagicMock

from django.test import TestCase

from apps.search_features import (
    ProductSearch,
    ProductAutocomplete,
    CategorySearch,
    BrandSearch,
    SORT_FIELDS,
)


# ---------------------------------------------------------------------------
# ProductSearch Tests
# ---------------------------------------------------------------------------


class ProductSearchFilterClauseTest(TestCase):
    """Test filter clause generation."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.search = ProductSearch(self.mock_client)

    def test_status_filter_active(self):
        clauses = self.search._build_filter_clauses({"status": "active"})
        self.assertEqual(clauses[0]["term"]["status"], "active")

    def test_category_id_filter(self):
        clauses = self.search._build_filter_clauses({"category_id": 5})
        self.assertEqual(clauses[1]["term"]["category.id"], 5)

    def test_category_slug_filter(self):
        clauses = self.search._build_filter_clauses({"category_slug": "phones"})
        self.assertEqual(clauses[1]["term"]["category.slug"], "phones")

    def test_brand_id_filter(self):
        clauses = self.search._build_filter_clauses({"brand_id": 3})
        self.assertEqual(clauses[1]["term"]["brand.id"], 3)

    def test_brand_slug_filter(self):
        clauses = self.search._build_filter_clauses({"brand_slug": "apple"})
        self.assertEqual(clauses[1]["term"]["brand.slug"], "apple")

    def test_price_range_filter(self):
        clauses = self.search._build_filter_clauses({
            "min_price": 100, "max_price": 500
        })
        self.assertEqual(clauses[1]["range"]["price.regular"]["gte"], 100.0)
        self.assertEqual(clauses[1]["range"]["price.regular"]["lte"], 500.0)

    def test_min_price_only_filter(self):
        clauses = self.search._build_filter_clauses({"min_price": 50})
        self.assertEqual(clauses[1]["range"]["price.regular"]["gte"], 50.0)
        self.assertNotIn("lte", clauses[1]["range"]["price.regular"])

    def test_max_price_only_filter(self):
        clauses = self.search._build_filter_clauses({"max_price": 200})
        self.assertEqual(clauses[1]["range"]["price.regular"]["lte"], 200.0)
        self.assertNotIn("gte", clauses[1]["range"]["price.regular"])

    def test_in_stock_filter(self):
        clauses = self.search._build_filter_clauses({"in_stock": True})
        self.assertTrue(clauses[1]["term"]["availability.in_stock"])

    def test_featured_filter(self):
        clauses = self.search._build_filter_clauses({"is_featured": True})
        self.assertTrue(clauses[1]["term"]["is_featured"])

    def test_min_rating_filter(self):
        clauses = self.search._build_filter_clauses({"min_rating": 4.0})
        self.assertEqual(clauses[1]["range"]["rating"]["gte"], 4.0)

    def test_attribute_filter(self):
        clauses = self.search._build_filter_clauses({
            "attributes": {"color": "red"}
        })
        self.assertEqual(clauses[1]["nested"]["path"], "variants.attribute_values")

    def test_attribute_filter_list(self):
        clauses = self.search._build_filter_clauses({
            "attributes": {"color": ["red", "blue"]}
        })
        nested = clauses[1]["nested"]
        self.assertIn("red", nested["query"]["bool"]["must"][1]["terms"]["variants.attribute_values.value"])

    def test_tag_filter(self):
        clauses = self.search._build_filter_clauses({"tags": ["sale"]})
        self.assertEqual(clauses[1]["terms"]["tags"], ["sale"])

    def test_multiple_filters(self):
        clauses = self.search._build_filter_clauses({
            "category_id": 1,
            "brand_id": 2,
            "min_price": 50,
        })
        self.assertEqual(len(clauses), 4)  # status + category + brand + price


# ---------------------------------------------------------------------------
# Sort Clause Tests
# ---------------------------------------------------------------------------


class ProductSearchSortClauseTest(TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.search = ProductSearch(self.mock_client)

    def test_default_sort_relevance(self):
        sort = self.search._build_sort_clause("-relevance")
        self.assertEqual(sort, ["_score"])

    def test_price_sort_ascending(self):
        sort = self.search._build_sort_clause("price")
        # Ascending: field without dash
        self.assertIn("price.regular", sort[0])
        self.assertEqual(sort[0]["price.regular"]["order"], "asc")

    def test_price_sort_descending(self):
        sort = self.search._build_sort_clause("-price")
        # Descending: SORT_FIELDS["-price"] = "-price.regular"
        self.assertIn("-price.regular", sort[0])
        self.assertEqual(sort[0]["-price.regular"]["order"], "desc")

    def test_name_sort(self):
        sort = self.search._build_sort_clause("name")
        self.assertEqual(sort[0]["name.keyword"]["order"], "asc")

    def test_rating_sort(self):
        sort = self.search._build_sort_clause("-rating")
        # SORT_FIELDS["-rating"] = "-rating"
        self.assertIn("-rating", sort[0])
        self.assertEqual(sort[0]["-rating"]["order"], "desc")

    def test_created_at_sort(self):
        sort = self.search._build_sort_clause("created_at")
        self.assertIn("created_at", sort[0])
        self.assertEqual(sort[0]["created_at"]["order"], "asc")

    def test_review_count_sort(self):
        sort = self.search._build_sort_clause("-review_count")
        # SORT_FIELDS["-review_count"] = "-review_count"
        self.assertIn("-review_count", sort[0])
        self.assertEqual(sort[0]["-review_count"]["order"], "desc")

    def test_unknown_sort_field(self):
        sort = self.search._build_sort_clause("unknown")
        self.assertIn("unknown", sort[0])


# ---------------------------------------------------------------------------
# Sort Field Mapping Tests
# ---------------------------------------------------------------------------


class SortFieldMappingTest(TestCase):

    def test_all_sort_fields_defined(self):
        expected_fields = [
            "name", "-name", "price", "-price", "rating", "-rating",
            "review_count", "-review_count", "created_at", "-created_at",
            "relevance", "-relevance",
        ]
        for field in expected_fields:
            self.assertIn(field, SORT_FIELDS, f"Missing sort field: {field}")


# ---------------------------------------------------------------------------
# Query Clause Tests
# ---------------------------------------------------------------------------


class ProductSearchQueryClauseTest(TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.search = ProductSearch(self.mock_client)

    def test_fuzzy_query_clause(self):
        clause = self.search._build_query_clause("iphone", fuzzy=True)
        multi_match = clause["bool"]["must"][0]["multi_match"]
        self.assertTrue(multi_match["fuzzy_transpositions"])

    def test_non_fuzzy_query_clause(self):
        clause = self.search._build_query_clause("iphone", fuzzy=False)
        multi_match = clause["bool"]["must"][0]["multi_match"]
        self.assertFalse(multi_match["fuzzy_transpositions"])

    def test_query_clause_boosts_name(self):
        clause = self.search._build_query_clause("iphone")
        fields = clause["bool"]["must"][0]["multi_match"]["fields"]
        self.assertIn("name^3", fields)

    def test_query_clause_includes_phrase_match(self):
        clause = self.search._build_query_clause("iphone")
        should = clause["bool"]["should"]
        self.assertEqual(len(should), 2)
        self.assertEqual(should[0]["match_phrase"]["name"]["boost"], 2.0)


# ---------------------------------------------------------------------------
# Aggregation Definition Tests
# ---------------------------------------------------------------------------


class ProductSearchAggregationTest(TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.search = ProductSearch(self.mock_client)

    def test_aggregations_has_categories(self):
        aggs = self.search._build_aggregations()
        self.assertIn("categories", aggs)
        self.assertEqual(aggs["categories"]["terms"]["field"], "category.id")

    def test_aggregations_has_brands(self):
        aggs = self.search._build_aggregations()
        self.assertIn("brands", aggs)
        self.assertEqual(aggs["brands"]["terms"]["field"], "brand.id")

    def test_aggregations_has_price_ranges(self):
        aggs = self.search._build_aggregations()
        self.assertIn("price_ranges", aggs)
        self.assertIn("range", aggs["price_ranges"])

    def test_aggregations_has_ratings(self):
        aggs = self.search._build_aggregations()
        self.assertIn("ratings", aggs)

    def test_aggregations_has_availability(self):
        aggs = self.search._build_aggregations()
        self.assertIn("availability", aggs)
        self.assertIn("in_stock", aggs["availability"]["filters"]["filters"])


# ---------------------------------------------------------------------------
# Pagination Tests
# ---------------------------------------------------------------------------


class ProductSearchPaginationTest(TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.search = ProductSearch(self.mock_client)
        self.mock_client.search.return_value = {
            "hits": {
                "total": {"value": 0},
                "hits": [],
            }
        }

    def test_page_1_offset_0(self):
        self.search.search(query="test", page=1, page_size=20, facets=False)
        call_body = self.mock_client.search.call_args.kwargs["body"]
        self.assertEqual(call_body["from"], 0)

    def test_page_2_offset_20(self):
        self.search.search(query="test", page=2, page_size=20, facets=False)
        call_body = self.mock_client.search.call_args.kwargs["body"]
        self.assertEqual(call_body["from"], 20)

    def test_custom_page_size(self):
        self.search.search(query="test", page=1, page_size=50, facets=False)
        call_body = self.mock_client.search.call_args.kwargs["body"]
        self.assertEqual(call_body["size"], 50)


# ---------------------------------------------------------------------------
# Autocomplete Tests
# ---------------------------------------------------------------------------


class ProductAutocompleteTest(TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.autocomplete = ProductAutocomplete(self.mock_client)
        self.mock_client.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "product_id": 1,
                            "name": "iPhone 15",
                            "slug": "iphone-15",
                            "primary_image": "img.jpg",
                        },
                        "_score": 1.0,
                    }
                ]
            }
        }

    def test_suggest_returns_list(self):
        suggestions = self.autocomplete.suggest("iph")
        self.assertEqual(len(suggestions), 1)

    def test_suggest_includes_id(self):
        suggestions = self.autocomplete.suggest("iph")
        self.assertEqual(suggestions[0]["id"], 1)

    def test_suggest_includes_name(self):
        suggestions = self.autocomplete.suggest("iph")
        self.assertEqual(suggestions[0]["name"], "iPhone 15")

    def test_suggest_empty_prefix(self):
        suggestions = self.autocomplete.suggest("")
        self.assertIsInstance(suggestions, list)

    def test_suggest_with_size_limit(self):
        self.autocomplete.suggest("ip", size=5)
        call_body = self.mock_client.search.call_args.kwargs["body"]
        self.assertEqual(call_body["size"], 5)


# ---------------------------------------------------------------------------
# CategorySearch Tests
# ---------------------------------------------------------------------------


class CategorySearchTest(TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.search = CategorySearch(self.mock_client)
        self.mock_client.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "category_id": 1,
                            "name": "Phones",
                            "slug": "phones",
                        }
                    }
                ]
            }
        }

    def test_search_with_query(self):
        results = self.search.search(query="phone")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 1)

    def test_search_without_query(self):
        results = self.search.search()
        self.assertEqual(len(results), 1)


# ---------------------------------------------------------------------------
# BrandSearch Tests
# ---------------------------------------------------------------------------


class BrandSearchTest(TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.search = BrandSearch(self.mock_client)
        self.mock_client.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "brand_id": 1,
                            "name": "Apple",
                            "slug": "apple",
                        }
                    }
                ]
            }
        }

    def test_search_with_query(self):
        results = self.search.search(query="apple")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 1)

    def test_search_without_query(self):
        results = self.search.search()
        self.assertEqual(len(results), 1)
