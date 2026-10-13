"""Kafka clients (confluent-kafka, the librdkafka-based client maintained by
Confluent; see README "Kafka client").

Producers always use ``acks=all`` with idempotence on, so a retried send
doesn't write the message twice within one producer session. That protects
against producer retries only: the outbox publisher can still publish a row
twice (crash before marking it), so consumers deduplicate on the event id.

Consumers never auto-commit: a worker commits an offset only after the
message was processed or its retry/dead letter was durably recorded.
"""

import os
import socket
import threading

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from confluent_kafka.admin import AdminClient
from django.conf import settings

# Errors retrying the same message can't fix.
PERMANENT_ERRORS = {
    KafkaError.MSG_SIZE_TOO_LARGE,
    KafkaError.INVALID_RECORD,
    KafkaError.RECORD_LIST_TOO_LARGE,
    KafkaError.TOPIC_AUTHORIZATION_FAILED,
    KafkaError._INVALID_ARG,
}


def enabled():
    return settings.EVENTS_BACKEND == "kafka"


def base_config(role=""):
    config = {
        "bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS,
        "client.id": f"{settings.KAFKA_CLIENT_ID}-{role or 'client'}-{socket.gethostname()}",
        "security.protocol": settings.KAFKA_SECURITY_PROTOCOL,
    }
    if settings.KAFKA_SASL_MECHANISM:
        config.update({
            "sasl.mechanism": settings.KAFKA_SASL_MECHANISM,
            "sasl.username": settings.KAFKA_SASL_USERNAME,
            "sasl.password": settings.KAFKA_SASL_PASSWORD,
        })
    if settings.KAFKA_SSL_CA_LOCATION:
        config["ssl.ca.location"] = settings.KAFKA_SSL_CA_LOCATION
    return config


def producer(role="producer", **overrides):
    return Producer({
        **base_config(role),
        "enable.idempotence": True,
        "acks": "all",
        "linger.ms": 5,
        "compression.type": "lz4",
        "delivery.timeout.ms": settings.KAFKA_DELIVERY_TIMEOUT_MS,
        # Same key -> same partition as the Java clients (murmur2).
        "partitioner": "murmur2_random",
        **overrides,
    })


def consumer(group_id, **overrides):
    return Consumer({
        **base_config(group_id),
        "group.id": group_id,
        "enable.auto.commit": False,
        "enable.auto.offset.store": False,
        "auto.offset.reset": "earliest",
        "max.poll.interval.ms": settings.KAFKA_MAX_POLL_INTERVAL_MS,
        "session.timeout.ms": settings.KAFKA_SESSION_TIMEOUT_MS,
        "isolation.level": "read_committed",
        **overrides,
    })


def admin():
    return AdminClient({**base_config("admin"), "socket.timeout.ms": int(settings.KAFKA_ADMIN_TIMEOUT * 1000)})


def is_permanent(error):
    return error is not None and error.code() in PERMANENT_ERRORS


def headers_list(headers):
    return [(str(k), str(v).encode()) for k, v in (headers or {}).items() if v not in (None, "")]


def produce_sync(client, topic, value, key=None, headers=None, timeout=10.0):
    """Send one message and wait for the broker's acknowledgement.

    Returns ``(error, partition, offset)``; ``error`` is None on success, a
    ``KafkaError`` otherwise (a local timeout is ``_MSG_TIMED_OUT``).
    """
    result = {}

    def delivered(err, msg):
        result["error"] = err
        if err is None:
            result["partition"], result["offset"] = msg.partition(), msg.offset()

    try:
        client.produce(topic, value=value, key=key, headers=headers_list(headers), on_delivery=delivered)
    except BufferError:
        return KafkaError(KafkaError._QUEUE_FULL, "local producer queue is full"), None, None
    except KafkaException as exc:
        return exc.args[0], None, None
    client.flush(timeout)
    if "error" not in result:
        return KafkaError(KafkaError._MSG_TIMED_OUT, "delivery not confirmed in time"), None, None
    return result["error"], result.get("partition"), result.get("offset")


class _ProcessProducer:
    """One lazily created producer per process (Gunicorn forks workers; a
    librdkafka client must not cross a fork)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pid = None
        self._producer = None

    def get(self):
        with self._lock:
            if self._producer is None or self._pid != os.getpid():
                self._producer = producer("web", **{"message.timeout.ms": 10000, "queue.buffering.max.messages": 10000})
                self._pid = os.getpid()
            return self._producer

    def flush(self, timeout=2.0):
        with self._lock:
            if self._producer is not None and self._pid == os.getpid():
                self._producer.flush(timeout)

    def reset(self):
        with self._lock:
            self._producer = None
            self._pid = None


web_producer = _ProcessProducer()
