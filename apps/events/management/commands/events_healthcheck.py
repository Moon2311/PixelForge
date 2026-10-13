"""Container health check for a worker or the publisher: exit 0 when this
host's process has a fresh heartbeat (Docker Compose `healthcheck`)."""

import socket
import sys
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.events.models import ConsumerHeartbeat
from apps.events.publisher import ROLE as PUBLISHER_ROLE


class Command(BaseCommand):
    help = "Exit 0 if this host's worker/publisher heartbeat is fresh."

    def add_arguments(self, parser):
        parser.add_argument("kind", choices=["worker", PUBLISHER_ROLE])

    def handle(self, *args, kind, **options):
        fresh = timezone.now() - timedelta(seconds=settings.EVENTS_HEARTBEAT_STALE_AFTER)
        rows = ConsumerHeartbeat.objects.filter(hostname=socket.gethostname()[:100], last_seen__gte=fresh)
        rows = rows.filter(role=PUBLISHER_ROLE) if kind == PUBLISHER_ROLE else rows.exclude(role=PUBLISHER_ROLE)
        if not rows.exists():
            self.stderr.write("no fresh heartbeat")
            sys.exit(1)
        self.stdout.write("ok")
