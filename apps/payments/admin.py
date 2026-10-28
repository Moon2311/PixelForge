from django.contrib import admin

from .models import GatewayCircuit, Payment, PaymentNotification


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("payment_id", "order", "payment_method", "amount", "status",
                    "provider_transaction_id", "check_attempts", "last_checked_at", "created_at", "paid_at")
    list_filter = ("status", "payment_method")
    search_fields = ("payment_id", "provider_transaction_id", "order__number")
    readonly_fields = [f.name for f in Payment._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(PaymentNotification)
class PaymentNotificationAdmin(admin.ModelAdmin):
    list_display = ("payment", "provider", "source", "signature_valid", "reported_status",
                    "receive_count", "first_received_at", "last_received_at")
    list_filter = ("provider", "source", "signature_valid")
    search_fields = ("payment__payment_id",)
    readonly_fields = [f.name for f in PaymentNotification._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(GatewayCircuit)
class GatewayCircuitAdmin(admin.ModelAdmin):
    list_display = ("provider", "state", "consecutive_failures", "opened_at", "last_failure_at", "updated_at")
    readonly_fields = [f.name for f in GatewayCircuit._meta.fields]

    def has_add_permission(self, request):
        return False
