"""Dead letters: list, inspect, replay (bounded) or discard. See apps/events/dlq.py."""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.events import dlq
from apps.events.models import DeadLetterEvent


class Command(BaseCommand):
    help = "Inspect and replay dead-lettered background tasks."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        listing = sub.add_parser("list")
        listing.add_argument("--group")
        listing.add_argument("--status", default=DeadLetterEvent.STATUS_PENDING,
                             choices=[c for c, _ in DeadLetterEvent.STATUS_CHOICES] + ["all"])
        listing.add_argument("--limit", type=int, default=50)
        show = sub.add_parser("show")
        show.add_argument("id", type=int)
        replay = sub.add_parser("replay")
        replay.add_argument("ids", type=int, nargs="*")
        replay.add_argument("--group", help="With --all: replay pending dead letters of this group.")
        replay.add_argument("--all", action="store_true")
        replay.add_argument("--limit", type=int, default=10)
        replay.add_argument("--note", default="")
        replay.add_argument("--dry-run", action="store_true")
        discard = sub.add_parser("discard")
        discard.add_argument("ids", type=int, nargs="+")
        discard.add_argument("--note", required=True)

    def handle(self, *args, action, **options):
        getattr(self, f"_{action}")(**options)

    def _list(self, group, status, limit, **_):
        rows = DeadLetterEvent.objects.all()
        if status != "all":
            rows = rows.filter(status=status)
        if group:
            rows = rows.filter(consumer_group=group)
        for row in rows[:limit]:
            self.stdout.write(
                f"#{row.pk} {row.status:9} {row.consumer_group:14} {row.event_type:40} {row.aggregate_id:20} "
                f"{row.reason:18} attempts={row.attempts} replays={row.replay_count} "
                f"{row.error_message[:80]}"
            )

    def _show(self, id, **_):
        row = DeadLetterEvent.objects.filter(pk=id).first()
        if row is None:
            raise CommandError(f"No dead letter #{id}")
        self.stdout.write(json.dumps(row.envelope, indent=2, default=str))

    def _replay(self, ids, group, all, limit, note, dry_run, **_):
        if limit < 1 or limit > dlq.MAX_REPLAY_BATCH:
            raise CommandError(f"--limit must be between 1 and {dlq.MAX_REPLAY_BATCH}")
        if ids and all:
            raise CommandError("Give ids or --all, not both")
        if all:
            if not group:
                raise CommandError("--all needs --group")
            ids = list(
                DeadLetterEvent.objects.filter(status=DeadLetterEvent.STATUS_PENDING, consumer_group=group)
                .order_by("created_at").values_list("pk", flat=True)[:limit]
            )
        elif not ids:
            raise CommandError("Give dead letter ids, or --group G --all")
        if len(ids) > limit:
            raise CommandError(f"{len(ids)} ids given; at most --limit={limit} per run")
        replayed = 0
        for pk in ids:
            if dry_run:
                self.stdout.write(f"would replay #{pk}")
                continue
            try:
                dlq.replay(pk, note=note)
            except (dlq.ReplayRefused, DeadLetterEvent.DoesNotExist) as exc:
                self.stderr.write(f"#{pk}: {exc}")
                continue
            replayed += 1
            self.stdout.write(f"replayed #{pk}")
        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f"{replayed} replayed (published by outbox_publisher)"))

    def _discard(self, ids, note, **_):
        for pk in ids:
            try:
                dlq.discard(pk, note)
            except DeadLetterEvent.DoesNotExist:
                self.stderr.write(f"#{pk}: not found")
                continue
            self.stdout.write(f"discarded #{pk}")
