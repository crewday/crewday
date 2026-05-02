#!/usr/bin/env bash
# Run all quality gates (ruff format + ruff check + mypy) at once with
# autofix where possible, then re-check to surface what still needs
# manual attention. Mirrors the CI gates in .github/workflows/ci.yml so
# a clean run here means CI will pass on this front.
#
# Sequence:
#   1. ruff format .          (auto-format)
#   2. ruff check . --fix     (auto-fix lint)
#   3. ruff check .           (report remaining lint)
#   4. ruff format --check .  (catch any remaining formatting drift)
#   5. mypy app               (strict type check; CI parity)
#
# Exit 0 only when every gate is clean. Non-zero exit means there is
# something the agent must fix by hand — the unfixable items are
# printed above the final summary.

set -uo pipefail

cd "$(git rev-parse --show-toplevel)"

ruff_fix_status=0
ruff_check_status=0
ruff_fmt_status=0
mypy_status=0

section() {
  printf '\n=== %s ===\n' "$1"
}

section "ruff format (autofix)"
uv run ruff format . || ruff_fix_status=$?

section "ruff check --fix (autofix)"
uv run ruff check . --fix || true  # exit code reflects remaining issues; we re-check below

section "ruff check (remaining issues)"
uv run ruff check . || ruff_check_status=$?

section "ruff format --check (verify formatting clean)"
uv run ruff format --check . || ruff_fmt_status=$?

section "mypy --strict app (no autofix)"
uv run mypy app || mypy_status=$?

section "summary"
overall=0
if [[ $ruff_fix_status -ne 0 ]]; then
  echo "ruff format:        FAILED to run (exit $ruff_fix_status)"
  overall=1
else
  echo "ruff format:        ok (autofixed)"
fi
if [[ $ruff_check_status -eq 0 ]]; then
  echo "ruff check:         ok"
else
  echo "ruff check:         FAILED — fix the lint issues printed above"
  overall=1
fi
if [[ $ruff_fmt_status -eq 0 ]]; then
  echo "ruff format check:  ok"
else
  echo "ruff format check:  FAILED — formatter would still rewrite files"
  overall=1
fi
if [[ $mypy_status -eq 0 ]]; then
  echo "mypy --strict app:  ok"
else
  echo "mypy --strict app:  FAILED — fix the type errors printed above"
  overall=1
fi

if [[ $overall -eq 0 ]]; then
  echo
  echo "all quality gates clean."
fi

exit "$overall"
