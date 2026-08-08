#!/usr/bin/env bash
# Nightly Postgres backup for a docker-compose deployment (see
# docker-compose.prod.yml / README.md "Backups"). Dumps via `pg_dump` inside
# the running postgres container (works whether or not the container's port
# is published to the host) and gzips the result to a host-side directory --
# deliberately NOT inside the postgres_data Docker volume, so a mistaken
# `docker volume rm`/`down -v` on the live database can't take the backups
# down with it. Dumps with `--clean --if-exists` so the file is a
# self-contained restore (see restore.sh): it drops and recreates every
# object itself, no separate empty-database setup needed first.
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="${HMS_BACKUP_DIR:-$(pwd)/backups}"
RETENTION_DAYS="${HMS_BACKUP_RETENTION_DAYS:-14}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="$BACKUP_DIR/hms_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U hms -d hms --clean --if-exists | gzip > "$OUT_FILE"

find "$BACKUP_DIR" -name 'hms_*.sql.gz' -mtime "+$RETENTION_DAYS" -delete

echo "backup.sh: wrote $OUT_FILE ($(du -h "$OUT_FILE" | cut -f1))"
