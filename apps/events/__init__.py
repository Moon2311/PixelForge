"""Asynchronous events: transactional outbox -> Kafka -> consumer workers.

* ``outbox``     ``enqueue()`` writes an event in the caller's transaction;
                 ``audit()`` sends a best-effort audit event (no outbox).
* ``publisher``  ``outbox_publisher``: publishes pending outbox rows to Kafka
                 and marks them published after the broker acknowledges them.
* ``worker``     ``kafka_worker <group>``: consumer group loop with
                 idempotency, timeouts, retry-topic scheduling and the DLQ.
* ``processing`` runs one task for one event (dedup, timeout, classification);
                 shared by the worker and by inline mode.
* ``inline``     no Kafka (EVENTS_BACKEND=inline): outbox rows are processed
                 in-process after commit.
* ``registry``   consumer groups, their topics and their tasks
                 (``@task(...)`` in each app's ``tasks.py``).
* ``retry``      retry policy (exponential backoff, jitter, limits) and error
                 classification.
* ``dlq``        dead-letter archive and the explicit replay workflow.
* ``health``     outbox backlog, broker, consumer lag, heartbeats, DLQ counts.

Delivery is at least once. Consumers are idempotent (``ProcessedEvent`` and
each task's own unique records); see README "Background tasks (Kafka)".
"""
