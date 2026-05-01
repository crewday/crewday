#!/usr/bin/env bash
# Authenticated curl against the dev stack. Caches the dev session
# cookie per workspace+email, auto-refreshes on stale sessions, pretty-
# prints JSON, exits non-zero on 4xx/5xx.
#
#   ./scripts/agent-curl.sh <workspace> <METHOD> <path> [body] [--raw]
#
# Path is appended verbatim to $AGENT_CURL_BASE_URL (default
# http://127.0.0.1:8100) — include /w/<slug>/api/v1/... yourself.
# Env: AGENT_CURL_EMAIL (me@dev.local), AGENT_CURL_BASE_URL,
# AGENT_CURL_COMPOSE (mocks/docker-compose.yml).

set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <workspace> <METHOD> <path> [body] [--raw]" >&2
  exit 2
fi

workspace="$1"
method="$2"
path="$3"
body="${4:-}"
raw_flag=""
if [[ "${5:-}" == "--raw" || "${4:-}" == "--raw" ]]; then
  raw_flag="1"
  [[ "${4:-}" == "--raw" ]] && body=""
fi

email="${AGENT_CURL_EMAIL:-me@dev.local}"
base_url="${AGENT_CURL_BASE_URL:-http://127.0.0.1:8100}"
compose_file="${AGENT_CURL_COMPOSE:-mocks/docker-compose.yml}"

email_safe="${email//[^A-Za-z0-9._-]/_}"
cookie_file="/tmp/crewday-agent-cookie-${workspace}-${email_safe}.txt"

refresh_cookie() {
  local out
  out="$(docker compose -f "$compose_file" exec -T -e CREWDAY_DEV_AUTH=1 app-api \
    python -m scripts.dev_login \
      --email "$email" --workspace "$workspace" --output cookie 2>&1)" || {
    echo "agent-curl: dev_login failed for ws=$workspace email=$email" >&2
    echo "$out" >&2
    return 1
  }
  # dev_login emits a single line ``__Host-crewday_session=<value>``.
  printf '%s' "$out" >"$cookie_file"
}

if [[ ! -s "$cookie_file" ]]; then
  refresh_cookie
fi

run_curl() {
  local cookie
  cookie="$(cat "$cookie_file")"
  local args=(-sS -o /tmp/agent-curl-body.$$ -w '%{http_code}\t%{content_type}'
              -X "$method" -b "$cookie" "$base_url$path")
  if [[ -n "$body" ]]; then
    args+=(-H 'Content-Type: application/json' --data-raw "$body")
  fi
  curl "${args[@]}"
}

# Workspace paths return 404 to unauthenticated callers (anti-
# enumeration), so 401 alone is not a reliable expiry signal. Probe
# /api/v1/me: 401 there proves the cookie is stale.
session_is_stale() {
  local cookie probe
  cookie="$(cat "$cookie_file")"
  probe="$(curl -sS -o /dev/null -m 3 -w '%{http_code}' \
           -b "$cookie" "$base_url/api/v1/me" 2>/dev/null || echo 000)"
  [[ "$probe" == "401" ]]
}

trap 'rm -f /tmp/agent-curl-body.$$' EXIT

meta="$(run_curl)" || {
  echo "agent-curl: curl invocation failed for $method $path" >&2
  exit 1
}
status="${meta%%$'\t'*}"
ctype="${meta#*$'\t'}"

if [[ "$status" == "401" || "$status" == "404" ]] && session_is_stale; then
  refresh_cookie
  meta="$(run_curl)"
  status="${meta%%$'\t'*}"
  ctype="${meta#*$'\t'}"
fi

echo "[${status} ${method} ${path}]" >&2

if [[ -z "$raw_flag" && "$ctype" == application/json* ]] && command -v jq >/dev/null 2>&1; then
  jq . </tmp/agent-curl-body.$$
else
  cat /tmp/agent-curl-body.$$
fi

case "$status" in
  2*) exit 0 ;;
  *)  exit 1 ;;
esac
