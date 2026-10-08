"""Request side of read routing (see apps.common.db.context).

For each request, when a replica is configured:

* the routing state is set up: unsafe methods read the primary, and the
  caller's read-your-writes identity comes from its headers;
* every statement on the primary connection is observed. A write counts once
  it has committed: inside ``atomic()`` through ``on_commit`` (a rollback
  discards it), in autocommit mode immediately;
* after the response, if a write committed, the primary's LSN is recorded
  for the caller (``consistency.mark_recent_writes``);
* a safe request that read from a replica and failed (5xx) while that
  replica is down is run again once, on the primary (the failed replica is
  out of the pool for the failure cooldown).
"""

from django.db import connections

from apps.common.db import PRIMARY, consistency, context, metrics, replica
from apps.common.db.router import is_write_sql

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _observe_writes(execute, sql, params, many, ctx):
    result = execute(sql, params, many, ctx)
    state = context.current()
    if state is not None and is_write_sql(sql):
        connection = ctx["connection"]
        if connection.in_atomic_block:
            connection.on_commit(state.note_write)
        else:
            state.note_write()
    return result


def _write_identities(request, state):
    identities = [state.identity]
    # DRF stores the authenticated user on the Django request; Django admin
    # (session login) via AuthenticationMiddleware. Mark the user as well, so
    # an admin who edits in one client reads their write from another.
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        identities.append(consistency.user_key(user.pk))
    return list(dict.fromkeys(i for i in identities if i))


class ReadReplicaMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not replica.configured():
            return self.get_response(request)

        state = context.RequestState(
            pinned=request.method not in SAFE_METHODS,
            identity=consistency.request_key(request),
        )
        with context.request_scope(state), connections[PRIMARY].execute_wrapper(_observe_writes):
            response = self.get_response(request)
            if self._should_retry(request, state, response):
                metrics.incr("db_replica_retries_total")
                metrics.event("warning", "replica_request_retried", replica=state.used_replica,
                              method=request.method, path=request.path)
                retry = context.RequestState(pinned=True, identity=state.identity)
                with context.request_scope(retry):
                    response = self.get_response(request)
                state.wrote = state.wrote or retry.wrote

        if state.wrote:
            consistency.mark_recent_writes(_write_identities(request, state))
        return response

    @staticmethod
    def _should_retry(request, state, response):
        """Retry only safe requests that used a replica, failed, and found
        that replica down on a fresh check (an application error on a
        healthy replica is returned as it is)."""
        if response.status_code < 500 or not state.used_replica or request.method not in SAFE_METHODS:
            return False
        connections[state.used_replica].close()  # probe on a fresh connection, not the one that failed
        return replica.check(state.used_replica).status != replica.OK
