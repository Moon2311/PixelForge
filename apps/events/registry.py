"""Consumer groups and their tasks.

A consumer group is one independent subscriber: every group sees every
message of its topics, and Kafka spreads a group's partitions over its
running workers (``kafka_worker <group>``; at most one worker per partition
does work, extra workers stand by).

Tasks are registered in each app's ``tasks.py`` (imported at startup)::

    @task("notifications", "order.placed")
    def send_order_confirmation(event): ...

One task per (group, event type); ``"*"`` matches every event type of the
group's topics. Policy overrides (``max_attempts=``, ``timeout=``, ...)
apply to that task only.
"""

from dataclasses import dataclass, field

from django.conf import settings

from . import topics
from .retry import RetryPolicy


@dataclass(frozen=True)
class Group:
    name: str
    topics: tuple
    description: str
    # False: a failed task isn't sent to the retry/DLQ topics; the message
    # is redelivered after a backoff instead (the DLQ archiver itself).
    retries: bool = True
    # The message kind this group processes (dead letters for the archiver).
    message_type: str = "event"
    # Deduplicate on the event id (business tasks) or on the message id
    # (the archiver: one event can be dead-lettered again after a replay).
    dedupe_on: str = "event_id"


GROUPS = {
    group.name: group
    for group in (
        Group(
            "notifications",
            (topics.ORDER_EVENTS, topics.PAYMENT_EVENTS, topics.INVENTORY_EVENTS, topics.NOTIFICATION_EVENTS),
            "Customer and staff emails (orders, payments, low stock, passwords)",
        ),
        Group("images", (topics.IMAGE_PROCESSING,), "Product image optimisation"),
        Group(
            "payments",
            (topics.PAYMENT_PROCESSING,),
            "Payment verification with JazzCash/Easypaisa (circuit breaker, retries, DLQ)",
        ),
        Group(
            "audit",
            (topics.ORDER_EVENTS, topics.PAYMENT_EVENTS, topics.INVENTORY_EVENTS, topics.AUDIT_EVENTS),
            "Audit trail in PostgreSQL",
        ),
        Group(
            "dlq-archiver",
            (topics.DEAD_LETTER_EVENTS,),
            "Archives dead letters for the replay workflow",
            retries=False,
            message_type="dead_letter",
            dedupe_on="message_id",
        ),
    )
}


def group_id(name):
    """The Kafka consumer group id, e.g. ``pixelforge-notifications``."""
    return f"{settings.KAFKA_CONSUMER_GROUP_PREFIX}-{name}"


@dataclass(frozen=True)
class Task:
    name: str
    group: str
    event_type: str
    func: object = field(compare=False)
    policy_overrides: tuple = ()

    def policy(self):
        return RetryPolicy.from_settings(**dict(self.policy_overrides))

    def __call__(self, event):
        return self.func(event)


_tasks = {}


def task(group, event_types, name=None, **policy):
    if group not in GROUPS:
        raise ValueError(f"Unknown consumer group {group!r}")
    if isinstance(event_types, str):
        event_types = (event_types,)

    def decorator(func):
        task_name = name or f"{func.__module__.rsplit('.', 2)[-2]}.{func.__name__}"
        for event_type in event_types:
            key = (group, event_type)
            existing = _tasks.get(key)
            if existing is not None and existing.func is not func:
                raise ValueError(f"{group}/{event_type} already handled by {existing.name}")
            _tasks[key] = Task(task_name, group, event_type, func, tuple(sorted(policy.items())))
        return func

    return decorator


def find(group, event_type):
    return _tasks.get((group, event_type)) or _tasks.get((group, "*"))


def tasks():
    return dict(_tasks)


def groups_for_topic(logical_topic):
    return [group for group in GROUPS.values() if logical_topic in group.topics]
