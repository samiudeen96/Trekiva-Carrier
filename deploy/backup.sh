#!/usr/bin/env bash
# Nightly PostgreSQL backup. Cron example (as the deploy user):
#   15 2 * * * /opt/trekiva-logistics/deploy/backup.sh >> /var/log/trekiva-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-/var/backups/trekiva}"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"
# shellcheck disable=SC1091
set -a; source .env; set +a
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom > "$BACKUP_DIR/trekiva-$stamp.dump"
find "$BACKUP_DIR" -name 'trekiva-*.dump' -mtime +"$KEEP_DAYS" -delete
echo "backup ok: $BACKUP_DIR/trekiva-$stamp.dump"
