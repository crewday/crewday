#!/usr/bin/env bash
# Run all quality gates (ruff format + ruff check + mypy + pytest) at
# once with autofix where possible, then re-check to surface what still
# needs manual attention. Pytest uses pytest-testmon by default so tests
# whose executed code has not changed are deselected after the cache is
# warm. The script uses ``--testmon-forceselect`` because the repo's
# pytest addopts include a marker selector.
#
# Sequence:
#   1. ruff format .          (auto-format)
#   2. ruff check . --fix     (auto-fix lint)
#   3. ruff check .           (report remaining lint)
#   4. ruff format --check .  (catch any remaining formatting drift)
#   5. migration tree check  (dev-stack readyz can read Alembic scripts)
#   6. mypy app              (strict type check; CI parity)
#   7. pytest --testmon-forceselect
#
# Exit 0 only when every gate is clean. Non-zero exit means there is
# something the agent must fix by hand — the unfixable items are
# printed above the final summary.

set -uo pipefail

cd "$(git rev-parse --show-toplevel)"

test_mode=testmon
pytest_args=()

usage() {
  cat <<'EOF'
Usage: ./scripts/agent-quality.sh [--full-tests | --skip-tests] [-- <pytest args>]

Options:
  --full-tests   Run the full pytest suite instead of pytest-testmon selection.
  --skip-tests   Run only Ruff and mypy gates.
  -h, --help     Show this help.

Any arguments after -- are passed through to pytest.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full-tests)
      test_mode=full
      shift
      ;;
    --skip-tests)
      test_mode=skip
      shift
      ;;
    --)
      shift
      pytest_args=("$@")
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

ruff_fix_status=0
ruff_check_status=0
ruff_fmt_status=0
migration_tree_status=0
mypy_status=0
pytest_status=0

section() {
  printf '\n=== %s ===\n' "$1"
}

check_migration_script_tree() {
  local versions_dir="migrations/versions"

  if [[ ! -d "$versions_dir" ]]; then
    echo "missing $versions_dir; /readyz cannot resolve the Alembic script tree" >&2
    return 1
  fi

  local dir
  for dir in migrations "$versions_dir"; do
    if [[ ! -r "$dir" || ! -x "$dir" ]]; then
      echo "$dir must be readable/executable by this user" >&2
      return 1
    fi
    if [[ -n "$(find "$dir" -maxdepth 0 ! -perm -005 -print)" ]]; then
      echo "$dir must be world-readable/executable for the dev app container" >&2
      return 1
    fi
  done

  local migration_count
  migration_count="$(find "$versions_dir" -type f -name '*.py' | wc -l | tr -d ' ')"
  if [[ "$migration_count" -eq 0 ]]; then
    echo "no Python migration scripts found under $versions_dir" >&2
    return 1
  fi

  local unreadable
  unreadable="$(find "$versions_dir" -type f -name '*.py' ! -perm -004 -print)"
  if [[ -n "$unreadable" ]]; then
    echo "migration scripts must be world-readable for the dev app container:" >&2
    printf '%s\n' "$unreadable" >&2
    echo "fix: chmod a+r <file>" >&2
    return 1
  fi
}

section "ruff format (autofix)"
uv run ruff format . || ruff_fix_status=$?

section "ruff check --fix (autofix)"
uv run ruff check . --fix || true  # exit code reflects remaining issues; we re-check below

section "ruff check (remaining issues)"
uv run ruff check . || ruff_check_status=$?

section "ruff format --check (verify formatting clean)"
uv run ruff format --check . || ruff_fmt_status=$?

section "migration script tree visibility"
check_migration_script_tree || migration_tree_status=$?

section "mypy --strict app (no autofix)"
uv run mypy app || mypy_status=$?

case "$test_mode" in
  testmon)
    section "pytest --testmon-forceselect (affected tests)"
    uv run pytest --testmon-forceselect "${pytest_args[@]}" || pytest_status=$?
    ;;
  full)
    section "pytest (full suite)"
    uv run pytest "${pytest_args[@]}" || pytest_status=$?
    ;;
  skip)
    section "pytest"
    echo "skipped (--skip-tests)"
    ;;
esac

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
if [[ $migration_tree_status -eq 0 ]]; then
  echo "migration tree:     ok"
else
  echo "migration tree:     FAILED — fix unreadable or missing migration scripts"
  overall=1
fi
if [[ $mypy_status -eq 0 ]]; then
  echo "mypy --strict app:  ok"
else
  echo "mypy --strict app:  FAILED — fix the type errors printed above"
  overall=1
fi
case "$test_mode" in
  testmon)
    if [[ $pytest_status -eq 0 ]]; then
      echo "pytest testmon:      ok"
    else
      echo "pytest testmon:      FAILED — fix the test failures printed above"
      overall=1
    fi
    ;;
  full)
    if [[ $pytest_status -eq 0 ]]; then
      echo "pytest full suite:   ok"
    else
      echo "pytest full suite:   FAILED — fix the test failures printed above"
      overall=1
    fi
    ;;
  skip)
    echo "pytest:             skipped"
    ;;
esac

if [[ $overall -eq 0 ]]; then
  echo
  echo "all quality gates clean."
fi

exit "$overall"
