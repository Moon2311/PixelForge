"""Management command for dead-letter event handling.

This command provides utilities for managing failed events in the outbox,
including retrying failed events, viewing dead-letter statistics, and
purging old failed events.

Usage:
    python manage.py dead_letter retry --event-id 123
    python manage.py dead_letter retry --status failed
    python manage.py dead_letter stats
    python manage.py dead_letter purge --older-than 30
"""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from apps.models import OutboxEvent

logger = logging.getLogger("catalog.events.dead_letter")


class Command(BaseCommand):
    help = "Manage dead-letter events in the outbox"

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="subcommand", help="Sub-command")

        # retry subcommand
        retry_parser = subparsers.add_parser("retry", help="Retry failed events")
        retry_parser.add_argument("--event-id", type=int, help="Specific event ID to retry")
        retry_parser.add_argument(
            "--status",
            type=str,
            default="failed",
            choices=["failed", "all"],
            help="Status of events to retry (default: failed)",
        )
        retry_parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum number of events to retry (default: 100)",
        )

        # stats subcommand
        subparsers.add_parser("stats", help="Show dead-letter statistics")

        # purge subcommand
        purge_parser = subparsers.add_parser("purge", help="Purge old failed events")
        purge_parser.add_argument(
            "--older-than",
            type=int,
            default=30,
            help="Purge events older than N days (default: 30)",
        )
        purge_parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be purged without actually deleting",
        )

        # reset subcommand
        reset_parser = subparsers.add_parser("reset", help="Reset stuck events")
        reset_parser.add_argument(
            "--stuck-minutes",
            type=int,
            default=5,
            help="Minutes before an event is considered stuck (default: 5)",
        )

    def handle(self, *args, **options):
        subcommand = options.get("subcommand")
        if not subcommand:
            self.stdout.write(self.style.ERROR("Please specify a subcommand: retry, stats, purge, reset"))
            return

        if subcommand == "retry":
            self._retry_events(options)
        elif subcommand == "stats":
            self._show_stats()
        elif subcommand == "purge":
            self._purge_events(options)
        elif subcommand == "reset":
            self._reset_stuck_events(options)

    def _retry_events(self, options):
        """Reset failed events back to pending for retry."""
        event_id = options.get("event_id")
        status = options.get("status")
        limit = options.get("limit")

        queryset = OutboxEvent.objects.all()

        if event_id:
            queryset = queryset.filter(pk=event_id)
        elif status == "failed":
            queryset = queryset.filter(status=OutboxEvent.STATUS_FAILED)

        count = 0
        for event in queryset[:limit]:
            event.status = OutboxEvent.STATUS_PENDING
            event.retry_count = 0
            event.last_error = ""
            event.available_at = timezone.now()
            event.save(update_fields=["status", "retry_count", "last_error", "available_at"])
            count += 1

            logger.info(
                "Event reset for retry",
                extra={"event_id": event.pk, "event_type": event.event_type},
            )

        self.stdout.write(self.style.SUCCESS(f"Reset {count} events for retry"))

    def _show_stats(self):
        """Show dead-letter statistics."""
        total = OutboxEvent.objects.count()
        pending = OutboxEvent.objects.filter(status=OutboxEvent.STATUS_PENDING).count()
        processing = OutboxEvent.objects.filter(status=OutboxEvent.STATUS_PROCESSING).count()
        published = OutboxEvent.objects.filter(status=OutboxEvent.STATUS_PUBLISHED).count()
        failed = OutboxEvent.objects.filter(status=OutboxEvent.STATUS_FAILED).count()

        self.stdout.write("\n=== Outbox Event Statistics ===")
        self.stdout.write(f"Total events:    {total}")
        self.stdout.write(f"Pending:         {pending}")
        self.stdout.write(f"Processing:      {processing}")
        self.stdout.write(f"Published:       {published}")
        self.stdout.write(f"Failed:          {failed}")
        self.stdout.write("")

        # Show failed events by type
        if failed > 0:
            self.stdout.write("\n=== Failed Events by Type ===")
            failed_by_type = (
                OutboxEvent.objects.filter(status=OutboxEvent.STATUS_FAILED)
                .values("event_type")
                .annotate(count=Count("id"))
                .order_by("event_type")
            )
            for item in failed_by_type:
                self.stdout.write(f"  {item['event_type']}: {item['count']}")

            self.stdout.write("\n=== Recent Failed Events ===")
            recent_failed = OutboxEvent.objects.filter(
                status=OutboxEvent.STATUS_FAILED
            ).order_by("-created_at")[:5]
            for event in recent_failed:
                self.stdout.write(
                    f"  [{event.pk}] {event.event_type} "
                    f"({event.aggregate_type}:{event.aggregate_id}) "
                    f"- Retries: {event.retry_count}, Error: {event.last_error[:100]}"
                )

    def _purge_events(self, options):
        """Purge old failed events."""
        older_than_days = options.get("older_than")
        dry_run = options.get("dry_run")

        cutoff_date = timezone.now() - timedelta(days=older_than_days)
        queryset = OutboxEvent.objects.filter(
            status=OutboxEvent.STATUS_FAILED,
            created_at__lt=cutoff_date,
        )

        count = queryset.count()

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"DRY RUN: Would purge {count} events older than {older_than_days} days")
            )
            for event in queryset[:10]:
                self.stdout.write(
                    f"  [{event.pk}] {event.event_type} "
                    f"({event.aggregate_type}:{event.aggregate_id}) "
                    f"- Created: {event.created_at}"
                )
        else:
            queryset.delete()
            self.stdout.write(
                self.style.SUCCESS(f"Purged {count} events older than {older_than_days} days")
            )

    def _reset_stuck_events(self, options):
        """Reset events stuck in processing status."""
        stuck_minutes = options.get("stuck_minutes")
        cutoff_time = timezone.now() - timedelta(minutes=stuck_minutes)

        stuck_events = OutboxEvent.objects.filter(
            status=OutboxEvent.STATUS_PROCESSING,
            created_at__lt=cutoff_time,
        )

        count = 0
        for event in stuck_events:
            event.status = OutboxEvent.STATUS_PENDING
            event.available_at = timezone.now()
            event.save(update_fields=["status", "available_at"])
            count += 1

            logger.warning(
                "Reset stuck event",
                extra={
                    "event_id": event.pk,
                    "event_type": event.event_type,
                    "stuck_since": event.created_at.isoformat(),
                },
            )

        self.stdout.write(self.style.SUCCESS(f"Reset {count} stuck events"))
