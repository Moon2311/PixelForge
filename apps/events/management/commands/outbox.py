"""Outbox operations: status, requeue failed rows, clean up published rows."""

import json
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.events import health
from apps.events.models import OutboxEvent, ProcessedEvent


class Command(BaseCommand):
    help = "Outbox status, requeue of failed rows, cleanup of published rows."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("status")
        requeue = sub.add_parser("requeue", help="Make failed rows pending again (after fixing the cause).")
        requeue.add_argument("ids", nargs="*")
        requeue.add_argument("--all-failed", action="store_true")
        cleanup = sub.add_parser("cleanup", help="Delete published rows older than OUTBOX_RETENTION_DAYS "
                                                 "and idempotency records older than EVENTS_PROCESSED_RETENTION_DAYS.")
        cleanup.add_argument("--days", type=int, default=None)

    def handle(self, *args, action, **options):
        getattr(self, f"_{action}")(**options)

    def _status(self, **_):
        self.stdout.write(json.dumps(health.outbox(), indent=2))

    def _requeue(self, ids, all_failed, **_):
        rows = OutboxEvent.objects.filter(status=OutboxEvent.STATUS_FAILED)
        if ids:
            rows = rows.filter(pk__in=ids)
        elif not all_failed:
            raise CommandError("Give row ids or --all-failed")
        count = rows.update(status=OutboxEvent.STATUS_PENDING, available_at=timezone.now(), locked_until=None)
        self.stdout.write(self.style.SUCCESS(f"{count} row(s) pending again"))

    def _cleanup(self, days, **_):
        days = settings.OUTBOX_RETENTION_DAYS if days is None else days
        cutoff = timezone.now() - timedelta(days=days)
        count, _ = OutboxEvent.objects.filter(status=OutboxEvent.STATUS_PUBLISHED, published_at__lt=cutoff).delete()
        self.stdout.write(f"deleted {count} published row(s) older than {days} day(s)")
        # Idempotency records must outlive any possible redelivery (Kafka retention).
        keep = settings.EVENTS_PROCESSED_RETENTION_DAYS
        count, _ = ProcessedEvent.objects.filter(processed_at__lt=timezone.now() - timedelta(days=keep)).delete()
        self.stdout.write(f"deleted {count} idempotency record(s) older than {keep} day(s)")
