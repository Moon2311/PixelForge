from django.contrib import admin

from .models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("payment_id", "order", "payment_method", "amount", "status",
                    "provider_transaction_id", "created_at", "paid_at")
    list_filter = ("status", "payment_method")
    search_fields = ("payment_id", "provider_transaction_id", "order__number")
    readonly_fields = [f.name for f in Payment._meta.fields]

    def has_add_permission(self, request):
        return False
