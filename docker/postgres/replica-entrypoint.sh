#!/bin/sh
# Read replica: on first start, clone the primary with pg_basebackup
# (--write-recovery-conf makes it a standby that streams WAL from the
# primary), then run PostgreSQL as a read-only hot standby.
set -eu

: "${PGDATA:=/var/lib/postgresql/data}"
: "${PRIMARY_HOST:?}"
: "${PRIMARY_PORT:=5432}"
: "${REPLICATION_USER:?}"
: "${PGPASSWORD:?}"  # the replication user's password, read by pg_basebackup

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "replica: cloning $PRIMARY_HOST:$PRIMARY_PORT"
  until pg_basebackup --host="$PRIMARY_HOST" --port="$PRIMARY_PORT" --username="$REPLICATION_USER" \
      --pgdata="$PGDATA" --wal-method=stream --write-recovery-conf --checkpoint=fast; do
    echo "replica: primary not ready, retrying in 2s"
    rm -rf "${PGDATA:?}"/*
    sleep 2
  done
  chmod 0700 "$PGDATA"
fi

exec postgres -c hot_standby=on
