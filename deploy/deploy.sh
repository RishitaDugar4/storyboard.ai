#!/usr/bin/env bash
# Push the working tree to the server and restart the stack.
#
#   ./deploy/deploy.sh root@your-host
#
# Deliberately rsync-over-SSH rather than a registry: one box, one developer,
# no CI. Adding a registry buys nothing here and costs a pipeline to maintain.
set -euo pipefail

HOST=${1:-${DEPLOY_HOST:-}}
APP_DIR=${APP_DIR:-/opt/hbday-zee}
[ -n "$HOST" ] || { echo "usage: $0 user@host   (or set DEPLOY_HOST)" >&2; exit 2; }

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

log "Checking the server is prepared"
ssh "$HOST" "test -f $APP_DIR/.env.prod" || {
  cat >&2 <<MSG
error: $APP_DIR/.env.prod is missing on $HOST.

  scp .env.prod.example $HOST:$APP_DIR/.env.prod
  ssh $HOST 'nano $APP_DIR/.env.prod'

Refusing to deploy without it: the stack would start with placeholder secrets.
MSG
  exit 1
}

log "Syncing source to $HOST:$APP_DIR"
rsync -az --delete \
  --exclude '.git' \
  --exclude '.env' --exclude '.env.prod' \
  --exclude 'node_modules' --exclude '.next' \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.venv' \
  --exclude 'apps/api/out' --exclude 'apps/api/.render-cache' \
  --exclude 'tools/bakeoff/out' --exclude 'tools/bakeoff/.venv' \
  --exclude 'storage' --exclude 'backups' \
  ./ "$HOST:$APP_DIR/"

log "Building and starting"
ssh "$HOST" "cd $APP_DIR && \
  docker compose --env-file .env.prod \
    -f docker-compose.yml -f docker-compose.prod.yml \
    up -d --build --remove-orphans"

log "Waiting for readiness"
ssh "$HOST" "cd $APP_DIR && for i in \$(seq 1 30); do
  if docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml \
       exec -T api python -c \"
import urllib.request, json, sys
b = json.load(urllib.request.urlopen('http://127.0.0.1:8000/readyz'))
sys.exit(0 if b['ready'] else 1)\" 2>/dev/null; then
    echo '  ready'; exit 0
  fi
  sleep 3
done
echo '  NOT ready after 90s -- check: docker compose logs api' >&2
exit 1"

log "Pruning old images"
ssh "$HOST" "docker image prune -f >/dev/null && docker system df | head -4"

SITE=$(ssh "$HOST" "grep -E '^SITE_ADDRESS=' $APP_DIR/.env.prod | cut -d= -f2" || true)
printf '\n  Deployed. https://%s\n\n' "${SITE:-<SITE_ADDRESS>}"
