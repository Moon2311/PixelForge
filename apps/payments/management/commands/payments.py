"""Payment operations: pipeline status, circuit reset, manual verification."""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.payments import health
from apps.payments.models import Payment
from apps.payments.services import circuit, request_verification


class Command(BaseCommand):
    help = "Payment pipeline status, circuit-breaker reset, manual provider check."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("status", help="Open payments, UNKNOWN backlog, circuits, worker counters.")
        reset = sub.add_parser("circuit-reset", help="Close a provider's circuit by hand.")
        reset.add_argument("provider", choices=[m for m, _ in Payment.METHOD_CHOICES])
        verify = sub.add_parser("verify", help="Queue a provider check for payments (bypasses throttling).")
        verify.add_argument("payment_ids", nargs="+")

    def handle(self, *args, action, **options):
        getattr(self, f"_{action.replace('-', '_')}")(**options)

    def _status(self, **_):
        self.stdout.write(json.dumps(health.snapshot(), indent=2, default=str))

    def _circuit_reset(self, provider, **_):
        circuit.reset(provider)
        self.stdout.write(self.style.SUCCESS(f"{provider} circuit closed"))

    def _verify(self, payment_ids, **_):
        for payment_id in payment_ids:
            payment = Payment.objects.filter(payment_id=payment_id).first()
            if payment is None:
                raise CommandError(f"{payment_id}: not found")
            queued = request_verification(payment, reason="operator", include_closed=True, force=True)
            self.stdout.write(f"{payment_id}: {'queued' if queued else f'not queued ({payment.status})'}")
