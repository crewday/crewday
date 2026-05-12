#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/dependency-age-gate.sh [lock|sync]

Resolve or install repo dependencies with the supply-chain age gate enabled.

Environment:
  CREWDAY_DEPENDENCY_MIN_AGE_DAYS  Minimum package age in days (default: 7).

Commands:
  lock  Refresh Python and npm lockfiles using only package releases older
        than the minimum age.
  sync  Install/sync dependencies with the same resolver age gate enabled.
EOF
}

action="${1:-lock}"
min_age_days="${CREWDAY_DEPENDENCY_MIN_AGE_DAYS:-7}"

if ! [[ "$min_age_days" =~ ^[0-9]+$ ]]; then
  echo "CREWDAY_DEPENDENCY_MIN_AGE_DAYS must be a non-negative integer." >&2
  exit 2
fi

repo_root="$(git rev-parse --show-toplevel)"
# uv stores the exact exclude-newer value in lockfiles. A date-only cutoff
# avoids per-run churn; subtract one extra day because uv treats a date as the
# next UTC midnight.
cutoff_days=$((min_age_days + 1))
cutoff="$(date -u -d "${cutoff_days} days ago" +%Y-%m-%d)"

python_projects=(
  "$repo_root"
  "$repo_root/mocks"
  "$repo_root/site/api"
)

npm_projects=(
  "$repo_root/app/web"
  "$repo_root/mocks/web"
  "$repo_root/site/web"
)

strip_uv_exclude_newer() {
  local lockfile="$1/uv.lock"
  local tmpfile

  tmpfile="$(mktemp)"
  grep -v '^exclude-newer = ' "$lockfile" > "$tmpfile"
  mv "$tmpfile" "$lockfile"
  perl -0pi -e 's/\n\[options\]\n\n(?=\[\[package\]\])/\n/g' "$lockfile"
}

case "$action" in
  lock)
    echo "Refreshing lockfiles with packages at least $min_age_days days old (uv cutoff: $cutoff)."
    for project in "${python_projects[@]}"; do
      uv lock --project "$project" --exclude-newer "$cutoff"
      strip_uv_exclude_newer "$project"
    done
    for project in "${npm_projects[@]}"; do
      npm install --prefix "$project" --package-lock-only --ignore-scripts --no-audit --no-fund --min-release-age="$min_age_days"
    done
    ;;
  sync)
    echo "Syncing dependencies with packages at least $min_age_days days old (uv cutoff: $cutoff)."
    for project in "${python_projects[@]}"; do
      uv sync --project "$project" --exclude-newer "$cutoff"
      strip_uv_exclude_newer "$project"
    done
    for project in "${npm_projects[@]}"; do
      npm ci --prefix "$project" --no-audit --no-fund --min-release-age="$min_age_days"
    done
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
