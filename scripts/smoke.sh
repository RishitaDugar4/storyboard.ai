#!/usr/bin/env bash
# Drive the whole product end to end against a running stack and report.
#
#   make smoke                 uses whatever providers .env selects
#   make smoke FAKE=1          zero spend
#
# Exercises: login, project, story, analyse, storyboard, apply, character lock,
# still generation, candidate selection, freshness, and the SSE stream.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
set -a; . ./.env; set +a

BASE=${BASE:-http://localhost:3000}
EMAIL=${EMAIL:-}
PASS=${PASS:-}
JAR=$(mktemp)
FAILED=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; FAILED=$((FAILED+1)); }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
api()  { curl -sS --max-time 90 -b "$JAR" -c "$JAR" "$@"; }
jq_()  { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)" 2>/dev/null; }

if [ -z "$EMAIL" ] || [ -z "$PASS" ]; then
  echo "set EMAIL and PASS, e.g.:"
  echo "  make smoke EMAIL=you@local PASS='your-passphrase'"
  exit 2
fi

step "reachability"
code=$(curl -sS --max-time 10 -o /dev/null -w "%{http_code}" "$BASE/login" || echo 000)
[ "$code" = "200" ] && ok "web  $BASE" || { bad "web unreachable ($code) — is 'make dev' running?"; exit 1; }
ready=$(api "$BASE/readyz" | jq_ "d['ready']")
[ "$ready" = "True" ] && ok "api ready (db + ffmpeg)" || bad "api not ready"

step "auth"
code=$(api -o /dev/null -w "%{http_code}" -X POST "$BASE/api/v1/auth/session" \
  -H 'content-type: application/json' -d "{\"email\":\"$EMAIL\",\"passphrase\":\"$PASS\"}")
[ "$code" = "204" ] && ok "logged in as $EMAIL" || { bad "login failed ($code)"; exit 1; }

step "project + story"
PID=$(api -X POST "$BASE/api/v1/projects" -H 'content-type: application/json' \
  -d '{"title":"Smoke test"}' | jq_ "d['id']")
[ -n "$PID" ] && ok "project ${PID:0:8}…" || { bad "could not create project"; exit 1; }
api -o /dev/null -X PUT "$BASE/api/v1/projects/$PID/story" \
  -H 'content-type: application/json' \
  -d '{"raw_text":"A keeper kept a light for forty years. One winter night the power failed and she turned the lens by hand until dawn, and eleven men came home who otherwise would not have."}'
ok "story saved"

wait_job() {  # $1 = job id, $2 = label
  for _ in $(seq 1 90); do
    st=$(api "$BASE/api/v1/jobs/$1" | jq_ "d['status']")
    case "$st" in
      succeeded) ok "$2"; return 0 ;;
      failed|cancelled)
        detail=$(api "$BASE/api/v1/jobs/$1" | jq_ "(d['error_code'] or '')+' '+((d['error_detail'] or '')[:120])")
        bad "$2 — $detail"; return 1 ;;
    esac
    sleep 2
  done
  bad "$2 — timed out"; return 1
}

step "pipeline"
JOB=$(api -X POST "$BASE/api/v1/projects/$PID/story:analyze" | jq_ "d['job_id']")
wait_job "$JOB" "story analysed"
JOB=$(api -X POST "$BASE/api/v1/projects/$PID/storyboard:generate" \
  -H 'content-type: application/json' -d '{"target_length_s":90}' | jq_ "d['job_id']")
wait_job "$JOB" "storyboard generated"

SB=$(api "$BASE/api/v1/projects/$PID/storyboards" | jq_ "d['items'][0]['id']")
RES=$(api -X POST "$BASE/api/v1/projects/$PID/storyboards/${SB}:apply" \
  -H 'content-type: application/json' -d '{}')
SCENES=$(echo "$RES" | jq_ "d['scenes']")
[ -n "$SCENES" ] && ok "applied: $SCENES scenes, $(echo "$RES" | jq_ "d['shots']") shots" \
                 || bad "apply failed"

step "cast"
CID=$(api "$BASE/api/v1/projects/$PID/characters" | jq_ "d['items'][0]['id']")
LOCKED=$(api -X POST "$BASE/api/v1/characters/${CID}:lock" | jq_ "d['locked']")
[ "$LOCKED" = "True" ] && ok "character locked (canon frozen)" || bad "lock failed"
code=$(api -o /dev/null -w "%{http_code}" -X PATCH "$BASE/api/v1/characters/$CID" \
  -H 'content-type: application/json' -d '{"name":"Nope"}')
[ "$code" = "409" ] && ok "locked character rejects edits" || bad "expected 409, got $code"

step "stills"
SHOT=$(api "$BASE/api/v1/projects/$PID/shots" | jq_ "d['items'][0]['id']")
PROMPT=$(api "$BASE/api/v1/shots/$SHOT/prompt")
NFRAG=$(echo "$PROMPT" | jq_ "len(d['fragments'])")
[ "${NFRAG:-0}" -gt 3 ] && ok "prompt composed from $NFRAG fragments" || bad "prompt looks empty"
JOB=$(api -X POST "$BASE/api/v1/shots/$SHOT/image:generate" \
  -H 'content-type: application/json' -d '{"n":2}' | jq_ "d['job_id']")
