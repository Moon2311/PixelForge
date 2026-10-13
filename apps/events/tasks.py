"""Tasks of the audit and DLQ archiver consumer groups."""

from . import dlq
from .outbox import record_audit
from .registry import task


@task("audit", "*", name="audit.record")
def record(event):
    record_audit(event)


@task("dlq-archiver", "*", name="dlq.archive")
def archive_dead_letter(event):
    dlq.archive(event)
