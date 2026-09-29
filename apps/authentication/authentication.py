from rest_framework.authentication import BaseAuthentication

from apps.authentication.tokens import user_from_access_token


class BearerTokenAuthentication(BaseAuthentication):
    """Authenticates ``Authorization: Bearer <access_token>`` headers.

    The token is the one returned by ``POST /api/auth/login/``. Requests
    without a Bearer header fall through to the next authenticator; an
    invalid or expired token is treated as anonymous (as before).
    """

    keyword = "Bearer"
    www_authenticate_realm = "api"

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != self.keyword.lower():
            return None
        user = user_from_access_token(parts[1])
        if user is None:
            return None
        return (user, parts[1])

    def authenticate_header(self, request):
        return f'{self.keyword} realm="{self.www_authenticate_realm}"'
