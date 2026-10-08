#!/bin/sh
# Allow streaming replication from the read replica (docker-compose.yml).
#
# Runs automatically when the primary's data volume is first created
# (/docker-entrypoint-initdb.d). Safe to run again, e.g. on a volume that
# existed before the replica was added:
#
#   docker compose exec postgres sh /docker-entrypoint-initdb.d/10-replication.sh
set -eu

: "${POSTGRES_REPLICATION_USER:?}"
: "${POSTGRES_REPLICATION_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v user="$POSTGRES_REPLICATION_USER" -v password="$POSTGRES_REPLICATION_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I WITH REPLICATION LOGIN PASSWORD %L', :'user', :'password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'user')
\gexec
SQL

rule="host replication $POSTGRES_REPLICATION_USER all scram-sha-256"
grep -qxF "$rule" "$PGDATA/pg_hba.conf" || echo "$rule" >> "$PGDATA/pg_hba.conf"
# Reload only when the server is running (not during the first initdb pass).
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -qAt -c "SELECT pg_reload_conf()" >/dev/null 2>&1 || true
