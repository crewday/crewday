#!/usr/bin/env bash
# Dev-stack status digest: compose health, /readyz, /healthz, alembic
# current vs head, git branch + dirty count. Exit 0 when the stack is
# ready to use; non-zero means fix the environment first.
#
# Env: AGENT_STATUS_BASE_URL (http://127.0.0.1:8100),
# AGENT_STATUS_COMPOSE (docker-compose.dev.yml).

set -uo pipefail

base_url="${AGENT_STATUS_BASE_URL:-http://127.0.0.1:8100}"
compose_file="${AGENT_STATUS_COMPOSE:-docker-compose.dev.yml}"

problems=0
app_api_state=""
app_api_health=""

# Compose v2 emits one JSON object per line; jq -s slurps to an array.
if compose_json="$(docker compose -f "$compose_file" ps --format json 2>/dev/null)"; then
  total="$(printf '%s\n' "$compose_json" | jq -s 'length' 2>/dev/null || echo 0)"
  healthy="$(printf '%s\n' "$compose_json" \
    | jq -s '[.[] | select(.Health=="healthy" or (.Health=="" and .State=="running"))] | length' \
      2>/dev/null || echo 0)"
  app_api_state="$(printf '%s\n' "$compose_json" \
    | jq -r 'select(.Service=="app-api") | .State' 2>/dev/null | head -n 1)"
  app_api_health="$(printf '%s\n' "$compose_json" \
    | jq -r 'select(.Service=="app-api") | .Health' 2>/dev/null | head -n 1)"
  if [[ "$total" -eq 0 ]]; then
    echo "stack:     down (no services)"
    problems=$((problems + 1))
  elif [[ "$healthy" -eq "$total" ]]; then
    echo "stack:     up (${healthy}/${total} healthy)"
  else
    echo "stack:     partial (${healthy}/${total} healthy)"
    problems=$((problems + 1))
  fi
else
  echo "stack:     unknown (docker compose ps failed)"
  problems=$((problems + 1))
fi

# curl writes `%{http_code}` to stdout *before* exiting non-zero on
# transport errors, so a `|| echo 000` fallback ends up appended to
# whatever curl printed (e.g. timeout → `000` then ` 000` → `000000`).
# Capture into an intermediate variable and overwrite on failure
# instead of piping the fallback through command substitution. Print
# both the HTTP code and curl exit status so diagnostics can distinguish
# a loop timeout from a connection refusal or plain non-200 response.
probe() {
  local out
  if out="$(curl -sS -o /dev/null -m 3 -w '%{http_code}' "$1" 2>/dev/null)"; then
    printf '%s:0' "$out"
  else
    printf '000:%s' "$?"
  fi
}
readyz_probe="$(probe "$base_url/readyz")"
healthz_probe="$(probe "$base_url/healthz")"
readyz="${readyz_probe%%:*}"
healthz="${healthz_probe%%:*}"
readyz_exit="${readyz_probe##*:}"
healthz_exit="${healthz_probe##*:}"
echo "endpoints: /readyz=${readyz} /healthz=${healthz}"
[[ "$readyz" == "200" ]] || problems=$((problems + 1))
[[ "$healthz" == "200" ]] || problems=$((problems + 1))

reload_state() {
  local recent_logs
  local last_reload
  local last_wait
  local last_started
  local last_finished

  recent_logs="$(docker compose -f "$compose_file" logs --since=5m --no-color app-api 2>/dev/null || true)"
  [[ -n "$recent_logs" ]] || return 0

  last_reload="$(printf '%s\n' "$recent_logs" \
    | awk '/WatchFiles detected changes/{line=NR} END{print line+0}')"
  last_wait="$(printf '%s\n' "$recent_logs" \
    | awk '/Waiting for connections to close/{line=NR} END{print line+0}')"
  last_started="$(printf '%s\n' "$recent_logs" \
    | awk '/Started server process/{line=NR} END{print line+0}')"
  last_finished="$(printf '%s\n' "$recent_logs" \
    | awk '/Finished server process/{line=NR} END{print line+0}')"

  if [[ "$last_reload" -gt 0 && "$last_wait" -ge "$last_reload" \
      && "$last_started" -le "$last_reload" && "$last_finished" -le "$last_reload" ]]; then
    printf 'reload-blocked'
  fi
}

if [[ "$app_api_state" == "running" ]]; then
  if [[ "$readyz" == "200" && "$healthz" == "200" && "$app_api_health" != "healthy" ]]; then
    echo "app-api:   healthcheck lag (container health=${app_api_health:-none}; endpoints are live)"
  elif [[ "$readyz" == "000" || "$healthz" == "000" ]]; then
    app_reload_state="$(reload_state)"
    if [[ "$app_reload_state" == "reload-blocked" ]]; then
      echo "app-api:   reload blocked waiting for old connections to drain (check SSE/EventSource clients)"
    elif [[ "$healthz" == "200" && "$readyz" == "000" ]]; then
      echo "app-api:   readiness probe unavailable (readyz curl=${readyz_exit}; /healthz is live)"
    elif [[ "$healthz_exit" == "28" ]]; then
      echo "app-api:   app loop unavailable (curl timeout; container still running)"
    else
      echo "app-api:   endpoint transport failure (readyz curl=${readyz_exit}, healthz curl=${healthz_exit})"
    fi
  fi
fi

# alembic prints the revision id on the last non-INFO line.
extract_rev() {
  awk '!/^INFO/ {rev=$1} END{print rev}'
}
if [[ "$healthz" == "000" ]]; then
  echo "alembic:   skipped (/healthz transport unavailable)"
  problems=$((problems + 1))
elif current_rev="$(timeout 10 docker compose -f "$compose_file" exec -T app-api \
    alembic current 2>&1 | extract_rev)" \
   && head_rev="$(timeout 10 docker compose -f "$compose_file" exec -T app-api \
    alembic heads 2>&1 | extract_rev)"; then
  if [[ -z "$current_rev" || -z "$head_rev" ]]; then
    echo "alembic:   unknown (no revision id parsed)"
    problems=$((problems + 1))
  elif [[ "$current_rev" == "$head_rev" ]]; then
    echo "alembic:   current=${current_rev} head=${head_rev} (in sync)"
  else
    echo "alembic:   current=${current_rev} head=${head_rev} (DRIFT — run alembic upgrade head)"
    problems=$((problems + 1))
  fi
else
  echo "alembic:   unknown (exec failed)"
  problems=$((problems + 1))
fi

branch="$(git -C "$(git rev-parse --show-toplevel 2>/dev/null)" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
dirty="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
echo "git:       ${branch}, ${dirty} dirty"

exit $((problems > 0 ? 1 : 0))
