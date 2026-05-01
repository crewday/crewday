#!/usr/bin/env bash
# Dev-stack status digest: compose health, /readyz, /healthz, alembic
# current vs head, git branch + dirty count. Exit 0 when the stack is
# ready to use; non-zero means fix the environment first.
#
# Env: AGENT_STATUS_BASE_URL (http://127.0.0.1:8100),
# AGENT_STATUS_COMPOSE (mocks/docker-compose.yml).

set -uo pipefail

base_url="${AGENT_STATUS_BASE_URL:-http://127.0.0.1:8100}"
compose_file="${AGENT_STATUS_COMPOSE:-mocks/docker-compose.yml}"

problems=0

# Compose v2 emits one JSON object per line; jq -s slurps to an array.
if compose_json="$(docker compose -f "$compose_file" ps --format json 2>/dev/null)"; then
  total="$(printf '%s\n' "$compose_json" | jq -s 'length' 2>/dev/null || echo 0)"
  healthy="$(printf '%s\n' "$compose_json" \
    | jq -s '[.[] | select(.Health=="healthy" or (.Health=="" and .State=="running"))] | length' \
      2>/dev/null || echo 0)"
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

readyz="$(curl -sS -o /dev/null -m 3 -w '%{http_code}' "$base_url/readyz" 2>/dev/null || echo 000)"
healthz="$(curl -sS -o /dev/null -m 3 -w '%{http_code}' "$base_url/healthz" 2>/dev/null || echo 000)"
echo "endpoints: /readyz=${readyz} /healthz=${healthz}"
[[ "$readyz" == "200" ]] || problems=$((problems + 1))

# alembic prints the revision id on the last non-INFO line.
extract_rev() {
  awk '!/^INFO/ {rev=$1} END{print rev}'
}
if current_rev="$(docker compose -f "$compose_file" exec -T app-api \
    alembic current 2>&1 | extract_rev)" \
   && head_rev="$(docker compose -f "$compose_file" exec -T app-api \
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
