from django.core.management.base import BaseCommand

from apps.events.publisher import OutboxPublisher


class Command(BaseCommand):
    help = "Publish pending outbox rows to Kafka (or process them in-process with EVENTS_BACKEND=inline)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Publish one batch and exit.")
        parser.add_argument("--batch-size", type=int, default=None)

    def handle(self, *args, once, batch_size, **options):
        OutboxPublisher(batch_size=batch_size).run(once=once)
