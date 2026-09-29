from django.urls import path

from . import views

urlpatterns = [
    path("guest/", views.GuestCartView.as_view(), name="guest-cart"),
    path("guest/items/", views.GuestCartItemView.as_view(), name="guest-cart-items"),
    path("guest/items/<int:pk>/", views.GuestCartItemView.as_view(), name="guest-cart-item-detail"),
    path("merge/", views.MergeCartView.as_view(), name="cart-merge"),
    path("", views.CartView.as_view(), name="cart"),
    path("items/", views.CartItemAddView.as_view(), name="cart-items-add"),
    path("items/<int:item_id>/", views.CartItemDetailView.as_view(), name="cart-item-detail"),
]
