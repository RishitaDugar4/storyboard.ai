#!/usr/bin/env bash
# Nightly Postgres dump with retention. Runs ON the server.
#
#   ssh HOST 'crontab -l 2>/dev/null; echo "17 3 * * * /opt/hbday-zee/deploy/backup.sh"' | ssh HOST crontab -
#
# A gift you cannot regenerate is worth more than the disk this costs: by the
# end, the database holds hours of curation -- approved stills, chosen clips,
# recorded narration -- that no amount of money re-creates identically.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/hbday-zee}
KEEP_DAYS=${KEEP_DAYS:-14}
STAMP=$(date +%Y%m%d-%H%M%S)
DEST="$APP_DIR/backups"

cd "$APP_DIR"
mkdir -p "$DEST"

docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml \
  exec -T db pg_dump -U hbz -d hbday_zee --format=custom \
  > "$DEST/hbday_zee-$STAMP.dump"

# A zero-byte dump is worse than none: it looks like a backup.
if [ ! -s "$DEST/hbday_zee-$STAMP.dump" ]; then
  echo "backup FAILED: empty dump" >&2
  rm -f "$DEST/hbday_zee-$STAMP.dump"
  exit 1
fi

find "$DEST" -name 'hbday_zee-*.dump' -mtime "+$KEEP_DAYS" -delete
echo "backup ok: $DEST/hbday_zee-$STAMP.dump ($(du -h "$DEST/hbday_zee-$STAMP.dump" | cut -f1))"
