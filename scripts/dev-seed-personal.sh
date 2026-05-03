#!/usr/bin/env bash
# Dev-only wrapper around scripts/dev_seed_personal.py — runs the
# script inside the dev compose stack and shuttles the seed file
# across the read-only ``/app/scripts`` mount so ``capture`` /
# ``apply`` round-trip to ``scripts/dev_seed_personal.json`` in the
# host repo without you remembering ``docker cp``.
#
# Usage:
#
#   ./scripts/dev-seed-personal.sh apply
#   ./scripts/dev-seed-personal.sh capture --email <email> --workspace <slug>
#
# The seed file lives at scripts/dev_seed_personal.json. It carries
# only public material (credential id + COSE public key + AAGUID) —
# the private key never leaves your authenticator. Commit the file so
# every dev box belonging to you can re-hydrate identically.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SEED_HOST="$REPO_ROOT/scripts/dev_seed_personal.json"
SEED_CONTAINER="/tmp/dev_seed_personal.json"
COMPOSE_FILE="$REPO_ROOT/mocks/docker-compose.yml"
SERVICE="app-api"
CONTAINER="crewday-app-api"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 apply" >&2
  echo "       $0 capture --email <email> --workspace <slug>" >&2
  exit 2
fi

cmd="$1"
shift

case "$cmd" in
  apply)
    if [[ ! -f "$SEED_HOST" ]]; then
      echo "error: $SEED_HOST not found — run \`$0 capture\` first" >&2
      exit 2
    fi
    docker cp "$SEED_HOST" "$CONTAINER:$SEED_CONTAINER"
    exec docker compose -f "$COMPOSE_FILE" exec -T "$SERVICE" \
      python -m scripts.dev_seed_personal apply --file "$SEED_CONTAINER" "$@"
    ;;
  capture)
    docker compose -f "$COMPOSE_FILE" exec -T "$SERVICE" \
      python -m scripts.dev_seed_personal capture --file "$SEED_CONTAINER" "$@"
    docker cp "$CONTAINER:$SEED_CONTAINER" "$SEED_HOST"
    echo "seed written to $SEED_HOST"
    ;;
  *)
    echo "error: unknown subcommand $cmd (use apply or capture)" >&2
    exit 2
    ;;
esac
