#!/usr/bin/env bash
# copy_db_to_prod.sh
# Usage: bash NightCityBot/scripts/copy_db_to_prod.sh <PROD_DATABASE_URL>
#
# Dumps the current dev database (DATABASE_URL) and restores it into the
# production database supplied as the first argument.
# Safe to re-run: restore uses --clean so the target is reset first.

set -euo pipefail

PROD_URL="${1:-}"
if [[ -z "$PROD_URL" ]]; then
  echo "Usage: bash NightCityBot/scripts/copy_db_to_prod.sh <PROD_DATABASE_URL>"
  exit 1
fi

DEV_URL="${DATABASE_URL:-}"
if [[ -z "$DEV_URL" ]]; then
  echo "ERROR: DATABASE_URL is not set in the environment."
  exit 1
fi

DUMP_FILE="$(mktemp /tmp/ncbot_db_XXXXXX.dump)"
trap 'rm -f "$DUMP_FILE"' EXIT

echo "==> Dumping dev database..."
pg_dump --format=custom --no-owner --no-acl "$DEV_URL" -f "$DUMP_FILE"
echo "    Dump size: $(du -sh "$DUMP_FILE" | cut -f1)"

echo "==> Restoring to production database..."
pg_restore --format=custom --no-owner --no-acl \
  --clean --if-exists \
  --single-transaction \
  -d "$PROD_URL" "$DUMP_FILE"

echo "==> Done. Dev database has been copied to production."
