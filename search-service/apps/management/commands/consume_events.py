"""Management command to start the event consumer relay.

This command starts a Kafka consumer that processes catalog events
and updates the Elasticsearch projection.

Usage:
    python manage.py consume_events
    python manage.py consume_events --group-id my-group
    python manage.py consume_events --topics catalog.product catalog.category
"""

import logging

from django.core.management.base import BaseCommand

from apps.event_consumer_relay import EventConsumerRelay, create_kafka_consumer

logger = logging.getLogger("catalog.search.consumer")


class Command(BaseCommand):
    help = "Start the event consumer relay for catalog events"

    def add_arguments(self, parser):
        parser.add_argument(
            "--group-id",
            type=str,
            default="search-service-consumer",
            help="Kafka consumer group ID (default: search-service-consumer)",
        )
        parser.add_argument(
            "--topics",
            nargs="+",
            default=None,
            help="Kafka topics to subscribe to (default: all catalog topics)",
        )

    def handle(self, *args, **options):
        group_id = options["group_id"]
        topics = options["topics"]

        if topics is None:
            topics = [
                "catalog.product",
                "catalog.category",
                "catalog.subcategory",
                "catalog.brand",
                "catalog.product_image",
                "catalog.product_variant",
                "catalog.variant_price",
                "catalog.inventory",
                "catalog.product_review",
            ]

        self.stdout.write(
            self.style.SUCCESS(
                f"Starting event consumer relay (group={group_id}, topics={topics})"
            )
        )

        # Create Kafka consumer
        kafka_consumer = create_kafka_consumer(topics, group_id)

        if not kafka_consumer:
            self.stdout.write(
                self.style.ERROR(
                    "Failed to create Kafka consumer. "
                    "Make sure kafka-python is installed and Kafka is running."
                )
            )
            return

        # Create and start relay
        relay = EventConsumerRelay(
            kafka_consumer=kafka_consumer,
            topics=topics,
            group_id=group_id,
        )

        try:
            relay.start()
        except KeyboardInterrupt:
            relay.stop()
            self.stdout.write(self.style.SUCCESS("Event consumer relay stopped"))
