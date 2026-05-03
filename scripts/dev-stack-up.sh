#!/usr/bin/env bash
# Bring the dev compose stack up and verify it's actually serving.
#
# Wraps ``docker compose -f mocks/docker-compose.yml up -d --build``,
# polls ``/readyz`` until the loopback API responds, and runs the
# shared drift probe so migration / heartbeat / root-key issues
# surface here instead of as a cryptic test failure later (cd-3yp9,
# surfaced by cd-t2jz).
#
# Exits 0 on a green stack; non-zero with a loud warning + remediation
# when a probe fails. Override the readyz drift gate with
# ``CREWDAY_SKIP_READYZ_CHECK=1`` (compose still comes up either way).
#
# Env knobs:
#   READYZ_BASE_URL       — base URL to probe (default ``http://127.0.0.1:8100``).
#   READYZ_BOOT_TIMEOUT   — seconds to wait for the API to first respond
#                           (default ``60``).
#   READYZ_BOOT_INTERVAL  — poll cadence in seconds (default ``2``).

set -uo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_repo_root="$(cd "$_here/.." && pwd)"

compose_file="${COMPOSE_FILE:-mocks/docker-compose.yml}"
base_url="${READYZ_BASE_URL:-http://127.0.0.1:8100}"
boot_timeout="${READYZ_BOOT_TIMEOUT:-60}"
boot_interval="${READYZ_BOOT_INTERVAL:-2}"

cd "$_repo_root" || exit 1

printf '==> docker compose -f %s up -d --build\n' "$compose_file"
if ! docker compose -f "$compose_file" up -d --build; then
  printf '\n!! compose up failed — see output above\n' >&2
  exit 1
fi

printf '==> waiting up to %ss for %s/readyz to respond\n' "$boot_timeout" "$base_url"
deadline=$(( $(date +%s) + boot_timeout ))
ready=0
while [[ "$(date +%s)" -lt "$deadline" ]]; do
  # We only need a transport-level success here; the body parse
  # happens in the drift check below. ``-o /dev/null`` suppresses
  # body, ``-w '%{http_code}'`` gives us a status to gate on.
  http_code="$(curl -sS -o /dev/null -m 3 -w '%{http_code}' \
    "$base_url/readyz" 2>/dev/null || true)"
  if [[ "$http_code" == "200" || "$http_code" == "503" ]]; then
    ready=1
    break
  fi
  sleep "$boot_interval"
done

if [[ "$ready" != "1" ]]; then
  printf '\n!! %s/readyz never responded within %ss\n' "$base_url" "$boot_timeout" >&2
  printf '   check: docker compose -f %s logs --tail=80 app-api\n' "$compose_file" >&2
  printf '   fix:   docker compose -f %s restart app-api\n' "$compose_file" >&2
  exit 1
fi

# Run the shared drift probe. The library prints its own warning
# block on failure, so we just need to translate exit codes.
# shellcheck source=scripts/_lib/check_readyz.sh
. "$_here/_lib/check_readyz.sh"

if [[ "${CREWDAY_SKIP_READYZ_CHECK:-0}" == "1" ]]; then
  printf '==> readyz drift check skipped (CREWDAY_SKIP_READYZ_CHECK=1)\n'
  printf '\nstack ready (unverified): %s\n' "$base_url"
  exit 0
fi

if _check_readyz; then
  printf '==> readyz: ok\n'
  printf '\nstack ready: %s\n' "$base_url"
  exit 0
fi

# _check_readyz already wrote the failing-checks block to stderr.
printf '\n!! dev stack came up but is degraded — see warning above\n' >&2
exit 1
