# 01 — Architecture

## High-level picture

```
+-------------------+         +---------------------------+
|  Manager browser  |         |  Employee phone (PWA)     |
|  (HTMX + Tailwind)|         |  (HTMX + Tailwind + SW)   |
+---------+---------+         +-------------+-------------+
          |                                 |
          |   HTTPS, passkey session        |   HTTPS, passkey session
          v                                 v
+-------------------------------------------------------------+
|                   FastAPI app (ASGI)                        |
|                                                             |
|  +-----------+  +-----------+  +-----------+  +-----------+ |
|  | web.*    |  | api.v1.* |  | webhooks |  | admin     | |
|  | (HTMX)   |  | (OpenAPI)|  | (in/out) |  | (CLI/API) | |
|  +-----+----+  +-----+----+  +-----+----+  +-----+-----+ |
|        \            |              |             /        |
|         \           v              v            /         |
|          +-----------------------------------+            |
|          |            domain layer           |            |
|          |  (services, scheduling, policy)   |            |
|          +-----------------+-----------------+            |
|                            |                              |
|          +-----------------+-----------------+            |
|          |             adapters              |            |
|          |  db  |  storage  |  mail  |  llm  |            |
|          +--+---+-----+-----+---+----+---+---+            |
+-------------|---------|---------|--------|----------------+
              v         v         v        v
         +--------+ +--------+ +-------+ +----------+
         | SQLite | |  fs    | | SMTP  | | OpenRtr  |
         |  / PG  | | (/data)| | relay | | (Gemma)  |
         +--------+ +--------+ +-------+ +----------+

  Cron / APScheduler worker -- generates tasks from RRULEs, sends digests,
                              polls iCal, runs anomaly detection.
```

Agents (OpenClaw, Hermes, Claude Code, ad-hoc scripts) connect as **HTTPS
clients** to `api.v1.*` using a long-lived API token (§03). The CLI
(`miployees`) is a thin local client to the same HTTP surface.

## Component responsibilities

### `web.*` (HTMX server-rendered)

- Serves Jinja2 partials rendered server-side, hydrated in place by
  HTMX (`hx-get`, `hx-post`, `hx-swap=outerHTML`). See §14.
- Owns the session cookie (passkey-authenticated; §03).
- Hands the same underlying service functions to both web and API
  handlers. **No business logic lives in the handler layer.**

### `api.v1.*`

- Pure JSON, OpenAPI 3.1 (§12).
- No cookies, only `Authorization: Bearer <token>` (§03).
- Idempotency-Key header is honored on all `POST` mutations (§12).

### `webhooks`

- **Inbound**: iCal polling (outbound HTTP, not true webhooks, but
  colocated), email bounce handling via the SMTP provider's webhook,
  optional provider-specific reservation webhooks.
- **Outbound**: event stream (§11, §12) — POSTs signed with HMAC-SHA256
  to agent-supplied URLs on task.created, task.completed,
  task.overdue, stay.upcoming, etc.

### `admin`

- Intended surface for manager-only destructive operations (rotate API
  tokens, export, purge, re-send a magic link) — reachable from web and
  API.

### Domain layer

- Pure Python, no FastAPI/HTTP imports. Exposes service functions like
  `tasks.create_one_off`, `schedules.generate_occurrences`,
  `payroll.compute_period`, `expenses.autofill_from_receipt`.
- Depends on **ports** (Protocol classes) for every side effect:
  `DbSession`, `Storage`, `Mailer`, `LLMClient`, `Clock`.

### Adapters

- **db**: SQLAlchemy 2.x + Alembic. SQLite and Postgres dialects are
  both supported. No dialect-specific SQL outside `app/adapters/db/`.
- **storage**: `Storage` protocol with a `LocalFsStorage` implementation
  writing to `$MIPLOYEES_DATA_DIR/uploads/<first-2-of-hash>/<hash>`,
  content-addressed. An `S3Storage` implementation is specified but
  not required for v1.
- **mail**: `Mailer` protocol with SMTP implementation (envelope sender
  from config). For v1, SMTP only; providers like Resend/SES can plug in
  later behind the same protocol.
- **llm**: `LLMClient` protocol with `OpenRouterClient` v1
  implementation. Capability routing lives in the domain layer (§11).

### Worker

- Single-process `APScheduler` running inside the web process by
  default (simplest deploy), switchable to a separate process (same
  image, different entrypoint) when the manager sets
  `MIPLOYEES_WORKER=external`.
- Jobs: `generate_task_occurrences`, `poll_ical`, `send_daily_digest`,
  `detect_anomalies`, `retry_failed_webhooks`, `prune_sessions`,
  `rotate_audit_log`.

## Repo layout

