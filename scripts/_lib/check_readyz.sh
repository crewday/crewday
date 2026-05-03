#!/usr/bin/env bash
# shellcheck shell=bash
# Reusable /readyz drift probe for dev-stack tooling.
#
# Source this file and call ``_check_readyz`` to confirm the loopback
# app is healthy before running login flows, smokes, or pytest. The
# function prints nothing on success; on drift it writes a loud
# warning block to stderr naming the failing checks plus a one-line
# remediation hint. Return code is 0 when healthy, 1 on drift /
# transport error, 2 on a parser fault (jq / curl missing).
#
# Designed to mirror the parsing already in scripts/agent-status.sh
# without duplicating the broader status-digest logic. The /readyz
# JSON shape is pinned in app/api/health.py:
#
#   200: {"status": "ok",       "checks": []}
#   503: {"status": "degraded", "checks": [{"check": "...",
#                                           "ok": false,
#                                           "detail": "..."}, ...]}
#
# Env knobs:
#   READYZ_BASE_URL  — base URL to probe (default ``http://127.0.0.1:8100``).
#   READYZ_TIMEOUT   — curl ``-m`` seconds (default ``5``).

# Recognised failure ``detail`` strings → operator remediation hint.
# Pinned to the strings emitted by ``app/api/health.py`` so the hint
# survives a rename refactor (dev DB drift falls through to the
# generic "restart" hint, which is the right call when nothing
# specific matches).
_readyz_remedy_for() {
  case "$1" in
    migrations_behind|alembic_version_empty|alembic_version_unreadable)
      printf 'docker compose -f mocks/docker-compose.yml exec app-api alembic upgrade head\n'
      ;;
    alembic_script_tree_unreadable)
      # A restart can't fix a broken script tree (missing revisions,
      # unmerged heads, IO faults under alembic/versions/) — the
      # operator must inspect the source tree or rebuild the image.
      printf 'inspect alembic/versions/ for missing/unmerged revisions, then docker compose -f mocks/docker-compose.yml up -d --build app-api\n'
      ;;
    db_unreachable)
      printf 'docker compose -f mocks/docker-compose.yml restart app-api app-db\n'
      ;;
    no_heartbeat|heartbeat_stale)
      printf 'docker compose -f mocks/docker-compose.yml restart app-worker\n'
      ;;
    root_key_missing)
      printf 'set CREWDAY_ROOT_KEY in mocks/.env then docker compose -f mocks/docker-compose.yml up -d app-api\n'
      ;;
    *)
      printf 'docker compose -f mocks/docker-compose.yml restart app-api\n'
      ;;
  esac
}

# ``_check_readyz`` — probe ``$READYZ_BASE_URL/readyz`` once.
#
# Echos nothing on a healthy 200 response. On drift, transport
# failure, or non-200 response, prints a fenced warning block to
# stderr and returns 1. Returns 2 when jq / curl are missing.
_check_readyz() {
  local base_url="${READYZ_BASE_URL:-http://127.0.0.1:8100}"
  local timeout="${READYZ_TIMEOUT:-5}"
  local url="${base_url%/}/readyz"

  if ! command -v curl >/dev/null 2>&1; then
    printf 'readyz check: curl missing on PATH\n' >&2
    return 2
  fi
  if ! command -v jq >/dev/null 2>&1; then
    printf 'readyz check: jq missing on PATH\n' >&2
    return 2
  fi

  # Capture body + status separately. ``-w '%{http_code}'`` appends
  # the status to stdout; we slice it off with bash parameter
  # expansion so we don't have to write a temp file. Curl's transport
  # error (DNS, refused, timeout) goes to a tmpfile so it doesn't
  # contaminate the body parse.
  local response http_code body curl_err curl_rc
  curl_err="$(mktemp)"
  response="$(curl -sS -m "$timeout" -w '\n%{http_code}' "$url" 2>"$curl_err")"
  curl_rc=$?
  if [[ $curl_rc -ne 0 ]]; then
    local err_msg
    err_msg="$(tr -d '\n' < "$curl_err")"
    rm -f "$curl_err"
    {
      printf '\n'
      printf '!! readyz drift detected at %s\n' "$url"
      printf '   transport error: %s\n' "${err_msg:-curl exit $curl_rc}"
      printf '   hint: docker compose -f mocks/docker-compose.yml up -d --build\n'
      printf '   override: CREWDAY_SKIP_READYZ_CHECK=1 to bypass\n'
      printf '\n'
    } >&2
    return 1
  fi
  rm -f "$curl_err"

  http_code="${response##*$'\n'}"
  body="${response%$'\n'*}"

  # Healthy fast path: 200 + status=="ok". We still parse status so a
  # 200 with a wrong shape (proxy intercept, captive portal) trips the
  # drift branch instead of silently passing.
  local status
  if ! status="$(printf '%s' "$body" | jq -r '.status // "unknown"' 2>/dev/null)"; then
    status="unparseable"
  fi

  if [[ "$http_code" == "200" && "$status" == "ok" ]]; then
    return 0
  fi

  # Drift / degraded path. Pull failing checks; on a parse failure
  # fall back to a single synthetic entry so the warning block still
  # tells the operator something useful.
  local failing
  if ! failing="$(printf '%s' "$body" \
      | jq -r '.checks[]? | select(.ok == false) | "\(.check)\t\(.detail // "no_detail")"' \
        2>/dev/null)"; then
    failing=""
  fi

  {
    printf '\n'
    printf '!! readyz drift detected at %s\n' "$url"
    printf '   http=%s status=%s\n' "$http_code" "$status"
    if [[ -n "$failing" ]]; then
      local check detail hint
      while IFS=$'\t' read -r check detail; do
        [[ -z "$check" ]] && continue
        hint="$(_readyz_remedy_for "$detail")"
        printf '   - %s: %s\n' "$check" "$detail"
        printf '     fix: %s\n' "$hint"
      done <<< "$failing"
    else
      printf '   (no parseable checks[] in body — first 200 chars below)\n'
      printf '   %s\n' "${body:0:200}"
      printf '   fix: docker compose -f mocks/docker-compose.yml restart app-api\n'
    fi
    printf '   override: CREWDAY_SKIP_READYZ_CHECK=1 to bypass\n'
    printf '\n'
  } >&2

  return 1
}
