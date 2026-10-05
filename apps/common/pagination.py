from rest_framework.pagination import PageNumberPagination

from apps.common.custom_response import CustomResponse


class CustomPagination(PageNumberPagination):
    """Page-number pagination answered as a ``CustomResponse``: the page's
    items in ``data``, the number of pages in ``page_count``, plus ``count``,
    ``next`` and ``previous``."""

    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        return CustomResponse.successful_response(
            data,
            page_count=self.page.paginator.num_pages,
            count=self.page.paginator.count,
            next=self.get_next_link(),
            previous=self.get_previous_link(),
        )
