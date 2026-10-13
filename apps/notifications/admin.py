from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("kind", "recipient", "status", "attempts", "sent_at", "created_at")
    list_filter = ("status", "kind")
    search_fields = ("recipient", "dedup_key", "event_id")
    readonly_fields = [f.name for f in Notification._meta.fields]
