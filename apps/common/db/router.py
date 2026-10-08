"""Database router: the writer (``default``, the current primary) takes every
write and every migration; reads of ``READ_REPLICA_APPS`` models may use a
replica from the reader pool (``context.choose_read_db``).

Only the public catalog (``catalog`` app, which the search module also
reads) is replica-eligible. Auth, sessions, admin, cart, orders and
payments always read the primary: they write inside GET requests (carts,
payment status checks), handle money, or are updated by callers that can't
be tied to the reader (payment provider callbacks).

Replica connections additionally refuse write statements (and are opened
with ``default_transaction_read_only=on``; a hot standby is read-only
anyway), so a routing mistake fails loudly instead of being attempted.
"""

from django.conf import settings
from django.db import DatabaseError
from django.db.backends.signals import connection_created
from django.dispatch import receiver

from apps.common.db import PRIMARY, context, metrics

_WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE")


def is_write_sql(sql):
    return isinstance(sql, str) and sql.lstrip()[:8].upper().startswith(_WRITE_PREFIXES)


class ReplicaWriteError(DatabaseError):
    """A write statement reached the read replica connection."""


class PrimaryReplicaRouter:
    def db_for_read(self, model, **hints):
        if model._meta.app_label in settings.READ_REPLICA_APPS:
            return context.choose_read_db()
        return PRIMARY

    def db_for_write(self, model, **hints):
        metrics.incr("db_writer_routing_total")
        return PRIMARY

    def allow_relation(self, obj1, obj2, **hints):
        # The writer and every replica hold the same data.
        if {obj1._state.db, obj2._state.db} <= {PRIMARY, *settings.READ_REPLICA_ALIASES}:
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == PRIMARY


def reject_writes(execute, sql, params, many, context):
    if is_write_sql(sql):
        raise ReplicaWriteError("Refusing to send a write statement to the read replica")
    return execute(sql, params, many, context)


@receiver(connection_created)
def _guard_replica_connection(sender, connection, **kwargs):
    # Every alias other than the writer is a read replica in this project.
    if connection.alias != PRIMARY and reject_writes not in connection.execute_wrappers:
        connection.execute_wrappers.append(reject_writes)
