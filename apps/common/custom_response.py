from typing import Any, Generic, Literal, TypeVar, overload

from rest_framework.response import Response

from apps.common.constants import BAD_REQUEST_CODE, SUCCESS_RESPONSE_CODE, failure, success
from apps.common.utils import get_all_error_messages

T = TypeVar("T")


class CustomResponse(Response, Generic[T]):
    """Custom response wrapper with type safety and standardized structure.

    This class extends DRF's Response to add:
    1. Generic type parameter support for static type checking
    2. Helper methods for creating successful/failed responses with standard structure
    """

    @overload
    @classmethod
    def successful_response(
        cls,
        data: T,
        message: str = "",
        status: int = SUCCESS_RESPONSE_CODE,
        page_count: int = 1,
        **kwargs: Any,
    ) -> "CustomResponse[T]": ...

    @overload
    @classmethod
    def successful_response(
        cls,
        *,
        message: str = "",
        status: int = SUCCESS_RESPONSE_CODE,
        page_count: int = 1,
        **kwargs: Any,
    ) -> "CustomResponse[None]": ...

    @classmethod
    def successful_response(
        cls,
        data: Any = None,
        message: str = "",
        status: int = SUCCESS_RESPONSE_CODE,
        page_count: int = 1,
        **kwargs: Any,
    ) -> Any:
        """Create a successful response with standardized structure.
        Extra keyword arguments are added to the response body as-is.
        """
        response_: dict[str, Any] = {
            "message": message,
            "status": status,
            "is_success": True,
            "messageTypeId": success,
            "data": data,
            "page_count": page_count,
            **kwargs,
        }
        return cls(response_, status=status)

    @overload
    @classmethod
    def failed_response(
        cls,
        message: str = "",
        *,
        data: T,
        status: int = BAD_REQUEST_CODE,
        serializer: bool = False,
        **kwargs: Any,
    ) -> "CustomResponse[T]": ...

    @overload
    @classmethod
    def failed_response(
        cls,
        message: str = "",
        *,
        status: int = BAD_REQUEST_CODE,
        serializer: bool = False,
        **kwargs: Any,
    ) -> "CustomResponse[Any]": ...

    @overload
    @classmethod
    def failed_response(
        cls,
        message: dict[str, Any],
        *,
        status: int = BAD_REQUEST_CODE,
        serializer: Literal[True],
        **kwargs: Any,
    ) -> "CustomResponse[Any]": ...

    @classmethod
    def failed_response(
        cls,
        message: str | dict[str, Any] = "",
        data: Any = None,
        status: int = BAD_REQUEST_CODE,
        serializer: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Create a failed response with standardized structure.
        With ``serializer=True``, ``message`` is a serializer's ``errors`` and
        is flattened into one readable message.
        """
        if not data:
            data = {"response": []}
        response_: dict[str, Any] = {
            "message": get_all_error_messages(message) if serializer else message,
            "status": status,
            "is_success": False,
            "messageTypeId": failure,
            "data": data,
        }
        return cls(response_, status=status)
