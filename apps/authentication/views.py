from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db.models import Q
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from apps.common.responses import APIResponse

from .tokens import issue_access_token
from .emails import send_password_changed_email, send_password_reset_email
from .serializers import (
    BuyerRegistrationSerializer,
    ForgotPasswordSerializer,
    InventoryManagerCreateSerializer,
    LoginSerializer,
    ResetPasswordSerializer,
    UserSerializer,
    get_password_reset_users,
)


class RegisterBuyerView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = BuyerRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return APIResponse.created(
            data=UserSerializer(user).data, message="Buyer registered successfully"
        )


class CreateInventoryManagerView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        serializer = InventoryManagerCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return APIResponse.created(
            data=UserSerializer(user).data,
            message="Inventory manager created successfully",
        )


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        identifier = serializer.validated_data["username"]
        password = serializer.validated_data["password"]

        user = None
        try:
            account = User.objects.get(Q(username=identifier) | Q(email=identifier))
        except User.DoesNotExist:
            account = None
        if account is not None:
            user = authenticate(
                request, username=account.username, password=password
            )
        if user is None:
            return APIResponse.error(
                message="Invalid username or password", status_code=401
            )
        login(request, user)
        return APIResponse.success(
            data={
                "user": UserSerializer(user).data,
                "access_token": issue_access_token(user),
            },
            message="Login successful",
        )


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        for user in get_password_reset_users(serializer.validated_data["email"]):
            send_password_reset_email(user)
        # Same response whether or not the email matched an account.
        return APIResponse.success(
            message="If an account exists for this email, a password reset link has been sent."
        )


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        send_password_changed_email(user)
        return APIResponse.success(message="Password reset successfully")


class LogoutView(APIView):
    """POST /api/auth/logout/ — end the Django session, if any.

    Access tokens are stateless and expire on their own
    (``ACCESS_TOKEN_MAX_AGE``); clients drop the token on logout.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        logout(request)
        return APIResponse.success(message="Logged out")
