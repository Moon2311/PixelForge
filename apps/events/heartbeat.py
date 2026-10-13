"""Worker/publisher heartbeats in PostgreSQL (``ConsumerHeartbeat``).

Each process upserts its row every EVENTS_HEARTBEAT_INTERVAL seconds with
its partition assignment and counters, and deletes it on a clean shutdown.
A row that stops updating belongs to a crashed or stuck process: health
checks report it as stale. Heartbeat failures never stop the process.
"""

import os
import socket
import time
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone

from .models import ConsumerHeartbeat

# Rows not updated for this long are removed by any live process.
FORGET_AFTER = timedelta(days=1)


class Heartbeat:
    def __init__(self, role):
        self.role = role
        self.hostname = socket.gethostname()
        self.pid = os.getpid()
        self.member = f"{role}@{self.hostname}:{self.pid}"
        self.started_at = timezone.now()
        self._last = 0.0

    def beat_if_due(self, assignment=None, stats=None):
        if time.monotonic() - self._last >= settings.EVENTS_HEARTBEAT_INTERVAL:
            self.beat(assignment, stats)

    def beat(self, assignment=None, stats=None):
        self._last = time.monotonic()
        now = timezone.now()
        try:
            ConsumerHeartbeat.objects.update_or_create(
                member=self.member,
                defaults={
                    "role": self.role,
                    "hostname": self.hostname[:100],
                    "pid": self.pid,
                    "started_at": self.started_at,
                    "last_seen": now,
                    "assignment": assignment or [],
                    "stats": stats or {},
                },
            )
            ConsumerHeartbeat.objects.filter(last_seen__lt=now - FORGET_AFTER).delete()
        except DatabaseError:
            pass

    def stop(self):
        try:
            ConsumerHeartbeat.objects.filter(member=self.member).delete()
        except DatabaseError:
            pass
