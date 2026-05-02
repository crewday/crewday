#!/usr/bin/env bash
# Print the next ready Beads task and its paired selfreview task in one
# shot. With no args, picks the highest-ranked task that is NOT itself a
# selfreview, preferring `bv --robot-triage` rankings (matches the
# director skill's "pick from the top of the triage list" rule) and
# falling back to `bd ready` when bv is unavailable. With an arg, looks
# up that exact task id.
#
# Usage:
#   ./scripts/agent-next-task.sh              # next ranked non-selfreview task
#   ./scripts/agent-next-task.sh cd-kd26      # specific task id
#   ./scripts/agent-next-task.sh --id-only    # print only the main task id
#   ./scripts/agent-next-task.sh --no-bv      # force the bd ready fallback
#
# A "selfreview task" is a Beads issue with the `selfreview` label whose
# `blocked-by` dependency points at the main task. Per the selfreview
# skill (.claude/skills/selfreview/SKILL.md), every non-trivial task
# gets a paired selfreview that the commiter closes alongside the main
# one. This script surfaces both so the implementer sees the full pair
# at claim time.

set -uo pipefail

if ! command -v bd >/dev/null 2>&1; then
  echo "bd (Beads CLI) not found on PATH — install it or skip Beads for this task." >&2
  exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "jq not found on PATH — required to parse Beads JSON." >&2
  exit 2
fi

id_only=0
no_bv=0
task_id=""
for arg in "$@"; do
  case "$arg" in
    --id-only) id_only=1 ;;
    --no-bv) no_bv=1 ;;
    -h|--help)
      sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*)
      echo "unknown flag: $arg" >&2
      exit 2
      ;;
    *)
      if [[ -n "$task_id" ]]; then
        echo "too many positional args; expected at most one task id" >&2
        exit 2
      fi
      task_id="$arg"
      ;;
  esac
done

# Resolve the main task id.
source=""
if [[ -z "$task_id" ]]; then
  # Prefer bv --robot-triage rankings (the director skill's recommended
  # source). `.triage.recommendations` carries `labels` inline, so we
  # filter selfreview entries in one jq pass without extra `bd show`
  # calls. Fall back to `bd ready` when bv is unavailable or returns
  # nothing usable.
  if [[ "$no_bv" -eq 0 ]] && command -v bv >/dev/null 2>&1; then
    if triage_json="$(bv --robot-triage 2>/dev/null)"; then
      task_id="$(printf '%s\n' "$triage_json" \
        | jq -r 'first(
                   (.triage.recommendations // [])[]
                   | select((.labels // []) | index("selfreview") | not)
                   | .id
                 ) // empty')"
      [[ -n "$task_id" ]] && source="bv --robot-triage"
    fi
  fi

  if [[ -z "$task_id" ]]; then
    # `bd ready` is dependency-aware and sorted by priority. Pull 50 so
    # a queue dominated by selfreviews still finds work.
    ready_json="$(bd ready --json --limit 50 2>/dev/null)" || {
      echo "bd ready failed" >&2
      exit 1
    }
    task_id="$(printf '%s\n' "$ready_json" \
      | jq -r 'first(.[] | select(.labels // [] | index("selfreview") | not) | .id) // empty')"
    [[ -n "$task_id" ]] && source="bd ready"
  fi

  if [[ -z "$task_id" ]]; then
    echo "no ready non-selfreview tasks found." >&2
    exit 1
  fi
fi

# Fetch the main task.
if ! main_json="$(bd show "$task_id" --json 2>/dev/null)"; then
  echo "bd show $task_id failed (does the issue exist?)" >&2
  exit 1
fi

if [[ "$id_only" -eq 1 ]]; then
  printf '%s\n' "$task_id"
  exit 0
fi

# Find the paired selfreview: a dependent with label "selfreview".
# `bd show --json` returns a single-element array; unwrap with .[0].
selfreview_id="$(printf '%s\n' "$main_json" \
  | jq -r '(.[0].dependents // [])
           | map(select(.labels // [] | index("selfreview")))
           | (first | .id) // empty')"

# Pretty-print the main task (bd show without --json is the human view).
if [[ -n "$source" ]]; then
  printf '\n=== MAIN TASK: %s  (picked via %s) ===\n' "$task_id" "$source"
else
  printf '\n=== MAIN TASK: %s ===\n' "$task_id"
fi
bd show "$task_id"

if [[ -n "$selfreview_id" ]]; then
  printf '\n=== PAIRED SELFREVIEW: %s ===\n' "$selfreview_id"
  bd show "$selfreview_id"
else
  printf '\n=== PAIRED SELFREVIEW ===\n'
  echo "(none — no dependent issue with label \"selfreview\" found for $task_id)"
fi

# Trailing one-liner for scripting hooks: MAIN<TAB>SELFREVIEW (or empty).
printf '\nids\t%s\t%s\n' "$task_id" "$selfreview_id"
