from django.contrib.auth import authenticate, login
from django.contrib.auth.models import User
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.db.models import Q
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.views import APIView
from .auth_tokens import issue_access_token
from .responses import APIResponse
from .serializers import (
    BuyerRegistrationSerializer,
    ForgotPasswordSerializer,
    InventoryManagerCreateSerializer,
    LoginSerializer,
    ResetPasswordSerializer,
    UserSerializer,
)

token_generator = PasswordResetTokenGenerator()


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

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.get(email=serializer.validated_data["email"])
        token = token_generator.make_token(user)
        return APIResponse.success(
            data={"user_id": user.id, "token": token},
            message="Password reset token generated. Use /api/auth/reset-password/ to reset.",
        )


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            user = User.objects.get(pk=serializer.validated_data["user_id"])
        except User.DoesNotExist:
            return APIResponse.error(message="Invalid user", status_code=400)

        if not user.profile.role or user.profile.role.name != "buyer":
            return APIResponse.error(
                message="Password reset is only available for buyers", status_code=403
            )

        if not token_generator.check_token(user, serializer.validated_data["token"]):
            return APIResponse.error(
                message="Invalid or expired token", status_code=400
            )

        user.set_password(serializer.validated_data["new_password"])
        user.save()
        return APIResponse.success(message="Password reset successfully")
