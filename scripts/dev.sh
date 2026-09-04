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

# Fail fast on a port that is already taken. Without this the child dies with
# "address already in use", `wait` keeps waiting on its siblings, and the whole
# thing looks like a hang rather than a conflict.
port_holder() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1; }
check_port() {
  local port=$1 label=$2 pid
  pid=$(port_holder "$port") || true
  [ -z "$pid" ] && return 0
  local what
  what=$(ps -o command= -p "$pid" 2>/dev/null | cut -c1-70)
  cat >&2 <<MSG

  port $port ($label) is already in use by pid $pid:
    $what

  Another 'make dev' is probably still running. Either stop it (Ctrl-C in its
  terminal, or 'make dev-stop'), or pick different ports:
    API_PORT=8001 WEB_PORT=3101 make dev

MSG
  return 1
}

[ -f .env ] || { echo "no .env — copy .env.example and fill it in" >&2; exit 1; }
set -a; . ./.env; set +a

if [ "${FAKE:-0}" = "1" ]; then
  # Every capability, not just the ones that existed when this was written.
  # A partial list makes the "nothing will be charged" promise a lie, and the
  # missing one only shows up as a rate limit on a real account.
  export AI_TEXT_PROVIDER=fake AI_IMAGE_PROVIDER=fake AI_SPEECH_PROVIDER=fake
  export AI_VIDEO_PROVIDER=fake
  log "FAKE providers (text, image, speech, video) — nothing will be charged"
fi

failed=0
check_port "$API_PORT" api || failed=1
check_port "$WEB_PORT" web || failed=1
[ "$failed" -eq 0 ] || exit 1

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

# Exit as soon as ANY child dies, rather than waiting on the survivors while
# the stack is quietly broken.
while :; do
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo >&2
      echo "  a service exited (pid $pid) — stopping the rest." >&2
      echo "  scroll up for its error, or check: docker compose ps" >&2
      exit 1
    fi
  done
  sleep 2
done