wait_job "$JOB" "2 candidates generated"

CANDS=$(api "$BASE/api/v1/shots/$SHOT/images" | jq_ "len(d['items'])")
[ "${CANDS:-0}" -ge 2 ] && ok "$CANDS candidates listed" || bad "expected candidates"
OTHER=$(api "$BASE/api/v1/shots/$SHOT/images" | jq_ "[i['id'] for i in d['items'] if not i['selected']][0]")
[ -n "$OTHER" ] && api -o /dev/null -X POST "$BASE/api/v1/shots/$SHOT/image:select" \
  -H 'content-type: application/json' -d "{\"asset_id\":\"$OTHER\"}" && ok "approved a different candidate"
FRESH=$(api "$BASE/api/v1/projects/$PID/shots" | jq_ "d['items'][0]['still_fresh']")
[ "$FRESH" = "True" ] && ok "still reports fresh" || bad "freshness wrong ($FRESH)"

step "narration"
api -o /dev/null -X POST "$BASE/api/v1/projects/$PID/narration:generate_all"
for _ in $(seq 1 120); do
  PENDING=$(api "$BASE/api/v1/projects/$PID/jobs?status=active" | jq_ "d['total']")
  [ "${PENDING:-1}" = "0" ] && break
  sleep 2
done
NARR=$(api "$BASE/api/v1/projects/$PID/narration")
RECORDED=$(echo "$NARR" | jq_ "sum(1 for i in d['items'] for l in i['lines'] if l['duration_ms'])")
LINES=$(echo "$NARR" | jq_ "sum(1 for i in d['items'] for l in i['lines'])")
[ "${RECORDED:-0}" -gt 0 ] && ok "$RECORDED/$LINES lines recorded with measured durations" \
                           || bad "no narration recorded"
UNKNOWN=$(echo "$NARR" | jq_ "sum(1 for i in d['items'] if i['fit']['status']=='unknown')")
[ "${UNKNOWN:-1}" = "0" ] && ok "fit measured for every shot" || bad "$UNKNOWN shots still unmeasured"

step "the film"
# Every shot needs a still before the renderer will accept the timeline.
for SH in $(api "$BASE/api/v1/projects/$PID/shots" | jq_ "' '.join(i['id'] for i in d['items'] if not i['still'])"); do
  api -o /dev/null -X POST "$BASE/api/v1/shots/$SH/image:generate" \
    -H 'content-type: application/json' -d '{"n":1}'
done
for _ in $(seq 1 150); do
  PENDING=$(api "$BASE/api/v1/projects/$PID/jobs?status=active" | jq_ "d['total']")
  [ "${PENDING:-1}" = "0" ] && break
  sleep 2
done
PRE=$(api -X POST "$BASE/api/v1/projects/$PID/preflight" \
  -H 'content-type: application/json' -d '{"profile":"preview"}')
[ "$(echo "$PRE" | jq_ "d['ok']")" = "True" ] && ok "preflight passed ($(echo "$PRE" | jq_ "d['clips']") shots)" \
  || bad "preflight: $(echo "$PRE" | jq_ "'; '.join(b['message'] for b in d['blocking'])")"

JOB=$(api -X POST "$BASE/api/v1/projects/$PID/renders" \
  -H 'content-type: application/json' -d '{"profile":"preview"}' | jq_ "d['job_id']")
wait_job "$JOB" "preview rendered"
REND=$(api "$BASE/api/v1/projects/$PID/renders" | jq_ "d['items'][0]")
VIDEO=$(api "$BASE/api/v1/projects/$PID/renders" | jq_ "d['items'][0]['video_url'] or ''")
DUR=$(api "$BASE/api/v1/projects/$PID/renders" | jq_ "round((d['items'][0]['duration_ms'] or 0)/1000)")
if [ -n "$VIDEO" ]; then
  code=$(api -o /dev/null -w "%{http_code}" "$BASE$VIDEO")
  [ "$code" = "200" ] && ok "film is downloadable (${DUR}s)" || bad "video URL returned $code"
else
  bad "no video produced"
fi

step "live events"
EV=$(curl -sS --max-time 4 -N -b "$JAR" "$BASE/api/v1/projects/$PID/events" 2>/dev/null | head -2)
echo "$EV" | grep -q "connected" && ok "SSE stream connected" || bad "SSE did not connect"

step "spend"
COST=$(api "$BASE/api/v1/projects/$PID" | jq_ "d['spent_cents']")
printf "  this run cost %s¢\n" "${COST:-?}"
printf "\n  open %s/projects/%s\n" "$BASE" "$PID"

echo
if [ "$FAILED" -eq 0 ]; then
  printf '\033[32mall checks passed\033[0m\n'
else
  printf '\033[31m%d check(s) failed\033[0m\n' "$FAILED"
fi
rm -f "$JAR"
exit "$FAILED"
