# 17 — Testing and quality gates

## Test pyramid

| layer                | tool                      | runs on               | budget per run |
|----------------------|---------------------------|-----------------------|----------------|
| static (type/lint)   | `ruff`, `mypy --strict`   | every commit          | < 15s          |
| import boundaries    | `import-linter`           | every commit          | < 10s          |
| tenant isolation     | `pytest tests/tenant/`    | every PR              | < 60s          |
| unit                 | `pytest`                  | every PR              | < 60s          |
| frontend unit        | `vitest` + `@testing-library/react` + `msw` | every PR | < 60s |
| integration (DB)     | `pytest` + testcontainers | every PR              | < 5min         |
| API contract         | `schemathesis` against `/openapi.json` | every PR | < 5min     |
| browser e2e          | `playwright` (headless)   | every PR              | < 10min        |
| visual regression    | `playwright` + `pixelmatch` | every PR            | < 5min         |
| load                 | `locust`                  | nightly               | 30min          |
| LLM regression       | `pytest` + fixtures       | on-demand + nightly   | varies         |
| security             | `osv-scanner`, `bandit`   | every PR              | < 2min         |
| CLI parity           | `scripts/cli_parity_check.py` | every PR         | < 10s          |

## Unit

- Pure domain, no network, no real DB. Fakes for `Clock`, `Storage`,
  `Mailer`, `LLMClient`.
- **One test package per bounded context**
  (`tests/unit/<context>/`) mirroring `app/domain/<context>/`.
  Each test imports only its context's public surface plus
  in-memory fakes of the context's ports. A test that imports
  from a sibling context's submodule is a code smell and is
  caught by the import-boundary gate below unless the file lives
  under `tests/integration/`.
- Every schedule / RRULE edge case has a parametrized test.
- Money math has property-based tests (`hypothesis`).
- Policy helpers (capability resolution, assignment algorithm, approval
  detection) get exhaustive truth-table tests.

## Import boundaries

