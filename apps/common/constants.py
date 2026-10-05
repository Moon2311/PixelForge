"""Values shared by every module's API responses."""

SUCCESS_RESPONSE_CODE = 200
BAD_REQUEST_CODE = 400

# ``messageTypeId`` in CustomResponse bodies: lets the frontend style the
# message (e.g. a success or error toast) without reading ``is_success``.
success = 1
failure = 2
