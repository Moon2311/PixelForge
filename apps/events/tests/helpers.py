"""Test doubles for the Kafka clients and helpers to register test tasks.

Unit tests drive ``Worker.handle`` and ``OutboxPublisher`` with these fakes;
``test_kafka_integration`` runs the same code against a real broker.
"""

import json

from confluent_kafka import KafkaError

from apps.events import envelope, registry, topics
from apps.events.registry import Task


class FakeMessage:
    def __init__(self, topic, value, partition=0, offset=0, key=None, error=None):
        self._topic, self._value, self._partition = topic, value, partition
        self._offset, self._key, self._error = offset, key, error

    def topic(self):
        return self._topic

    def partition(self):
        return self._partition

    def offset(self):
        return self._offset

    def key(self):
        return self._key

    def value(self):
        return self._value

    def error(self):
        return self._error


class FakeConsumer:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.commits, self.paused, self.resumed, self.seeks = [], [], [], []
        self.closed = False

    def subscribe(self, topics, **callbacks):
        self.subscribed = topics

    def poll(self, timeout):
        return self.messages.pop(0) if self.messages else None

    def commit(self, message=None, asynchronous=True):
        self.commits.append((message.topic(), message.partition(), message.offset()))

    def pause(self, partitions):
        self.paused.extend((p.topic, p.partition) for p in partitions)

    def resume(self, partitions):
        self.resumed.extend((p.topic, p.partition) for p in partitions)

    def seek(self, partition):
        self.seeks.append((partition.topic, partition.partition, partition.offset))

    def close(self):
        self.closed = True


class _Delivered:
    def __init__(self, topic, partition, offset):
        self._topic, self._partition, self._offset = topic, partition, offset

    def topic(self):
        return self._topic

    def partition(self):
        return self._partition

    def offset(self):
        return self._offset


class FakeProducer:
    """Records produced messages. ``fail`` = a KafkaError code makes every
    delivery fail with it; ``silent`` drops deliveries (no callback)."""

    def __init__(self, fail=None, silent=False):
        self.fail, self.silent = fail, silent
        self.sent, self._pending = [], []

    def produce(self, topic, value=None, key=None, headers=None, on_delivery=None):
        self._pending.append((topic, value, key, headers, on_delivery))

    def poll(self, timeout=0):
        return 0

    def flush(self, timeout=None):
        pending, self._pending = self._pending, []
        for topic, value, key, headers, callback in pending:
            if self.silent:
                continue
            if self.fail is not None:
                callback(KafkaError(self.fail, "test failure"), _Delivered(topic, None, None))
                continue
            offset = len(self.sent)
            self.sent.append({"topic": topic, "value": json.loads(value), "key": key,
                              "headers": dict(headers or [])})
            callback(None, _Delivered(topic, 0, offset))
        return 0

    def on(self, logical_topic):
        return [m["value"] for m in self.sent if m["topic"] == topics.name(logical_topic)]


def event_message(event_type, payload=None, aggregate_id="A1", **kwargs):
    return envelope.new_event(event_type, "test", aggregate_id, payload or {}, **kwargs)


def kafka_message(message, logical_topic=topics.ORDER_EVENTS, offset=0, partition=0, key=b"A1"):
    return FakeMessage(topics.name(logical_topic), envelope.dumps(message), partition, offset, key)


class TaskRegistrationMixin:
    """``self.register(group, event_type, func, **policy)`` adds a task for
    the test only."""

    def register(self, group, event_type, func, **policy):
        key = (group, event_type)
        previous = registry._tasks.get(key)
        registry._tasks[key] = Task(f"test.{func.__name__}", group, event_type, func, tuple(sorted(policy.items())))

        def restore():
            if previous is None:
                registry._tasks.pop(key, None)
            else:
                registry._tasks[key] = previous

        self.addCleanup(restore)


class Flaky:
    """A task that raises ``error`` for the first ``failures`` calls."""

    def __init__(self, failures, error=None):
        self.failures, self.error = failures, error
        self.calls = []
        self.__name__ = "flaky"

    def __call__(self, event):
        self.calls.append(event.attempt)
        if len(self.calls) <= self.failures:
            raise self.error or ConnectionError("smtp down")