```
miployees/
├── README.md
├── AGENTS.md
├── CLAUDE.md              -> AGENTS.md (symlink)
├── pyproject.toml
├── uv.lock
├── docs/
│   └── specs/             <-- this suite
├── app/
│   ├── main.py            # FastAPI factory
│   ├── config.py          # pydantic-settings
│   ├── api/
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── employees.py
│   │       ├── properties.py
│   │       ├── tasks.py
│   │       ├── stays.py
│   │       ├── inventory.py
│   │       ├── time.py
│   │       ├── expenses.py
│   │       ├── payroll.py
│   │       ├── instructions.py
│   │       ├── llm.py
│   │       └── webhooks.py
│   ├── web/               # HTMX handlers + Jinja templates
│   ├── domain/            # service functions, policies, schedulers
│   ├── adapters/
│   │   ├── db/
│   │   ├── storage/
│   │   ├── mail/
│   │   └── llm/
│   ├── auth/              # WebAuthn + magic link + tokens
│   ├── worker/            # APScheduler jobs
│   └── util/              # clock, ulid, hashids, etc.
├── cli/
│   └── miployees/         # click-based CLI (thin over REST)
├── migrations/            # alembic
├── tests/
│   ├── unit/
│   ├── integration/       # real DB (SQLite + PG via testcontainers)
│   ├── api/               # schemathesis contract tests
│   ├── e2e/               # playwright
│   └── load/              # locust
├── deploy/
│   ├── single/            # Dockerfile for minimal SQLite image
│   └── compose/           # docker-compose.yml + Caddyfile + MinIO
├── scripts/
└── .github/
    └── workflows/
```

Rationale:

- **`app/` vs `cli/` separation.** The CLI can be shipped as an
  independent wheel (`miployees-cli`) without pulling in FastAPI.
- **`domain/` is HTTP-unaware.** Tests at the domain level use fakes for
  all adapters; integration tests exercise real DB + real filesystem.
- **`adapters/` depends on `domain/`, not the other way round.** Makes
  swapping Postgres for SQLite, or MinIO for local FS, a one-file
  change.

## Environments

| Env        | Purpose                               | DB       | Storage   | Mail         | LLM       |
|------------|---------------------------------------|----------|-----------|--------------|-----------|
| dev        | local dev loop (uv run, hot reload)   | SQLite   | local fs  | MailHog      | OpenRouter (or a mock) |
| ci         | pytest + playwright in GH Actions     | SQLite + PG | tmpfs  | fake         | record/replay cassettes |
| staging    | managers' shared test instance        | Postgres | local fs  | real SMTP    | OpenRouter |
| prod       | operating household                   | SQLite or Postgres | local fs | real SMTP | OpenRouter |

## Runtime dependencies (pinned families)

- `fastapi >= 0.115`
- `uvicorn[standard]`
- `sqlalchemy >= 2.0`, `alembic`
- `pydantic >= 2.6`, `pydantic-settings`
- `jinja2`, `htmx` static assets (vendored, not from CDN)
- `webauthn` (Duo Labs / py_webauthn) for passkeys
- `python-ulid`
- `apscheduler`
- `httpx` (outbound — iCal, OpenRouter)
- `icalendar` (RFC 5545 parsing + RRULE)
- `dateutil`
- `itsdangerous` (signed tokens for guest welcome pages, magic links)
- `weasyprint` (payslip PDFs)
- `click` (CLI)
- Dev: `pytest`, `pytest-asyncio`, `schemathesis`, `playwright`,
  `ruff`, `mypy`, `locust`.

No NPM runtime. Tailwind is compiled at build time via
`tailwindcss` standalone binary and committed CSS; no Node on the
server. HTMX is a single vendored JS file.

## Key runtime invariants

1. **All times persisted in UTC.** Display time is computed from the
   target property's timezone.
2. **Domain layer never touches `datetime.now()`** — always
   `clock.now()` through the `Clock` port. Determinism matters for
   scheduling.
3. **Every mutation originates from a `RequestContext`** carrying
   `actor_id`, `actor_kind` (human/agent/system), and an
   `audit_correlation_id`. Persisted into `audit_log` in the same
   transaction as the mutation.
4. **Bind guard on public interfaces.** Default bind is
   `127.0.0.1:8000`. Loopback always passes; a non-loopback address
   passes only when it lives on an interface whose name matches a
   glob in `MIPLOYEES_TRUSTED_INTERFACES` (default `tailscale*`,
   replaced wholesale when overridden). `0.0.0.0` / `::` never pass
   on their own. Anything else requires
   `MIPLOYEES_ALLOW_PUBLIC_BIND=1`. The guard does not trust CIDR
   ranges or detect containers — it reads the live interface table.
   §16 recipes set the opt-in explicitly and gate reachability via
   the Docker port map or the internal compose network. See §15 and
   §16.
5. **Secrets are never logged.** Redactor filter on the root logger
   masks anything matching token, cookie, Authorization, passkey
   credential id.

## Decision log pointers

Further architectural decisions (e.g. "why SQLAlchemy and not SQLModel",
"why APScheduler and not Arq") live in
[`docs/adr/`](../adr/) once implementation begins. The spec references
ADRs but does not embed them.
