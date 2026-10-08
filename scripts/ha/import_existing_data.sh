#!/usr/bin/env bash
# Copy an existing single-node PixelForge database into the HA cluster
# (docker-compose.ha.yml), through its writer endpoint. Safe by construction:
#
#   * the source is only read (pg_dump);
#   * the target database must be empty (no tables in "public"): start the
#     cluster *without* the backend, so Django hasn't migrated it yet;
#   * replicas receive the data through normal replication.
#
# Usage, e.g. from the plain docker-compose.yml stack (its volume is untouched):
#
#   docker compose up -d postgres                            # existing data, port 5433
#   docker compose -f docker-compose.ha.yml up -d --build etcd1 etcd2 etcd3 pg1 pg2 pg3 pg4 haproxy reconciler
#   SOURCE_URL=postgres://pixelforge:pixelforge@127.0.0.1:5433/pixelforge scripts/ha/import_existing_data.sh
#   docker compose -f docker-compose.ha.yml up -d            # backend: migrate is then a no-op
#
# TARGET_URL defaults to the HA writer endpoint (127.0.0.1:5000). pg_dump and
# psql run from the postgres:16 image (host network), so the client matches
# the server version.
set -euo pipefail

: "${SOURCE_URL:?Set SOURCE_URL to the existing database (postgres://user:pass@host:port/db)}"
TARGET_URL="${TARGET_URL:-postgres://pixelforge:${HA_APP_DB_PASSWORD:-pixelforge}@127.0.0.1:${HA_WRITER_PORT:-5000}/pixelforge}"
PG=(docker run --rm -i --network host postgres:16)

tables="$("${PG[@]}" psql "$TARGET_URL" -qAt -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
if [ "$tables" != "0" ]; then
    echo "The target database already has $tables table(s); refusing to import (nothing was changed)." >&2
    echo "Import before the HA backend runs its migrations, into a fresh cluster." >&2
    exit 1
fi

echo "Copying schema and data (source is only read)..."
"${PG[@]}" pg_dump --no-owner --no-privileges "$SOURCE_URL" \
    | "${PG[@]}" psql "$TARGET_URL" --quiet --set ON_ERROR_STOP=1 --single-transaction --output=/dev/null

echo "Imported $("${PG[@]}" psql "$TARGET_URL" -qAt -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") tables."
echo "Now start the backend: docker compose -f docker-compose.ha.yml up -d"
