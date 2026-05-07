#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
site_root="$(cd -- "$script_dir/.." >/dev/null 2>&1 && pwd)"
web_dir="$site_root/web"
api_dir="$site_root/api"

run_step() {
  printf "\n==> %s\n" "$1"
  shift
  "$@"
}

run_in() {
  local dir="$1"
  shift
  (
    cd -- "$dir"
    "$@"
  )
}

if [[ "${SITE_QUALITY_INSTALL:-1}" != "0" ]]; then
  run_step "Install site web dependencies" npm --prefix "$web_dir" ci
  run_step "Install site API dependencies" uv sync --project "$api_dir" --locked --group dev
fi

run_step "Check site design primitives" npm --prefix "$web_dir" run check:design
run_step "Lint site web" npm --prefix "$web_dir" run lint --if-present
run_step "Typecheck site web" npm --prefix "$web_dir" run typecheck
run_step "Test site web" npm --prefix "$web_dir" run test --if-present
run_step "Build site web" npm --prefix "$web_dir" run build

run_step "Check site API formatting" run_in "$api_dir" uv run ruff format --check .
run_step "Lint site API" run_in "$api_dir" uv run ruff check .
run_step "Typecheck site API" run_in "$api_dir" uv run mypy site_api
run_step "Test site API" run_in "$api_dir" uv run pytest -q
