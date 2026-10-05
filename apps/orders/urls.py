from django.urls import path

from . import views

urlpatterns = [
    path("", views.OrderListCreateView.as_view(), name="order-list-create"),
    path("checkout-options/", views.CheckoutOptionsView.as_view(), name="checkout-options"),
    path("admin/orders/", views.AdminOrderListView.as_view(), name="admin-order-list"),
    path(
        "admin/products/<int:product_id>/sales/",
        views.AdminProductSalesView.as_view(),
        name="admin-product-sales",
    ),
    path("<str:number>/", views.OrderDetailView.as_view(), name="order-detail"),
]
