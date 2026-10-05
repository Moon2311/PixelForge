from django.urls import path
from .views import (
    RegisterBuyerView,
    CreateInventoryManagerView,
    LoginView,
    RefreshTokenView,
    LogoutView,
    ForgotPasswordView,
    ResetPasswordView,
)

urlpatterns = [
    path("register/buyer/", RegisterBuyerView.as_view(), name="register-buyer"),
    path(
        "create/inventory-manager/",
        CreateInventoryManagerView.as_view(),
        name="create-inventory-manager",
    ),
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", RefreshTokenView.as_view(), name="token-refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("forgot-password/", ForgotPasswordView.as_view(), name="forgot-password"),
    path("reset-password/", ResetPasswordView.as_view(), name="reset-password"),
]
