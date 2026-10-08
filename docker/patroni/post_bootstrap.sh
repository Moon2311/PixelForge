#!/bin/sh
# Runs once, on the node that initialises a brand-new cluster (Patroni's
# post_bootstrap; $1 is a superuser connection string). Creates the
# application role and database. pg_monitor lets the app read
# pg_stat_replication (replica LSNs and lag) for /api/health/database/.
set -eu

: "${APP_DB_NAME:?}" "${APP_DB_USER:?}" "${APP_DB_PASSWORD:?}"

psql "$1" -v ON_ERROR_STOP=1 -v db="$APP_DB_NAME" -v user="$APP_DB_USER" -v password="$APP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'user', :'password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'user')
\gexec
SELECT format('GRANT pg_monitor TO %I', :'user')
\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db')
\gexec
SQL
