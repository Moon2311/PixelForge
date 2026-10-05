from django.contrib import admin

from .models import Order, OrderItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("number", "user_id", "status", "payment_method", "delivery_method", "total", "created_at")
    list_filter = ("status", "payment_method", "delivery_method")
    search_fields = ("number", "contact")
    inlines = [OrderItemInline]
