#!/usr/bin/env bash
# Copy data from the three pre-monolith databases into the single PixelForge
# database. Safe by construction:
#   * the source databases are only read (pg_dump --data-only);
#   * the target must already be migrated (python manage.py migrate) and the
#     copied tables must be empty, otherwise the script aborts before writing;
#   * table names are unchanged (models pin db_table="apps_<model>"), so rows
#     are copied as-is. Columns added since (e.g. search stats) take their
#     database defaults and are rebuilt at the end.
#
# Usage (any source may be omitted):
#   AUTH_DATABASE_URL=postgres://user:pass@host:5432/auth-service \
#   CATALOG_DATABASE_URL=postgres://user:pass@host:5432/product_catalog_db \
#   CART_DATABASE_URL=postgres://user:pass@host:5432/cart_db \
#   DATABASE_URL=postgres://user:pass@host:5432/pixelforge \
#   scripts/merge_legacy_databases.sh
#
# DATABASE_URL defaults to the value in ./.env. Run from the repository root.
set -euo pipefail

cd "$(dirname "$0")/.."
# Run Django through uv when available (override with PYTHON=...).
if [[ -z "${PYTHON:-}" ]]; then
  if command -v uv >/dev/null; then PYTHON="uv run python"; else PYTHON=python; fi
fi

if [[ -z "${DATABASE_URL:-}" && -f .env ]]; then
  DATABASE_URL="$(grep -E '^DATABASE_URL=' .env | cut -d= -f2-)"
fi
: "${DATABASE_URL:?Set DATABASE_URL (target database)}"

AUTH_TABLES=(auth_user apps_role apps_userprofile)
CATALOG_TABLES=(
  apps_category apps_subcategory apps_brand apps_attribute apps_attributevalue
  apps_product apps_productimage apps_productvariant apps_productattributevalue
  apps_variantattributevalue apps_productspecification apps_variantprice
  apps_inventory apps_inventorylog apps_lowstockalert apps_productreview
  apps_recentlyviewed apps_banner
)
CART_TABLES=(apps_cart apps_cartitem apps_guestcart apps_guestcartitem)

existing_tables() {  # $1=db url, rest=candidate tables -> tables that exist in $1
  local url="$1"; shift
  local list; list=$(printf "'%s'," "$@"); list="${list%,}"
  psql "$url" -Atc "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN ($list)"
}

ensure_empty() {  # target tables must be empty
  for table in "$@"; do
    local n; n=$(psql "$DATABASE_URL" -Atc "SELECT count(*) FROM \"$table\"")
    if [[ "$n" != "0" ]]; then
      echo "Target table $table already has $n row(s); refusing to merge." >&2
      exit 1
    fi
  done
}

declare -a PLAN_URLS=() PLAN_TABLES=()
plan() {  # $1=label $2=url, rest=tables
  local label="$1" url="$2"; shift 2
  [[ -z "$url" ]] && { echo "- $label: skipped (no URL)"; return; }
  mapfile -t found < <(existing_tables "$url" "$@")
  echo "- $label: ${#found[@]} table(s): ${found[*]}"
  ensure_empty "${found[@]}"
  PLAN_URLS+=("$url"); PLAN_TABLES+=("${found[*]}")
}

echo "Checking sources and target..."
plan auth "${AUTH_DATABASE_URL:-}" "${AUTH_TABLES[@]}"
plan catalog "${CATALOG_DATABASE_URL:-}" "${CATALOG_TABLES[@]}"
plan cart "${CART_DATABASE_URL:-}" "${CART_TABLES[@]}"

for i in "${!PLAN_URLS[@]}"; do
  args=()
  for table in ${PLAN_TABLES[$i]}; do args+=(--table="public.$table"); done
  echo "Copying: ${PLAN_TABLES[$i]}"
  pg_dump --data-only --no-owner --no-privileges "${args[@]}" "${PLAN_URLS[$i]}" \
    | psql --quiet --output=/dev/null --set ON_ERROR_STOP=1 --single-transaction "$DATABASE_URL"
done

echo "Resetting sequences..."
DATABASE_URL="$DATABASE_URL" $PYTHON manage.py sqlsequencereset auth authentication catalog cart \
  | psql --quiet --output=/dev/null --set ON_ERROR_STOP=1 "$DATABASE_URL"

echo "Rebuilding product search stats..."
DATABASE_URL="$DATABASE_URL" $PYTHON manage.py refresh_product_stats

echo "Done."
