from django.contrib import admin

from .models import (
    AuditEvent,
    ConsumerHeartbeat,
    DeadLetterEvent,
    OutboxEvent,
    ProcessedEvent,
)


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "aggregate_id", "topic", "kind", "status", "attempts", "created_at", "published_at")
    list_filter = ("status", "kind", "topic", "event_type")
    search_fields = ("event_id", "aggregate_id")
    readonly_fields = [f.name for f in OutboxEvent._meta.fields]


@admin.register(ProcessedEvent)
class ProcessedEventAdmin(admin.ModelAdmin):
    list_display = ("consumer_group", "event_type", "event_id", "attempt", "duration_ms", "processed_at")
    list_filter = ("consumer_group", "event_type")
    search_fields = ("event_id",)


@admin.register(DeadLetterEvent)
class DeadLetterEventAdmin(admin.ModelAdmin):
    list_display = ("consumer_group", "event_type", "aggregate_id", "reason", "attempts", "status",
                    "replay_count", "last_failed_at")
    list_filter = ("status", "consumer_group", "reason", "event_type")
    search_fields = ("event_id", "aggregate_id")
    readonly_fields = [f.name for f in DeadLetterEvent._meta.fields if f.name not in ("note",)]


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "aggregate_type", "aggregate_id", "actor_user_id", "occurred_at")
    list_filter = ("event_type",)
    search_fields = ("aggregate_id", "event_id", "correlation_id")


@admin.register(ConsumerHeartbeat)
class ConsumerHeartbeatAdmin(admin.ModelAdmin):
    list_display = ("role", "member", "last_seen", "started_at")
    list_filter = ("role",)
