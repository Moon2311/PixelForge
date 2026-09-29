from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers
from .models import Role, UserProfile


class BuyerRegistrationSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=6)
    confirm_password = serializers.CharField(write_only=True)

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError("Username already taken")
        return value

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("Email already registered")
        return value

    def validate(self, data):
        if data["password"] != data["confirm_password"]:
            raise serializers.ValidationError(
                {"confirm_password": "Passwords do not match"}
            )
        return data

    def create(self, validated_data):
        validated_data.pop("confirm_password")
        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data["email"],
            password=validated_data["password"],
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
        )
        buyer_role = Role.objects.get(name="buyer")
        UserProfile.objects.create(user=user, role=buyer_role)
        return user


class InventoryManagerCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=6)

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError("Username already taken")
        return value

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("Email already registered")
        return value

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data["email"],
            password=validated_data["password"],
            is_staff=True,
        )
        inv_role = Role.objects.get(name="inventory_manager")
        UserProfile.objects.create(user=user, role=inv_role)
        return user


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)


def get_password_reset_users(email):
    """Active buyers with a usable password matching the email (case-insensitive)."""
    users = User.objects.filter(
        email__iexact=email, is_active=True, profile__role__name="buyer"
    )
    return [user for user in users if user.has_usable_password()]


class ForgotPasswordSerializer(serializers.Serializer):
    # No existence check here: responding differently for unknown emails
    # would let callers enumerate registered accounts.
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)

    invalid_link_message = "Invalid or expired password reset link"

    def validate(self, data):
        user = self._get_user(data["uid"])
        if user is None or not default_token_generator.check_token(
            user, data["token"]
        ):
            raise serializers.ValidationError({"token": self.invalid_link_message})

        if data["new_password"] != data["confirm_password"]:
            raise serializers.ValidationError(
                {"confirm_password": "Passwords do not match"}
            )
        try:
            validate_password(data["new_password"], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"new_password": list(exc.messages)})

        data["user"] = user
        return data

    def _get_user(self, uid):
        try:
            pk = force_str(urlsafe_base64_decode(uid))
            user = User.objects.select_related("profile__role").get(
                pk=pk, is_active=True
            )
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return None
        role = getattr(getattr(user, "profile", None), "role", None)
        if role is None or role.name != "buyer":
            return None
        return user

    def save(self):
        user = self.validated_data["user"]
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password"])
        return user


class UserSerializer(serializers.ModelSerializer):
    role = serializers.CharField(source="profile.role.name", read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "role", "is_staff"]
