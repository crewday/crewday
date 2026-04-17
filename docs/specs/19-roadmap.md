# 19 — Roadmap

Phased delivery plan. Phases are budget-capped goals, not fixed
sprints. A phase ships when its goals are met and its quality gates
(§17) pass on `main`.

## Phase 0 — Project scaffolding

- Repo layout (§01), tooling (`uv`, `ruff`, `mypy`, `pytest`,
  `playwright`, Alembic, Caddy compose).
- `AGENTS.md`, `CLAUDE.md`, skill files, CI pipeline.
- Empty FastAPI app with `/healthz`, `/readyz`, `/version`.
- Vite + React + TS strict pipeline; styleguide page; all 35 mock
  routes at parity; SPA served by FastAPI at `127.0.0.1:8100`.
- Docker images; single + compose recipes baseline.

**Exit:** CI green on empty build; `miployees admin init` creates a
workspace row and prints a magic link in the dev profile.

## Phase 1 — Identity

- Passkeys (managers + employees), magic links, sessions.
- API tokens with scopes + per-token audit.
- Audit log core.
- Basic manager UI: profile, passkeys, tokens.

**Exit:** a manager and an employee can be enrolled end-to-end on
devices; a token can drive the API; every action appears in the audit
log.

## Phase 2 — Places and people

- Properties, **units within properties**, areas, employees, roles,
  capabilities.
- Property detail manager UI (incl. unit management for multi-unit
  properties).
- Employee profile and capability management.
- CLI covers all of the above.

**Exit:** full CRUD for the identity+places core; seed demo passes;
CLI generation pipeline produces commands for all Phase 2 endpoints;
`cli-parity` gate green.

## Phase 3 — Tasks and schedules

- Task templates, schedules with RRULE + RDATE/EXDATE, task
  generation worker.
- Task detail and today view for employees.
- Completion, evidence, comments, skip/cancel.
- Assignment algorithm with **availability precedence stack** (leave,
  overrides, holidays, weekly pattern).
- Blackout dates (property closures, employee leave).
- **Employee availability overrides** (self-service add, manager-
  approval reduce).
- **Public holidays with scheduling effects** (`block | allow |
  reduced`).

**Exit:** a weekly recurring task is created by the manager, the
worker generates occurrences, the assigned employee completes them
with evidence, audit trail is complete. Availability overrides and
holidays correctly affect assignment.

## Phase 4 — Instructions

- Instruction CRUD with versioning, scope resolution, attachments,
  linking.
- Task page renders resolved instructions.

**Exit:** a global house rule, a property SOP, and an area safety note
all surface on the right tasks with the right badges.

## Phase 5 — Stays and iCal

- iCal feed polling per provider (Airbnb, VRBO, Booking, generic),
  with per-unit feed mapping.
- Stay model (unit-scoped) + manager UI + calendar.
- **Stay lifecycle rules** (`before_checkin`, `after_checkout`,
  `during_stay`) + auto-generated **stay task bundles** with
  pull-back scheduling.
- Guest welcome page with tokenized URL, unit-aware info merge, and
  check-out checklist.

**Exit:** an imported Airbnb calendar yields correct stay task bundles
(including pre-arrival prep via `before_checkin` rules) with check-out
checklist visible to the guest via the welcome link; pull-back
scheduling correctly moves pre-arrival tasks when the ideal date is
unavailable.

## Phase 6 — Inventory

- Items, movements, consumption on task completion, reorder worker,
  barcode scanner UI.
- Reports (low stock, burn rate).

**Exit:** a stay task bundle consumes inventory; threshold breaches
produce restock tasks; burn-rate report looks right.

## Phase 6b — Assets, actions & documents

- Asset types catalog (system-seeded + workspace-custom).
- Asset CRUD with condition/status tracking, QR tokens.
- Asset actions with scheduling integration (§06).
- Asset documents (manuals, warranties, invoices) with expiry alerts.
- TCO reporting and replacement forecasts.
- Guest-visible assets on the welcome page (§04).

**Exit:** a manager registers a pool pump from the pre-seeded catalog;
recurring maintenance actions generate tasks via the schedule worker;
completing a filter-clean task updates `last_performed_at`; the daily
digest surfaces an expiring warranty; TCO report sums purchase price,
expenses, and document invoices correctly.

## Phase 7 — Time, payroll, expenses

- Shifts with clock-in/out + geofence capability.
- Pay rules, periods, payslips with PDF.
- Expense claims with LLM-powered receipt autofill.
- CSV exports.

