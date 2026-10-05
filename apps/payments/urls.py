from django.urls import path

from . import views
from .webhooks import easypaisa, jazzcash

urlpatterns = [
    path("create/", views.PaymentCreateView.as_view(), name="payment-create"),
    path("jazzcash/callback/", jazzcash.JazzCashReturnView.as_view(), name="jazzcash-callback"),
    path("jazzcash/ipn/", jazzcash.JazzCashIPNView.as_view(), name="jazzcash-ipn"),
    path("easypaisa/callback/", easypaisa.EasypaisaReturnView.as_view(), name="easypaisa-callback"),
    path("easypaisa/ipn/", easypaisa.EasypaisaIPNView.as_view(), name="easypaisa-ipn"),
    path("<str:payment_id>/", views.PaymentDetailView.as_view(), name="payment-detail"),
]
