from rest_framework.exceptions import ValidationError
from rest_framework.views import exception_handler

from apps.common.custom_response import CustomResponse

# Headers DRF sets on error responses that clients rely on.
KEPT_HEADERS = ("WWW-Authenticate", "Retry-After", "Allow")


def custom_exception_handler(exc, context):
    """DRF's errors (authentication, permissions, not found, validation,
    throttling, bad JSON...) as ``CustomResponse.failed_response``.

    Validation errors keep the field errors in ``data`` and flatten them
    into ``message``.
    """
    response = exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, ValidationError):
        failed = CustomResponse.failed_response(
            response.data, data=response.data, status=response.status_code, serializer=True
        )
    else:
        detail = response.data.get("detail", response.data) if isinstance(response.data, dict) else response.data
        failed = CustomResponse.failed_response(str(detail), status=response.status_code)
    for header in KEPT_HEADERS:
        if header in response:
            failed[header] = response[header]
    return failed
