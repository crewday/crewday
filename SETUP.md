# SETUP.md

Setup procedure for developer agents working on crew.day.

This document is for coding agents and developer-side automation. It is
not the operating guide for LLM agents acting inside a running household
workspace; those live in `docs/specs/11-llm-and-agents.md` and
`docs/specs/13-cli.md`.

## Surfaces

- **Dev app**: `https://dev-app.crew.day`
  - Remote browser entry point.
  - Gated by Pangolin badger forward-auth.
  - Agents on this host cannot pass badger; use the loopback equivalent
    `http://127.0.0.1:8100`.
  - The loopback and public app paths are 1:1.
- **Dev marketing site**: `https://dev.crew.day`
  - Served by `site/docker-compose.yml`.
  - Production-like local static Caddy path:
    `http://127.0.0.1:18080`.
  - Source hot reload path:
    `http://127.0.0.1:18081`.
- **Production**: not deployed yet.
  - Production app code lives under `app/`.
  - High-fidelity mocks remain under `mocks/`.
  - See `docs/specs/19-roadmap.md`.

Never bind a new service to the public interface. Use `127.0.0.1` or
`tailscale0` only. A public misbind is a blocker bug; see `docs/specs/16`.

## Prerequisites

Developer agents are expected to run from the repo root on a host with:

- Docker with Compose v2.
- Python managed by `uv`.
- Node tooling available through the repo-managed frontend packages.
- Access to the shared worktree; always check `git status --short` before
  editing because multiple agents may be active.

If Python dependencies are missing during tests or app startup, run:

```bash
uv sync --all-groups
```

Do not install ad hoc Python packages outside the project dependency files.

## New Agent Checklist

1. Read `AGENTS.md` for collaboration and coding rules.
2. Read this file for environment setup and dev-stack access.
3. Run `git status --short` and preserve unrelated dirty files.
4. Bring the dev stack up with `./scripts/dev-stack-up.sh` when the task needs
   the app.
5. Use `http://127.0.0.1:8100` for app automation and smoke checks.
6. Use the narrowest quality gates that prove the change before handoff.

## First Boot

From the repo root:

```bash
./scripts/dev-stack-up.sh
```

The wrapper runs `docker compose -f docker-compose.dev.yml up -d --build`,
waits for `/readyz`, and reports migration, heartbeat, and root-key drift with
a one-line remediation hint. The raw compose command still works when you
intentionally want to skip the drift gate.

The dev stack defaults to the in-process fake LLM client. To smoke the real
OpenRouter path locally, set `CREWDAY_LLM_PROVIDER=openrouter` and
`CREWDAY_OPENROUTER_API_KEY` in the gitignored root `.env`, then start the stack;
do not put the key in tracked compose files, specs, tests, or logs.

Check readiness with:

```bash
./scripts/agent-status.sh
```

Trust the `endpoints:` line over the compose `stack:` line. A transient
`app-api` container healthcheck failure can lag behind `/readyz`, `/healthz`,
and API routes. Recurring `llm.budget.refresh.workspace_failed` warnings for
idle zero-spend dev workspaces are not expected; if they continue after a code
change, first check whether WatchFiles is waiting for open connections to drain
before the app process reloads. The dev app runs uvicorn with a bounded
graceful shutdown timeout so long-lived `/events` streams do not keep the old
worker and its scheduler alive indefinitely during source reload.

If `/healthz` itself times out, treat that as an app-loop availability problem,
not a DB readiness failure. `scripts/agent-status.sh` exits non-zero for that
case and reports whether a recent WatchFiles reload is blocked on old
connections; collect `crewday-app-api` logs around the timeout before
restarting so pool waits or blocking middleware can be fixed at the source.

## App Access

Use loopback for agent-driven app work:

```text
http://127.0.0.1:8100
```

Do not use `https://dev-app.crew.day` from this host for scripted checks; badger
forward-auth blocks agents.

For authenticated API smoke checks, prefer:

```bash
./scripts/agent-curl.sh dev GET /w/dev/api/v1/employees
./scripts/agent-curl.sh dev POST /w/dev/api/v1/tasks '{"title":"smoke"}'
```

`agent-curl.sh` caches and refreshes a dev session per workspace/email, pretty
prints JSON, writes `[<status> <METHOD> <path>]` to stderr, and exits non-zero
on 4xx/5xx. Include the full workspace path in the request path.

If you need a raw cookie inside the compose stack:

```bash
docker compose -f docker-compose.dev.yml exec app-api \
  python -m scripts.dev_login --email me@dev.local --workspace smoke
```

Stdout is `__Host-crewday_session=<value>`. Feed it to:

```bash
curl -b "$cookie" http://127.0.0.1:8100/w/smoke/api/v1/...
```

Host-side login variant:

```bash
CREWDAY_DEV_AUTH=1 ./scripts/dev-login.sh <email> <slug>
```

The wrapper forces the login subprocess into the dev profile and falls
back to the running `app-api` container when host Python lacks app deps
or host-only auth config. For browser checks, add
`--output playwright` and inject the printed alias cookie into
Playwright.