Enforced on every commit by `import-linter` (see §01 "Module
boundaries and bounded contexts"). Config lives at
`pyproject.toml` under `[tool.importlinter]`:

- **Layer contract.** `domain` layer is forbidden from importing
  `adapters`, `api`, `web`, or `worker`.
- **Independence contract.** Each `domain.<X>` is an independent
  module; sibling contexts may import `domain.<Y>` (the package
  `__init__.py`) but not `domain.<Y>.<submodule>`.
- **Shared-kernel allowlist.** `app.util`, `app.audit`,
  `app.tenancy`, `app.events`, and `app.adapters.*.ports` are
  the only modules any layer may import freely.
- **Handler thinness.** `app.api.v1.<X>` may import only
  `domain.<X>` (its own context's public surface) plus the
  shared-kernel. Cross-context orchestration in a handler fails
  the gate — it belongs in a domain service.

A violation fails CI. There is no skip flag; the fix is either
to move the code to the right module or to promote the function
to a public surface.

## Tenant isolation

A dedicated test package `tests/tenant/` seeds two workspaces
and verifies that code in one cannot reach rows in the other.

### Cross-tenant regression test

The test fixture seeds two workspaces `A` and `B` in a single test
database with distinct, colliding data (same property names, same
user emails where uniqueness allows, same scheduling windows) so
that an accidental cross-tenant read is visible as a wrong row,
not merely an empty one. The test runs **nightly on both SQLite
AND Postgres** (via `testcontainers`) and gates the CI
`surface-parity` check: any new repository method, background
job, or event subscription added without extending one of the
cases below fails the gate.

Four cases, each applied exhaustively to the relevant surface:

- **(a) HTTP surface.** For every resource endpoint listed in
  `_surface.json`, issue the request under a token scoped to
  workspace `A` with a path prefix for workspace `B`
  (`/w/<slug-B>/api/v1/...`). The test asserts `404 not_found`.
  Never `200`, never `403` — a `403` leaks workspace existence.
  The assertion is byte-for-byte on the error envelope to catch
  timing-side-channel differences too (see §15 "Constant-time
  cross-tenant responses"). Across a sample of **at least 100
  endpoints** drawn from `_surface.json`, the test also asserts
  **byte-identical error envelopes** (identical body bytes,
  identical header set) and **timing bands that overlap within
  ±5 ms** between the "resource does not exist" case and the
  "resource exists but belongs to workspace B" case, under the
  steady-load harness described in §15.
- **(b) Background jobs.** For every worker job —
  `task_generator`, `ical_poller`, `digest_composer`,
  `anomaly_detector`, `retry_failed_webhooks`, `prune_sessions`,
  `rotate_audit_log`, `refresh_exchange_rates`, plus every job
  registered via the worker registry — inject a
  `WorkspaceContext(workspace_id=B)` and intercept the job's SQL
  via a SQL assertion harness (e.g. `sqlalchemy.event.listen` on
  `before_cursor_execute`). Assert every emitted statement
  targeting a workspace-scoped table carries a
  `WHERE workspace_id = 'B'` clause (or the equivalent bound
  parameter). Statements without a workspace filter — or with a
  filter bound to `A` while the context is `B` — fail the test.
- **(c) Event subscriptions.** For every event kind published on
  the SSE/`/events` bus and every internal in-process subscription
  (digest triggers, anomaly triggers, webhook dispatchers), publish
  an event scoped to workspace `A` and assert that subscribers
  bound to workspace `B` do not receive it. The test subscribes
  concurrently under both contexts and fails on any cross-delivery.
- **(d) Repository parity.** For every workspace-scoped
  repository method across every context, a parametrised case
  asserts that a caller with `WorkspaceContext(workspace_id=A)`
  cannot read, write, soft-delete, or restore a row with
  `workspace_id=B`. Runs on both SQLite (app-filter only) and
  Postgres (RLS + app-filter).

The surface-parity CI gate expands the reverse check (§ "Quality
gates") with three clauses:

- Every `operationId` in `_surface.json` must have an (a) case.
- Every job class registered in the worker registry must have a
  (b) case.
- Every event kind declared in the event registry must have a
  (c) case.

Gaps fail the gate; the fix is either to extend the tenant test
suite or to explain why the surface is genuinely tenant-agnostic
(deployment-scope, identity-scope — both recorded as explicit
opt-outs in `tests/tenant/_optouts.py` with a `# justification:`
comment).

### Additional checks

- **RLS enforcement.** For Postgres, the test clears
  `current_setting('crewday.workspace_id')` mid-transaction and
  asserts every subsequent query raises rather than silently
  returns cross-tenant rows.
- **Cross-workspace visibility (shared properties).** The
  `property_workspace` narrowing rules in §15 are exercised: a
  property shared from A to B with `share_guest_identity = false`
  hides guest name, email, phone; the cross-workspace `users`
  projection (§15 "Cross-workspace user identity") returns first
  name + last initial and nulls for PII columns. Runs on both
  backends.
- **URL enumeration.** HTTP-level tests hit
  `/w/<slug-B>/api/v1/...` with a session authenticated in
  workspace A and assert `404` with a constant-time response
  (see §15).
- **Parity check.** A lint pass walks
  `app/adapters/db/*/repository.py` and fails if a new public
  method is not covered by the tenant test suite.

## Integration

- Real SQLite file and a spun-up Postgres via `testcontainers`. Test
  matrix runs everything on both.
- A single `conftest.py` fixture gives each test its own DB.
- Migrations run once per worker; snapshots + truncate between tests
  for speed.
- Real filesystem Storage in `tmp_path`.
- Real Jinja template render tests for every email template.

## API contract

- `schemathesis run --checks all ./openapi.json` against a live dev
  server seeded with fixture data.
- Custom hooks enforce: `Authorization` present on non-public paths,
  idempotency honored, `ETag` round-trip.
- Breaking-change detection: `openapi-diff` between the current branch
  and `main` runs in CI; a breaking diff fails unless PR body contains
  `ALLOW-BREAKING-API`.

## End-to-end

- Playwright, Python bindings. Headed only locally; headless in CI.
- Covered journeys (minimum for GA):
  1. Install + first-boot owner enrollment.
  2. Add property, area, work_role, user; invite user; user enrolls
     passkey and completes first task.
  3. iCal feed imports stays; turnover tasks auto-generate; worker
     completes with photo evidence; guest opens welcome link.
  4. Expense submission with receipt; autofill population; approval;
     payslip issuance with reimbursement.
  5. Agent drives a task lifecycle via the CLI; action requiring
     approval is queued and approved.
- Passkey ceremonies are exercised via Chromium's
  [WebAuthn virtual authenticator](https://playwright.dev/docs/api/
  class-cdpsession). The e2e compose override aligns
  `CREWDAY_PUBLIC_URL` / `CREWDAY_WEBAUTHN_RP_ID` with
  `http://localhost:8100` so the browser origin satisfies WebAuthn's
  RP-ID rule. The e2e stack remains bound to loopback; `localhost` is
  used because Chromium rejects IP literals as RP IDs. WebKit remains
  in the authenticated route smoke matrix, but passkey ceremony
  coverage is Chromium-only until a WebKit virtual-authenticator
  driver exists in the suite.
- **360 px viewport sitemap** (§14 "Native wrapper readiness"): the
  full authenticated sitemap — worker + manager shells — is walked
  at a 360×780 viewport and fails on any horizontal scroll, any
  tap target < 44×44, or any unreachable nav entry. This is the
  web-platform side of the native-wrapper contract; the native
  shell later consumes it as a black box.

## Frontend

### Unit

- **vitest** + **@testing-library/react** for component and hook tests.
- **msw** (Mock Service Worker) intercepts `fetch` at the network level
  for request-level mocking in unit and integration tests — no actual
  HTTP traffic, no stubs in application code.

### Visual regression

- **Playwright** + **pixelmatch** for pixel-level comparison.
- `/styleguide` (dev + staging only) is the visual-regression baseline.
  A screenshot diff > **0.1%** on `/styleguide` fails the check.
  All other routes fail on > **0.5%** diff.
- Baselines are committed and updated intentionally; CI fails on any
  unreviewed diff.

## Load

- Locust scenarios:
  - "10 users clocking in at 08:00"
  - "Task list render for a property with 100k tasks history"
  - "Turnover day: 5 simultaneous completions with photo uploads"
- Pass criteria in §00 (success metrics) drive the budgets.

## LLM regression

- Fixture set per capability: receipts (good, bad, multi-page, non-
  English), intake strings, digests (happy, quiet day, anomalies).
- Expected shapes asserted via Pydantic; numeric fields allowed to
  drift within configured tolerance.
- `pytest -k llm` with `--replay` uses recorded cassettes; `--live`
  calls OpenRouter. Cassettes regenerated on demand.

## Quality gates (PR required)

- `ruff check`
- `ruff format --check`
- `mypy --strict`
- `pytest unit`
- `pytest integration` (SQLite + PG)
- `schemathesis`
- `playwright` smoke (the two shortest journeys above)
- `osv-scanner` (blocker on any unresolved high/critical)
- `bandit -ll`
- OpenAPI diff
- `cli-parity` (surface freshness + completeness + reverse check +
  operationId lint) — four checks:
  1. **Surface freshness** — regenerate `_surface.json` from current
     app, diff against committed version. Fail if stale.
  2. **Parity completeness** — every `operationId` in `openapi.json`
     must appear in `_surface.json` commands, exclusions, or override
     `covers=` declarations. Fail if any uncovered.
  3. **Reverse parity** — every `operationId` referenced in
     `_surface.json` must exist in `openapi.json`. Fail if a CLI
     command points at a removed endpoint.
  4. **operationId lint** — format must be
     `^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$`, first segment must be a
     known CLI group.
- `openapi-agent-annotations` — every **mutating** route in
  `openapi.json` (`x-cli.mutates = true`, or a non-
  `GET`/`HEAD`/`OPTIONS` method when the flag is absent) MUST
  carry exactly one of `x-agent-confirm`, `x-agent-forbidden`, or
  `x-interactive-only` (§12 "Rule for mutating routes"). A route
  carrying zero, or two or more, fails the gate. The check runs
  against the committed `docs/api/openapi.json` so reviewers see
  the annotation in the PR diff.
- Coverage threshold: 85% domain, 70% overall; tracked via codecov.

## Release gates

In addition to PR gates:

- Full Playwright journey suite.
- Full Locust load.
- Migration replay against a sanitized prod-like snapshot.
- SBOM generation (CycloneDX).
- Image signed with cosign.
- **Image non-root smoke test.** A CI step starts the release image
  with the stock entrypoint (no `--user` override), execs
  `id -u` inside it, and fails the build unless the result is
  non-zero. A second step runs `docker run --rm --user 0 <image>
  crewday-server serve` and asserts the process exits non-zero
  with the "refuses to run as root" error from §16. Both checks
  guard against regressions where a Dockerfile change drops the
  `USER crewday` directive or an orchestrator forces uid 0.

## Reproducibility

- `uv.lock` is the source of truth for Python deps.
- Dockerfile uses `--mount=type=cache` for `pip`/`uv` and `apt`.
- CI builds on Linux amd64 and arm64.
- Release notes include the exact image digest.

## Test data

- `crewday admin demo` seeds a realistic household for dev and e2e:
  - Main residence (Villa Sud, FR), vacation home (Chalet Alpe),
    one STR (Apt 3B Barcelona).
  - 5 employees across roles.
  - 30 task templates, 12 schedules.
  - 20 stays imported (synthetic iCal).
- Seeded deterministically from a single `--seed` integer so tests
  can reproduce.
