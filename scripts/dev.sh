#!/usr/bin/env bash
# Bring the whole stack up and keep it in the foreground.
#
#   make dev              real providers (needs GEMINI_API_KEY / FAL_KEY)
#   make dev FAKE=1       fake providers, zero spend
#
# Ctrl-C stops everything it started.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

API_PORT=${API_PORT:-8000}
WEB_PORT=${WEB_PORT:-3000}
PIDS=()

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
cleanup() {
  echo
  log "stopping"
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

[ -f .env ] || { echo "no .env — copy .env.example and fill it in" >&2; exit 1; }
set -a; . ./.env; set +a

if [ "${FAKE:-0}" = "1" ]; then
  export AI_TEXT_PROVIDER=fake AI_IMAGE_PROVIDER=fake
  log "FAKE providers — nothing will be charged"
fi

log "dependencies (postgres + redis)"
docker compose up -d db redis >/dev/null 2>&1 || {
  echo "  docker is not running; start Docker Desktop" >&2; exit 1; }

log "migrations"
(cd apps/api && .venv/bin/alembic upgrade head >/dev/null)

if ! (cd apps/api && .venv/bin/python -m app.cli user list 2>/dev/null | grep -q "@"); then
  log "no accounts yet — create one with:  make user-add email=you@local name=You"
fi

export JOB_QUEUE=arq
log "api        http://localhost:${API_PORT}"
(cd apps/api && exec .venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port "$API_PORT" --reload) &
PIDS+=($!)

log "worker     arq (queue=arq)"
(cd apps/api && exec .venv/bin/arq app.jobs.worker.WorkerSettings) &
PIDS+=($!)

log "web        http://localhost:${WEB_PORT}"
(cd apps/web && exec npx next dev -p "$WEB_PORT") &
PIDS+=($!)

echo
log "up. open http://localhost:${WEB_PORT}  (Ctrl-C to stop)"
wait