## Browser Checks

Playwright on loopback needs an alias cookie, not the curl cookie.
`__Host-` cookies require `Secure`, which browsers reject on plain HTTP.
See `docs/dev/playwright-auth.md` for the `--output playwright` recipe and the
e2e helper that wraps it.

Playwright screenshots go under `.playwright-mcp/` with descriptive filenames.
Close the browser after checks.

## Personal Passkey Seed

The checked-in personal seed is for the current shared dev operator account.
Developer agents should not assume it is their own credential. New developers
who need physical-passkey access should register their own passkey and capture
their own seed only when the project owner asks them to.

The personal seed file is:

```text
scripts/dev_seed_personal.json
```

It contains public passkey material only: credential id, COSE public key,
AAGUID, transports, plus the owner/workspace bootstrap data. The private key
never leaves the authenticator.

After a disposable dev DB reset, rehydrate the personal account with:

```bash
./scripts/dev-seed-personal.sh apply
```

This recreates the user, workspace, system permission groups, owner membership,
manager grant, LLM budget ledger, deployment admin grant, deployment owner row,
and passkey credential rows. The helper is dev-only and gated to
`CREWDAY_DEV_AUTH=1`, `CREWDAY_PROFILE=dev`, and SQLite.

## Passkey Reset After Dev Domain Changes

WebAuthn credentials are scoped to the RP ID. If the dev app domain or
`CREWDAY_WEBAUTHN_RP_ID` changes, old passkeys cannot be migrated.

Use this clean reset flow:

```bash
docker compose -f docker-compose.dev.yml down -v
./scripts/dev-stack-up.sh
```

Then sign up in the browser at:

```text
https://dev-app.crew.day/signup
```

Use the intended email and workspace slug, complete the activation link, and
register a fresh passkey. Then capture the new seed:

```bash
./scripts/dev-seed-personal.sh capture --email <email> --workspace <slug>
```

Inspect the captured seed before committing:

```bash
jq '{email: .owner.email, deployment_admin: .owner.deployment_admin, workspace: .workspace, passkeys: (.owner.passkeys | length)}' scripts/dev_seed_personal.json
python -m json.tool scripts/dev_seed_personal.json >/dev/null
```

If the fresh signup user should restore deployment admin access on future
resets, make sure `owner.deployment_admin` is `true` in the seed.

## Signup Activation Links

In the dev stack, signup mail should land in Mailpit:

```text
http://127.0.0.1:8025
```

The API endpoint is:

```bash
curl -fsS http://127.0.0.1:8025/api/v1/messages
```

If Mailpit is empty but a signup attempt exists, inspect the DB before asking
the user to repeat the flow. A pending signup attempt with `verified_at = NULL`
and `completed_at = NULL` means the workspace has not been created yet. The
activation token can be reconstructed from the pending `magic_link_nonce` row
inside the dev container when necessary.

## CAPTCHA Default

`captcha_required` defaults to `false`. The product must not require CAPTCHA
unless a CAPTCHA provider is configured and an operator explicitly enables the
gate. A clean dev DB should allow signup without a CAPTCHA token.

## Marketing Site

Production-like local static site:

```bash
docker compose -f site/docker-compose.yml up -d --build
```

Open:

```text
http://127.0.0.1:18080
```

Source hot reload:

```bash
docker compose -f site/docker-compose.yml -f site/docker-compose.dev.yml --profile dev up site-web-dev
```

Open:

```text
http://127.0.0.1:18081
```

## Migrations And Broken Dev DBs

If `dev_login`, `/readyz`, or a smoke request fails with a missing
column/table, run:

```bash
docker compose -f docker-compose.dev.yml exec app-api alembic upgrade head
```

If the disposable dev DB is still broken, reset only the dev app volume:

```bash
docker compose -f docker-compose.dev.yml down -v
./scripts/dev-stack-up.sh
```

Do not reset any non-dev database.

## Quality And Health Helpers

Use the wrapper for normal quality gates:

```bash
./scripts/agent-quality.sh
```

It runs `ruff format`, `ruff check --fix`, remaining lint/format checks,
`mypy --strict app`, and pytest through `pytest-testmon`. Use:

```bash
./scripts/agent-quality.sh --full-tests
./scripts/agent-quality.sh --skip-tests
```

Use `--skip-tests` only when a focused test already proves the change.

Code-health digest:

```bash
./scripts/agent-code-health.py
./scripts/agent-code-health.py app/domain/tasks
./scripts/agent-code-health.py --no-dup --top 20
./scripts/agent-code-health.py --json-out /tmp/code-health.json
```

This is not a gate; it is a refactor-target finder for cyclomatic complexity,
function length, parameter count, and duplicate token blocks.

## End-to-End Tests

The Playwright e2e suite under `tests/e2e/` runs against the dev compose stack
plus an override that aligns WebAuthn with loopback. See `tests/e2e/README.md`
for the full compose override, Playwright install, pytest flags, traces, and
videos.
