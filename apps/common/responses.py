from rest_framework.response import Response
from rest_framework import status as http_status


class APIResponse:
    @staticmethod
    def success(data=None, message="Success", status_code=http_status.HTTP_200_OK):
        return Response(
            {"message": message, "status_code": status_code, "data": data},
            status=status_code,
        )

    @staticmethod
    def created(data=None, message="Created successfully"):
        return APIResponse.success(data, message, http_status.HTTP_201_CREATED)

    @staticmethod
    def error(message="Error", status_code=http_status.HTTP_400_BAD_REQUEST, data=None):
        return Response(
            {"message": message, "status_code": status_code, "data": data},
            status=status_code,
        )

    @staticmethod
    def bad_request(message="Bad request", data=None):
        return APIResponse.error(message, http_status.HTTP_400_BAD_REQUEST, data)

    @staticmethod
    def not_found(message="Not found", data=None):
        return APIResponse.error(message, http_status.HTTP_404_NOT_FOUND, data)

    @staticmethod
    def forbidden(message="Forbidden", data=None):
        return APIResponse.error(message, http_status.HTTP_403_FORBIDDEN, data)

    @staticmethod
    def server_error(message="Internal server error", data=None):
        return APIResponse.error(
            message, http_status.HTTP_500_INTERNAL_SERVER_ERROR, data
        )