**Exit:** a month closes cleanly: shifts → payslips → approved
expenses → reimbursement included → CSV export.

## Phase 7b — Clients, vendors, work orders (§22)

- **`organization`** as a unified client/supplier entity; properties
  gain `client_org_id`; employees gain `engagement_kind` and
  `supplier_org_id`.
- **Client rate cards** (`client_rate` + `client_employee_rate`) and
  shift-close rate snapshotting via `shift_billing`.
- **`work_order`** with child tasks, **`quote`** with manager
  approval gate, **`vendor_invoice`** with OCR autofill and
  approval gate.
- **Payout destinations for organizations** (same model, different
  owner kind).
- CSV exports: **billable hours by client**, **work-order ledger**.
- CLI parity for everything above.

**Exit:** an agency workspace manages three clients, two payroll
employees, one contractor, and one agency-supplied worker; shifts
at a client property produce `shift_billing` rows; a repair job
flows draft → quoted → accepted → in_progress → completed →
invoiced → paid with agent-submitted drafts and manager approvals;
the billable CSV reconciles against the payroll register.

## Phase 8 — LLM features

- OpenRouter client, model assignment table, redaction layer.
- Natural-language task intake, daily digests, anomaly detection,
  staff chat assistant, agent approval workflow.
- Embedded **manager-side** and **employee-side** chat agents (§11)
  with conversation compaction.
- **WhatsApp (agent-mediated) + SMS fallback** for agent-originated
  outbound reach-out (§10). Moved from "Beyond v1" into v1.
- **Chat auto-translation** between employee-preferred and workspace-
  default languages on the employee agent (§10, §18). Moved from
  "deferred" into v1.

**Exit:** all capabilities run against Gemma 4 31B via OpenRouter with
bounded budget and audit; an agent driving the CLI experiences
approval-gated actions correctly; an employee writing in their own
language gets the agent replying in kind and the manager seeing the
workspace-default translation with a toggle for the original;
agent-originated WhatsApp reach-out respects quiet hours and per-
employee daily caps.

## Phase 9 — PWA and offline

- Manifest + service worker.
- Offline task list, queued completions, photo-then-completion
  ordering.

**Exit:** the scripted offline scenario (airplane mode, complete 5
tasks with photos, back online) syncs within 60s with zero loss.

## Phase 10 — Polish and hardening

- Accessibility audit pass (WCAG 2.2 AA).
- Security review (threat model items from §15).
- Performance tune to §00 targets.
- Docs site under `docs/` (Hugo or MkDocs — TBD).
- Release automation (semantic-release), SBOM, cosign.

**Exit:** v1.0.0 tagged. Public release.

## Beyond v1

Items explicitly deferred, in rough priority order:

1. Additional locales (ES, FR, PT-BR, TL) for UI chrome, instruction
   bodies, and digests. Chat auto-translation for the employee
   agent already ships in v1 (see Phase 8).
2. Local LLM provider (Ollama) adapter.
3. **True multi-tenancy** — more than one workspace per deployment,
   with a workspace-switcher UI and workspace-admin roles. The
   **schema is already ready** (every user-editable row carries
   `workspace_id`; junction tables `property_workspace` and
   `employee_workspace` exist; RLS seam is `workspace_id` per §15),
   so lifting the single-workspace lock is a policy + auth change,
   not a data migration. Bundled with SaaS lockout recovery that
   does not require host shell access — see §03.
4. Native mobile apps (only if PWA limitations become painful).
5. QuickBooks / Xero accounting export (beyond CSV).
6. OIDC for managers.
7. Owner-only dashboard (when a second-party manages on behalf of an
   owner). Also promotes `organization.portal_user_id` (§22) into a
   real `client_user` actor kind for client-facing read access.
8. **Client invoice PDFs + ageing / dunning** — full counterpart to
   the payslip PDF flow: render `client_invoice.pdf` from a
   template, run a `draft → issued → paid → voided` state machine,
   attach dunning emails. The v1 agency scope ships rate capture
   and CSV only (§22 "Out of scope"); this is the next increment.
9. **Split-billing a single property across multiple clients** — a
   co-owned villa where two families each pay half. Requires a
   `property_billing_split` mapping and a rewrite of rate
   resolution. Deferred until a real user asks.
10. Realtime chat (presence, typing indicators) — v1 uses SSE for
    task-state freshness; true realtime is separate.
11. Integrated guest messaging (Airbnb-style threads).
12. Additional outbound channels beyond email / WhatsApp / SMS
    (push, Slack, Matrix).
