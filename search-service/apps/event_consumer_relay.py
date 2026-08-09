"""Event consumer relay for processing catalog events from Kafka.

This module consumes events from Kafka topics and processes them
using the CatalogEventConsumer to update Elasticsearch.
"""

import json
import logging
import signal
import sys
from typing import Optional

from django.conf import settings
from elasticsearch import Elasticsearch

from apps.elasticsearch import get_elasticsearch_client
from apps.event_consumers import CatalogEventConsumer

logger = logging.getLogger("catalog.search.consumer")


class EventConsumerRelay:
    """Relays Kafka events to Elasticsearch via CatalogEventConsumer."""

    def __init__(
        self,
        kafka_consumer=None,
        es_client: Optional[Elasticsearch] = None,
        topics: Optional[list[str]] = None,
        group_id: str = "search-service-consumer",
    ):
        """Initialize the event consumer relay.

        Args:
            kafka_consumer: Kafka consumer instance
            es_client: Elasticsearch client
            topics: Kafka topics to subscribe to
            group_id: Consumer group ID
        """
        self.kafka_consumer = kafka_consumer
        self.es_client = es_client or get_elasticsearch_client()
        self.catalog_consumer = CatalogEventConsumer(self.es_client)
        self.topics = topics or [
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
        self.group_id = group_id
        self._running = False

    def set_kafka_consumer(self, consumer):
        """Set the Kafka consumer instance."""
        self.kafka_consumer = consumer

    def start(self):
        """Start consuming events from Kafka."""
        if not self.kafka_consumer:
            logger.error("No Kafka consumer configured")
            return

        self._running = True
        logger.info(
            f"Event consumer relay started",
            extra={"topics": self.topics, "group_id": self.group_id},
        )

        # Handle graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        try:
            while self._running:
                msg = self.kafka_consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    logger.error(f"Kafka consumer error: {msg.error()}")
                    continue

                self._process_message(msg)
        except Exception as e:
            logger.error(f"Error in consumer relay: {e}", exc_info=True)
        finally:
            self.stop()

    def stop(self):
        """Stop consuming events."""
        self._running = False
        if self.kafka_consumer:
            self.kafka_consumer.close()
        logger.info("Event consumer relay stopped")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self._running = False

    def _process_message(self, msg):
        """Process a single Kafka message."""
        try:
            topic = msg.topic()
            key = msg.key().decode("utf-8") if msg.key() else None
            value = msg.value().decode("utf-8") if msg.value() else None

            if not value:
                logger.warning(f"Empty message from topic {topic}")
                return

            event_data = json.loads(value)

            # Process the event
            success = self.catalog_consumer.process_event(event_data)

            if success:
                logger.info(
                    f"Processed event from {topic}",
                    extra={
                        "topic": topic,
                        "key": key,
                        "event_type": event_data.get("event_type"),
                        "event_id": event_data.get("event_id"),
                    },
                )
            else:
                logger.warning(
                    f"Failed to process event from {topic}",
                    extra={
                        "topic": topic,
                        "key": key,
                        "event_type": event_data.get("event_type"),
                        "event_id": event_data.get("event_id"),
                    },
                )

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in message: {e}", extra={"topic": topic})
        except Exception as e:
            logger.error(
                f"Error processing message: {e}",
                exc_info=True,
                extra={"topic": topic, "key": key},
            )


def create_kafka_consumer(topics: list[str], group_id: str = "search-service-consumer"):
    """Create a Kafka consumer instance."""
    try:
        from kafka import KafkaConsumer

        kafka_bootstrap = getattr(settings, "KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")

        consumer = KafkaConsumer(
            *topics,
            bootstrap_servers=kafka_bootstrap,
            group_id=group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            value_deserializer=lambda m: m.decode("utf-8"),
        )

        logger.info(f"Created Kafka consumer connected to {kafka_bootstrap}")
        return consumer

    except ImportError:
        logger.warning("kafka-python not installed")
        return None
    except Exception as e:
        logger.error(f"Failed to create Kafka consumer: {e}")
        return None
