from rest_framework.response import Response

from apps.common.custom_response import CustomResponse


class CustomResponseMixin:
    """For DRF viewsets: answer the built-in actions (retrieve, create,
    update...) with ``CustomResponse`` like every other endpoint.

    Responses that already are a ``CustomResponse``, errors (handled by
    ``custom_exception_handler``) and bodiless 204s are left alone.
    """

    def finalize_response(self, request, response, *args, **kwargs):
        if (
            type(response) is Response
            and response.status_code < 400
            and response.status_code != 204
        ):
            wrapped = CustomResponse.successful_response(
                response.data,
                "Created successfully" if response.status_code == 201 else "Success",
                status=response.status_code,
            )
            for header, value in response.items():
                wrapped[header] = value
            response = wrapped
        return super().finalize_response(request, response, *args, **kwargs)
