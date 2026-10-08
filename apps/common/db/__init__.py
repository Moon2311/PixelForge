"""PostgreSQL primary + read replica routing.

* ``router``      Django database router: writes and migrations -> primary,
                  replica-eligible reads -> ``context.choose_read_db()``.
* ``context``     per-request routing state (contextvars) and the read decision.
* ``consistency`` read-your-writes: commit LSNs recorded in Redis.
* ``replica``     replica health, replay LSN and lag (per worker, cached).
* ``middleware``  sets up the request state, records committed writes, and
                  retries a GET on the primary when the replica fails mid-request.
* ``metrics``     per-worker counters shown by /api/health/database/.

See README "Read replica".
"""

PRIMARY = "default"
REPLICA = "replica"
