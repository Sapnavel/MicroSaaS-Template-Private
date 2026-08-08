#!/usr/bin/env bash
# Restore a backup produced by backup.sh. DESTRUCTIVE: the dump was taken
# with `pg_dump --clean --if-exists`, so replaying it drops and recreates
# every table/policy/etc. in the target database before repopulating it --
# double check the filename before running this against a live deployment.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ $# -ne 1 ]; then
  echo "usage: $0 <path-to-hms_YYYYMMDD_HHMMSS.sql.gz>" >&2
  exit 1
fi
BACKUP_FILE="$1"

gunzip -c "$BACKUP_FILE" | docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres \
  psql -U hms -d hms

echo "restore.sh: restored $BACKUP_FILE"
