#!/usr/bin/env bash
# Run all quality gates (ruff format + ruff check + mypy + React Doctor
# + pytest) at once with autofix where possible, then re-check to surface
# what still needs manual attention. Pytest uses pytest-testmon by default
# so tests whose executed code has not changed are deselected after the
# cache is warm. The script uses ``--testmon-forceselect`` because the
# repo's pytest addopts include a marker selector.
#
# Sequence:
#   1. ruff format .          (auto-format)
#   2. ruff check . --fix     (auto-fix lint)
#   3. ruff check .           (report remaining lint)
#   4. ruff format --check .  (catch any remaining formatting drift)
#   5. bind-mount visibility (dev-stack containers can read source files)
#   6. mypy app              (strict type check; CI parity)
#   7. make openapi-agent-links
#   8. scripts/react-doctor-gate.sh
#   9. pytest --testmon-forceselect
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
  --skip-tests   Run static gates only, including React Doctor; skip pytest.
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
bind_mount_visibility_status=0
mypy_status=0
openapi_agent_links_status=0
react_doctor_status=0
pytest_status=0

section() {
  printf '\n=== %s ===\n' "$1"
}

fix_bind_mount_visibility() {
  local -a roots=(
    "app"
    "migrations"
    "scripts"
    "alembic.ini"
    "pyproject.toml"
  )
  local -a files=()
  local path
  local dir
  local fixed=0

  while IFS= read -r -d '' path; do
    files+=("$path")
  done < <(
    {
      git ls-files -z -- "${roots[@]}"
      git ls-files -z --others --exclude-standard -- "${roots[@]}"
    } | sort -zu
  )

  for path in "${files[@]}"; do
    if [[ -f "$path" && -n "$(find "$path" -maxdepth 0 ! -perm -004 -print)" ]]; then
      chmod a+r "$path"
      echo "made world-readable: $path"
      fixed=1
    fi

    dir="$(dirname "$path")"
    while [[ "$dir" != "." && "$dir" != "/" ]]; do
      if [[ -d "$dir" && -n "$(find "$dir" -maxdepth 0 ! -perm -005 -print)" ]]; then
        chmod a+rx "$dir"
        echo "made world-readable/executable: $dir"
        fixed=1
      fi
      dir="$(dirname "$dir")"
    done
  done

  if [[ $fixed -eq 0 ]]; then
    echo "bind-mounted source files are container-readable"
  fi
}

check_bind_mount_visibility() {
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
    return 1
  fi

  local -a roots=(
    "app"
    "migrations"
    "scripts"
    "alembic.ini"
    "pyproject.toml"
  )
  local -a files=()
  local path
  while IFS= read -r -d '' path; do
    files+=("$path")
  done < <(
    {
      git ls-files -z -- "${roots[@]}"
      git ls-files -z --others --exclude-standard -- "${roots[@]}"
    } | sort -zu
  )

  local unreadable_file_count=0
  local unreadable_dir_count=0
  for path in "${files[@]}"; do
    if [[ -f "$path" && -n "$(find "$path" -maxdepth 0 ! -perm -004 -print)" ]]; then
      if [[ $unreadable_file_count -eq 0 ]]; then
        echo "bind-mounted files must be world-readable for dev containers:" >&2
      fi
      printf '%s\n' "$path" >&2
      unreadable_file_count=$((unreadable_file_count + 1))
    fi

    local dir
    dir="$(dirname "$path")"
    while [[ "$dir" != "." && "$dir" != "/" ]]; do
      if [[ -d "$dir" && -n "$(find "$dir" -maxdepth 0 ! -perm -005 -print)" ]]; then
        if [[ $unreadable_dir_count -eq 0 ]]; then
          echo "bind-mounted directories must be world-readable/executable for dev containers:" >&2
        fi
        printf '%s\n' "$dir" >&2
        unreadable_dir_count=$((unreadable_dir_count + 1))
      fi
      dir="$(dirname "$dir")"
    done
  done
  if [[ $unreadable_file_count -ne 0 || $unreadable_dir_count -ne 0 ]]; then
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

section "bind-mount visibility (autofix)"
fix_bind_mount_visibility

section "bind-mount visibility (verify)"
check_bind_mount_visibility || bind_mount_visibility_status=$?

section "mypy --strict app (no autofix)"
uv run mypy app || mypy_status=$?

section "openapi-agent-links"
make openapi-agent-links || openapi_agent_links_status=$?

section "react-doctor (active React surfaces)"
./scripts/react-doctor-gate.sh || react_doctor_status=$?

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
if [[ $bind_mount_visibility_status -eq 0 ]]; then
  echo "bind visibility:    ok"
else
  echo "bind visibility:    FAILED — fix unreadable or missing bind-mounted files"
  overall=1
fi
if [[ $mypy_status -eq 0 ]]; then
  echo "mypy --strict app:  ok"
else
  echo "mypy --strict app:  FAILED — fix the type errors printed above"
  overall=1
fi
if [[ $openapi_agent_links_status -eq 0 ]]; then
  echo "openapi links:      ok"
else
  echo "openapi links:      FAILED — fix x-agent-links issues printed above"
  overall=1
fi
if [[ $react_doctor_status -eq 0 ]]; then
  echo "react doctor:       ok"
else
  echo "react doctor:       FAILED — fix every diagnostic printed above unless it is in a dirty file you did not edit; comment on the Beads task when blocked"
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
