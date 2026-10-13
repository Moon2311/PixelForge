"""Create the project's Kafka topics (idempotent; existing topics are left as
they are, and a partition-count difference is only reported)."""

import time

from confluent_kafka import KafkaException
from confluent_kafka.admin import NewTopic
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.events import kafka, topics


class Command(BaseCommand):
    help = "Create the Kafka topics in settings.KAFKA_TOPICS (see apps/events/topics.py)."

    def add_arguments(self, parser):
        parser.add_argument("--wait", type=float, default=0, help="Seconds to wait for the broker.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, wait, dry_run, **options):
        if not settings.KAFKA_BOOTSTRAP_SERVERS:
            raise CommandError("KAFKA_BOOTSTRAP_SERVERS is not set")
        client = kafka.admin()
        metadata = self._metadata(client, wait)
        specs = topics.specs()
        missing = [spec for spec in specs if spec.name not in metadata.topics]
        for spec in specs:
            if spec.name in metadata.topics:
                actual = len(metadata.topics[spec.name].partitions)
                note = "" if actual == spec.partitions else f" (has {actual} partitions, config says {spec.partitions})"
                self.stdout.write(f"exists  {spec.name}{note}")
        if dry_run or not missing:
            for spec in missing:
                self.stdout.write(f"missing {spec.name} ({spec.partitions} partitions)")
            return
        futures = client.create_topics(
            [NewTopic(s.name, num_partitions=s.partitions, replication_factor=s.replication_factor, config=s.config)
             for s in missing],
            request_timeout=settings.KAFKA_ADMIN_TIMEOUT * 5,
        )
        failed = False
        for name, future in futures.items():
            try:
                future.result()
                self.stdout.write(self.style.SUCCESS(f"created {name}"))
            except KafkaException as exc:
                error = exc.args[0]
                if error.code() == error.TOPIC_ALREADY_EXISTS:
                    self.stdout.write(f"exists  {name}")
                else:
                    failed = True
                    self.stderr.write(f"failed  {name}: {error.str()}")
        if failed:
            raise CommandError("Some topics could not be created")

    def _metadata(self, client, wait):
        deadline = time.monotonic() + wait
        while True:
            try:
                return client.list_topics(timeout=settings.KAFKA_ADMIN_TIMEOUT)
            except KafkaException as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"Kafka not reachable: {exc}") from None
                self.stdout.write("waiting for Kafka...")
                time.sleep(2)
