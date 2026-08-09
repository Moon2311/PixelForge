"""Management command to run the event relay.

This command polls the OutboxEvent table for pending events and publishes
them to Kafka. It implements exponential backoff for retries, dead-letter
handling, and structured logging.

Usage:
    python manage.py relay_events
    python manage.py relay_events --batch-size 50
    python manage.py relay_messages --poll-interval 10
"""

import logging

from django.core.management.base import BaseCommand

from apps.event_relay import EventRelay

logger = logging.getLogger("catalog.events.relay")


class Command(BaseCommand):
    help = "Run the event relay to publish outbox events to Kafka"

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of events to process per batch (default: 100)",
        )
        parser.add_argument(
            "--poll-interval",
            type=int,
            default=5,
            help="Seconds between polling cycles (default: 5)",
        )
        parser.add_argument(
            "--topic-prefix",
            type=str,
            default="catalog.",
            help="Prefix for Kafka topic names (default: catalog.)",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        poll_interval = options["poll_interval"]
        topic_prefix = options["topic_prefix"]

        self.stdout.write(
            self.style.SUCCESS(
                f"Starting event relay (batch_size={batch_size}, "
                f"poll_interval={poll_interval}s, topic_prefix={topic_prefix})"
            )
        )

        relay = EventRelay(
            topic_prefix=topic_prefix,
            batch_size=batch_size,
            poll_interval=poll_interval,
        )

        # Try to connect to Kafka
        try:
            from kafka import KafkaProducer

            from django.conf import settings

            kafka_bootstrap = getattr(settings, "KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
            producer = KafkaProducer(
                bootstrap_servers=kafka_bootstrap,
                value_serializer=lambda v: v,
            )
            relay.set_kafka_producer(producer)
            self.stdout.write(
                self.style.SUCCESS(f"Connected to Kafka at {kafka_bootstrap}")
            )
        except ImportError:
            self.stdout.write(
                self.style.WARNING(
                    "kafka-python not installed. Events will be logged only."
                )
            )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to connect to Kafka: {e}. Events will be logged only.")
            )

        try:
            relay.start()
        except KeyboardInterrupt:
            relay.stop()
            self.stdout.write(self.style.SUCCESS("Event relay stopped"))
