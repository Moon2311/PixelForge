"""Reconciliation loop (``manage.py payments_reconcile``): every
PAYMENT_RECONCILE_INTERVAL seconds, queue provider checks for payments left
PROCESSING/UNKNOWN or abandoned PENDING, and escalate old UNKNOWN ones
(``services.reconcile``). Several instances are safe (each payment is
claimed through ``next_check_at``)."""

import signal
import threading

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections

from apps.events import heartbeat
from apps.events import metrics as pipeline_metrics
from apps.payments import health, metrics, services


class Command(BaseCommand):
    help = "Re-check open payments with their provider and escalate unresolved UNKNOWN ones."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one pass and exit.")

    def handle(self, *args, once, **options):
        stop = threading.Event()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
        beat = heartbeat.Heartbeat(health.RECONCILER_ROLE)
        metrics.event("info", "payments_reconciler_started", interval=settings.PAYMENT_RECONCILE_INTERVAL)
        try:
            while not stop.is_set():
                close_old_connections()
                try:
                    result = services.reconcile()
                    if once:
                        self.stdout.write(str(result))
                except DatabaseError as exc:
                    metrics.event("warning", "payments_reconcile_db_error", error=type(exc).__name__)
                beat.beat(stats=pipeline_metrics.snapshot())
                if once:
                    break
                stop.wait(settings.PAYMENT_RECONCILE_INTERVAL)
        finally:
            beat.stop()
            metrics.event("info", "payments_reconciler_stopped")
