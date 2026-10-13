from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.events import registry
from apps.events.worker import Worker


class Command(BaseCommand):
    help = "Run a Kafka consumer worker for one consumer group."

    def add_arguments(self, parser):
        parser.add_argument("group", choices=sorted(registry.GROUPS))
        parser.add_argument("--max-messages", type=int, default=None)

    def handle(self, *args, group, max_messages, **options):
        if settings.EVENTS_BACKEND != "kafka":
            raise CommandError("kafka_worker needs EVENTS_BACKEND=kafka (KAFKA_BOOTSTRAP_SERVERS)")
        if settings.KAFKA_MAX_POLL_INTERVAL_MS / 1000 <= settings.EVENTS_TASK_TIMEOUT:
            raise CommandError("KAFKA_MAX_POLL_INTERVAL_MS must exceed EVENTS_TASK_TIMEOUT")
        Worker(group).run(max_messages=max_messages)
