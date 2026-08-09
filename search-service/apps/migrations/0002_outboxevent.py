"""Migration for OutboxEvent model.

This migration adds the OutboxEvent table for the Transactional Outbox pattern.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("apps", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="OutboxEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("event_type", models.CharField(db_index=True, max_length=100)),
                (
                    "aggregate_type",
                    models.CharField(db_index=True, max_length=50),
                ),
                (
                    "aggregate_id",
                    models.CharField(db_index=True, max_length=100),
                ),
                ("payload", models.JSONField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("processing", "Processing"),
                            ("published", "Published"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("retry_count", models.IntegerField(default=0)),
                ("max_retries", models.IntegerField(default=5)),
                (
                    "available_at",
                    models.DateTimeField(db_index=True, default=django.utils.timezone.now),
                ),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("last_error", models.TextField(blank=True, default="")),
            ],
            options={
                "ordering": ["created_at"],
                "indexes": [
                    models.Index(
                        fields=["status", "available_at"],
                        name="outbox_event_status_available_idx",
                    ),
                    models.Index(
                        fields=["event_type"],
                        name="outbox_event_event_type_idx",
                    ),
                    models.Index(
                        fields=["aggregate_type", "aggregate_id"],
                        name="outbox_event_aggregate_idx",
                    ),
                ],
            },
        ),
    ]
