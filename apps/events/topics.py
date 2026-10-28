"""Kafka topics: one per event stream, not per task or per entity.

Keys keep related events in order within a partition (order number for
order and payment events, variant id for inventory, image path for image
jobs). Kafka orders messages only within a partition, never across them.

    order-events         order.placed                          key: order number
    payment-events       payment.succeeded/failed/refunded     key: order number
    payment-processing   payment.verification_requested        key: order number
    inventory-events     inventory.low_stock                   key: variant id
    notification-events  notification.password_*               key: user id
    image-processing     image.uploaded                        key: file path
    audit-events         audit.* (best effort, no outbox)      key: aggregate id
    retry-events         failed tasks waiting for their next attempt
    dead-letter-events   tasks that exhausted their retries or can't succeed
"""

from dataclasses import dataclass

from django.conf import settings

ORDER_EVENTS = "order-events"
PAYMENT_EVENTS = "payment-events"
# Work for the payments group: ask the provider what happened to a payment.
PAYMENT_PROCESSING = "payment-processing"
INVENTORY_EVENTS = "inventory-events"
NOTIFICATION_EVENTS = "notification-events"
IMAGE_PROCESSING = "image-processing"
AUDIT_EVENTS = "audit-events"
RETRY_EVENTS = "retry-events"
DEAD_LETTER_EVENTS = "dead-letter-events"


def name(logical):
    """The Kafka topic for a logical topic name (KAFKA_TOPIC_PREFIX and
    per-topic overrides applied)."""
    return settings.KAFKA_TOPICS[logical]


def logical_name(topic):
    """Inverse of ``name()``; None for a topic this project doesn't own."""
    for logical, actual in settings.KAFKA_TOPICS.items():
        if actual == topic:
            return logical
    return None


@dataclass(frozen=True)
class TopicSpec:
    name: str
    partitions: int
    replication_factor: int
    config: dict


def specs():
    """What ``kafka_create_topics`` creates.

    Every stream gets KAFKA_TOPIC_PARTITIONS partitions (the most consumers
    of one group that can work in parallel). The DLQ keeps one partition
    (low volume, read in order by operators) and a longer retention.
    """
    hours = settings.KAFKA_TOPIC_RETENTION_HOURS
    common = {
        "cleanup.policy": "delete",
        "min.insync.replicas": str(settings.KAFKA_TOPIC_MIN_INSYNC_REPLICAS),
    }
    result = []
    for logical, actual in settings.KAFKA_TOPICS.items():
        partitions = settings.KAFKA_TOPIC_PARTITIONS
        retention = hours
        if logical == DEAD_LETTER_EVENTS:
            partitions = 1
            retention = settings.KAFKA_DLQ_RETENTION_HOURS
        result.append(TopicSpec(
            name=actual,
            partitions=partitions,
            replication_factor=settings.KAFKA_TOPIC_REPLICATION_FACTOR,
            config={**common, "retention.ms": str(retention * 3600 * 1000)},
        ))
    return result
