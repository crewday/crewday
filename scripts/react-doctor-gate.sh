#!/usr/bin/env bash
# Run the blocking React Doctor quality gate for active React surfaces.
# Intentionally excludes mocks/web, which is not an active surface.

set -uo pipefail

cd "$(git rev-parse --show-toplevel)"

readonly REACT_DOCTOR_VERSION="0.2.3"
readonly -a WORKSPACES=("app/web" "site/web")

overall=0

section() {
  printf '\n=== %s ===\n' "$1"
}

for workspace in "${WORKSPACES[@]}"; do
  section "react-doctor $workspace"
  report="$(mktemp)"

  if ! npm exec --yes --package "react-doctor@$REACT_DOCTOR_VERSION" -- \
    react-doctor "$workspace" --json --json-compact --full --fail-on none >"$report"; then
    echo "React Doctor failed to run for $workspace" >&2
    cat "$report" >&2
    rm -f "$report"
    overall=1
    continue
  fi

  if ! jq -e '
    (.projects | length) == 1
    and (.projects[0].score.score == 100)
    and (.projects[0].diagnostics | length == 0)
    and (.summary.totalDiagnosticCount == 0)
  ' "$report" >/dev/null; then
    echo "React Doctor gate failed for $workspace: score must be 100 and diagnostics must be zero." >&2
    jq -r '
      "score: \(.projects[0].score.score // "unknown")",
      "diagnostics: \(.summary.totalDiagnosticCount // (.diagnostics | length))",
      (.diagnostics[]? | "- \(.filePath): \(.plugin)/\(.rule) [\(.severity)] \(.message)")
    ' "$report" >&2
    overall=1
  else
    jq -r '"score: \(.projects[0].score.score); diagnostics: \(.summary.totalDiagnosticCount)"' "$report"
  fi

  rm -f "$report"
done

exit "$overall"
