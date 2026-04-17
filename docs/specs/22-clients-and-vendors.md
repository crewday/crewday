# 22 — Clients, vendors, work orders, and billing

miployees started life as a **single-employer, single-household**
operations tool. Real deployments extend beyond that shape:

- A **cleaning / property-management agency** whose workspace owns
  many external maids and serves many paying clients; the agency
  collects money from clients and pays its workers.
- A **household-as-client** whose workspace hires one or more
  external contractors (repair handymen, drivers) alongside (or
  instead of) its own payroll staff.
- A **mixed** setup: a workspace with payroll employees, one-off
  contractors invoicing for specific jobs, and a handful of agency-
  supplied workers routed through a third-party vendor.

This section defines the entities, flows, and invariants that make
those shapes first-class while preserving the existing "family with
a maid" single-workspace default.

## Design summary

- Properties may belong to a **billing client** — a row in the
  unified `organization` table — via nullable
  `property.client_org_id`. When null, the workspace itself is the
  implicit billing target (the pre-existing behaviour).
- Employees carry an **`engagement_kind`** that decides which pay
  pipeline they are on: payroll (payslips), contractor (vendor
  invoices), or agency-supplied (vendor invoices billed to the
  supplying organization rather than the worker).
- Work that gets billed externally — whether to a client, or by a
  contractor — is grouped into a **`work_order`**: an optional
  parent of one or more tasks, under which a **`quote`** and one or
  more **`vendor_invoice`** rows live.
- Agency billing is captured as **billable rates per (client, role)**
  with an optional per-employee override; shifts carry a derived
  `client_org_id` for fast rollup and CSV export. v1 exports CSV;
  rendering a client-facing invoice PDF is deferred (see §19).
- All money-routing decisions made by agents — accepting a quote,
  approving a vendor invoice, marking one paid — are
  **unconditionally approval-gated** (§11), like
  `payout_destination.*`.

## `organization`

A counterparty of the workspace. May be a **client** (we bill them),
a **supplier** (they bill us, typically because they supply workers),
or both. One table, role flags.

| field              | type      | notes                                                                    |
|--------------------|-----------|--------------------------------------------------------------------------|
| id                 | ULID PK   |                                                                          |
| workspace_id       | ULID FK   | scoping                                                                  |
| name               | text      | display name ("Dupont family", "CleanCo SARL")                            |
| legal_name         | text?     | distinct from display when needed for invoices                            |
| is_client          | bool      | true if this org pays the workspace                                       |
| is_supplier        | bool      | true if this org supplies workers to the workspace                        |
| default_currency   | text      | ISO 4217; defaults to workspace default                                   |
| address_json       | jsonb     | same canonical shape as `property.address_json` (§04)                    |
| contacts_json      | jsonb     | array of `{label, name, email, phone_e164, role}` — free-form contacts   |
| tax_id             | text?     | VAT / SIRET / EIN as relevant — displayed on invoices                     |
| notes_md           | text?     | manager-visible                                                           |
| portal_user_id     | ULID FK?  | future seam (§ "Client surface" below); null in v1                        |
| default_pay_destination_id | ULID FK? | for suppliers: where vendor_invoice payments route by default (§09) |
| created_at/updated_at | tstz    |                                                                          |
| deleted_at         | tstz?     | soft delete                                                               |

**Invariants.**

- At least one of `is_client` / `is_supplier` must be true.
  Write-time check; 422 otherwise.
- `default_pay_destination_id` must reference a `payout_destination`
  whose `organization_id = this.id` (see §09 "Payout destinations
  for organizations").
- Unique on `(workspace_id, legal_name)` when `legal_name` is set;
  unique on `(workspace_id, name)` always. Prevents duplicate org
  rows from ambiguating invoice routing.

### Starter rows

None — a fresh workspace has no organizations and no clients. The
workspace remains the implicit billing target for its own properties.
Organizations are created lazily when the manager enters "agency
mode" by linking a property to a client, or registers a supplier to
route agency-supplied workers.

## `property.client_org_id`

Added to the `property` row (§04):

| field         | type     | notes                                                    |
|---------------|----------|----------------------------------------------------------|
| client_org_id | ULID FK? | `organization.id` where `is_client = true`. Nullable.    |

- **Null** = workspace-owned, self-managed. Vendor invoices for
  work at this property are paid by the workspace directly; no
  client-billing rollup.
- **Set** = billable to that client. Shifts and work orders at this
  property carry the client forward; billable-rate resolution
  consults `client_rate` / `client_employee_rate` (below).
- A property can only have one `client_org_id` at a time. Properties
  that are genuinely co-owned and split-billed are a deliberate
  non-goal in this iteration; see §19 "Beyond v1" for split-billing.
- Write-time check: the referenced org must have `is_client = true`
  and belong to the same `workspace_id`.

## Engagement kind

Added to the `employee` row (§05):

| field            | type     | notes                                                              |
|------------------|----------|--------------------------------------------------------------------|
| engagement_kind  | enum     | `payroll | contractor | agency_supplied`. Default `payroll`.        |
| supplier_org_id  | ULID FK? | `organization.id` where `is_supplier = true`. Required iff `engagement_kind = agency_supplied`, else null. |

Semantics:

- **`payroll`** — existing behaviour. Gets `pay_rule`, accrues into
  `pay_period`, is paid via `payslip`. May not have a
  `supplier_org_id`.
- **`contractor`** — an independent worker who **bills us**. Does
  not get `pay_rule` / `payslip`; payment flows through
  `vendor_invoice` rows. May hold their own `payout_destination`
  rows (the vendor invoice routes to one of them).
- **`agency_supplied`** — a worker provided by a third-party agency.
  The **agency** bills us; the worker's `supplier_org_id` points at
  the supplier. Vendor invoices for this worker route by default to
  `supplier_org.default_pay_destination_id`, not to a destination
  owned by the worker.

An employee's `engagement_kind` is not immutable but changes are
audited and gated: switching a row from `payroll` to `contractor`
requires that the employee have no `pay_rule` active on or after the
switch date, and any open `pay_period` with shifts for them must be
locked or drained. The reverse switch (contractor → payroll)
requires at least one `pay_rule` to be created in the same
transaction.

### UI and assignment

`engagement_kind` does **not** affect task assignment, shifts,
capabilities, or anything in §05 / §06 — the worker is still an
employee row with roles, property assignments, clock-mode, and
evidence policy. It only affects the pay pipeline and which UI
surfaces the person appears in.

## Billable rates

Rates the workspace bills a client for work done by its employees.
Parallel to `pay_rule` (what we pay the worker) but oriented the
other way.

### `client_rate` (per client × role)

| field              | type     | notes                                       |
|--------------------|----------|---------------------------------------------|
| id                 | ULID PK  |                                             |
| workspace_id       | ULID FK  |                                             |
| client_org_id      | ULID FK  | must have `is_client = true`                |
| role_id            | ULID FK  | §05                                         |
| currency           | text     | ISO 4217                                    |
| hourly_cents       | int      |                                             |
| effective_from     | date     |                                             |
| effective_to       | date?    | null = ongoing                              |
| notes_md           | text?    |                                             |

Unique: `(client_org_id, role_id, effective_from)`.

### `client_employee_rate` (per client × employee override)

| field              | type     | notes                                       |
|--------------------|----------|---------------------------------------------|
| id                 | ULID PK  |                                             |
| workspace_id       | ULID FK  |                                             |
| client_org_id      | ULID FK  |                                             |
| employee_id        | ULID FK  |                                             |
| currency           | text     |                                             |
| hourly_cents       | int      |                                             |
| effective_from     | date     |                                             |
| effective_to       | date?    |                                             |

Unique: `(client_org_id, employee_id, effective_from)`.

### Rate resolution

For a shift with `(client_org_id, employee_id)` and date `d`:

1. `client_employee_rate` matching `(client_org_id, employee_id)`
   with `effective_from ≤ d < coalesce(effective_to, ∞)`.
2. For each role the employee holds at the shift's property:
   `client_rate` matching `(client_org_id, role_id)` with the same
   effective-range test. If multiple roles resolve to different
   rates, the **highest-priority role** wins (roles have an
   implicit priority by `role.key` in the catalog; managers may
   override per client with `client_rate.priority` — deferred).
3. No match → the shift is **not billable** to this client and its
   hours surface in a "unpriced" bucket in the rollup CSV so the
   manager can fix the rate card.

Rate resolution happens at **shift close time** and is snapshotted
onto a new `shift_billing` row (below) so later rate-card edits do
not retroactively rewrite history.

### `shift_billing`

A derived, append-only row per `(shift, client_org_id)` pair,
written when a shift closes against a property with
`client_org_id IS NOT NULL`.

| field              | type     | notes                                                |
|--------------------|----------|------------------------------------------------------|
| id                 | ULID PK  |                                                      |
| workspace_id       | ULID FK  |                                                      |
| shift_id           | ULID FK  |                                                      |
| client_org_id      | ULID FK  | denormalised from `property.client_org_id` at close  |
| employee_id        | ULID FK  |                                                      |
| currency           | text     |                                                      |
| billable_minutes   | int      | = duration minus breaks                              |
| hourly_cents       | int      | snapshot of the resolved rate                        |
| subtotal_cents     | int      | `billable_minutes / 60 * hourly_cents`, rounded      |
| rate_source        | enum     | `client_employee_rate | client_rate | unpriced`      |
| rate_source_id     | ULID?    | id of the resolving rate row; null when `unpriced`   |

Editing a shift's time fields (`adjusted = true` in §09)
re-derives its `shift_billing` row inside the same transaction.
Archiving a property or client never removes `shift_billing` rows;
they are historical.

## `work_order`

A billable envelope wrapping one or more tasks. Optional — a casual
one-off repair can skip work_order entirely and just have a task
with a single attached `vendor_invoice`. Work orders are for jobs
worth quoting up front or that span multiple tasks.

| field                     | type     | notes                                                      |
|---------------------------|----------|------------------------------------------------------------|
| id                        | ULID PK  |                                                            |
| workspace_id              | ULID FK  |                                                            |
| property_id               | ULID FK  | the location of the work                                   |
| client_org_id             | ULID FK? | derived cache from `property.client_org_id` at creation    |
| asset_id                  | ULID FK? | §21 — set when the work is about a specific asset          |
| title                     | text     | "Replace pool pump seal"                                   |
| description_md            | text     |                                                            |
| state                     | enum     | `draft | quoted | accepted | in_progress | completed | cancelled | invoiced | paid` |
| assigned_employee_id      | ULID FK? | the contractor / agency-supplied worker doing the work     |
| requested_by_manager_id   | ULID FK? | who opened the work order                                  |
| currency                  | text     | ISO 4217; defaults to property currency                    |
| accepted_quote_id         | ULID FK? | set when a quote is accepted; null otherwise               |
| accepted_at               | tstz?    |                                                            |
| completed_at              | tstz?    |                                                            |
| cancellation_reason       | text?    |                                                            |
| notes_md                  | text?    |                                                            |
| created_at/updated_at     | tstz     |                                                            |
| deleted_at                | tstz?    |                                                            |

### State machine

```
draft → quoted → accepted → in_progress → completed → invoiced → paid
  └───────────────────┘            ↑                    ↑
                                   └────────────────────┘
                                 (may skip `quoted`/`accepted`
                                  when the manager invoices
                                  directly without a quote)
cancelled is reachable from any non-terminal state.
```

Tasks referencing a work_order (`task.work_order_id FK?`) inherit
its `assigned_employee_id` as a default but may be re-assigned; all
such tasks appear grouped under the work_order in the manager UI.

### Invariants

- `currency` must equal `property.default_currency` at creation —
  multi-currency work_orders are out of scope (same rationale as
  per-period single-currency payroll in §09).
- Transitioning `draft → quoted` requires at least one `quote` row
  with `status = submitted`.
- Transitioning `quoted → accepted` is an approvable action
  (§11 "Which actions"): `work_order.accept_quote`. The manager
  picks exactly one submitted quote; the work_order records
  `accepted_quote_id`, the chosen quote flips to `accepted`, all
  other submitted quotes on the same work_order flip to
  `superseded` in the same transaction.
- `completed → invoiced` requires at least one `vendor_invoice` row
  with `status ≥ submitted`.
- `invoiced → paid` is set automatically when all vendor invoices
  on the work_order reach `status = paid`.

## `quote`

A worker-proposed price for a work_order. The quoting worker is
almost always a `contractor` or `agency_supplied` employee; payroll
employees usually don't quote (their labour is already paid for),
but the model does not forbid it — a salaried handyman may submit a
quote for a genuinely outside-scope job.

| field                | type     | notes                                                         |
|----------------------|----------|---------------------------------------------------------------|
| id                   | ULID PK  |                                                               |
| workspace_id         | ULID FK  |                                                               |
| work_order_id        | ULID FK  |                                                               |
| employee_id          | ULID FK  | who submitted the quote                                       |
| currency             | text     | must equal `work_order.currency`                              |
| subtotal_cents       | int      | sum of line totals                                            |
| tax_cents            | int      | informational; local tax behaviour is out of scope            |
| total_cents          | int      | `subtotal + tax`                                              |
| lines_json           | jsonb    | see shape below                                               |
| valid_until          | date?    | informational; system does not auto-expire                    |
| status               | enum     | `draft | submitted | accepted | rejected | superseded | expired` |
| submitted_at         | tstz?    |                                                               |
| decided_at           | tstz?    |                                                               |
| decided_by_manager_id| ULID FK? |                                                               |
| decision_note_md     | text?    |                                                               |
| attachment_file_ids  | ULID[]   | PDFs/photos of the worker's own quote document                |
| llm_autofill_json    | jsonb?   | reserved for future OCR of PDF quotes                         |
| created_at/updated_at| tstz     |                                                               |
| deleted_at           | tstz?    |                                                               |

### `lines_json` shape

```json
{
  "schema_version": 1,
  "lines": [
    {"kind": "labor",    "description": "Diagnosis + repair (3h)",
     "quantity": 3, "unit": "hour", "unit_price_cents": 6000, "total_cents": 18000},
    {"kind": "material", "description": "Replacement seal (OEM)",
     "quantity": 1, "unit": "unit", "unit_price_cents": 2400, "total_cents": 2400},
    {"kind": "travel",   "description": "Call-out fee",
     "quantity": 1, "unit": "unit", "unit_price_cents": 3500, "total_cents": 3500}
  ]
}
```

`kind` is free-form for display; v1 suggests `labor | material |
travel | other`. `total_cents` is recomputed server-side from
`quantity * unit_price_cents` on write; a mismatch raises 422.

### Acceptance

`quote.accept` is **unconditionally approval-gated** (§11): an
agent cannot accept a quote, even if it holds `expenses:approve` or
any other scope. The manager-side approval UI shows the quote,
attachments, and the resolved `work_order` context. Acceptance
writes `quote.status = accepted`, `work_order.accepted_quote_id =
this.id`, and `work_order.state = accepted`. Subsequent
`vendor_invoice` rows on the same work_order validate that
`total_cents ≤ accepted_quote.total_cents` **plus a workspace-
configurable tolerance** (default 10 %); overruns raise a soft
warning at invoice submission that the manager sees before
approving, but are not hard-blocked.

### Supersession and rejection

- Submitting a new quote on a work_order already in state `quoted`
  leaves prior submitted quotes in `submitted`; only manager
  acceptance collapses the set.
- Explicit `quote.reject` is available; sets `status = rejected`
  and records a reason. The work_order remains in `quoted` if
  other submitted quotes exist, else transitions back to `draft`.
- `expired` is a manual status the manager may set when a quote
  with a `valid_until` in the past is no longer usable; the system
  does not auto-expire, to avoid silent state changes.

## `vendor_invoice`

What the worker or supplier actually bills. Parallel in spirit to
`expense_claim` (§09) — OCR autofill, attachments, manager approval
— but the counterparty is the biller, not the submitting employee,
and payment flows to a `payout_destination` chosen at approval
time.

| field                  | type     | notes                                                            |
|------------------------|----------|------------------------------------------------------------------|
| id                     | ULID PK  |                                                                  |
| workspace_id           | ULID FK  |                                                                  |
| work_order_id          | ULID FK? | nullable — a one-off repair can carry an invoice without an explicit work_order |
| property_id            | ULID FK  | required when `work_order_id` is null                            |
| vendor_employee_id     | ULID FK? | exactly one of `vendor_employee_id` / `vendor_organization_id` is set |
| vendor_organization_id | ULID FK? | set when the biller is the supplier org (agency_supplied workers) |
| billed_at              | date     | on the invoice                                                   |
| due_on                 | date?    |                                                                  |
| currency               | text     |                                                                  |
| subtotal_cents         | int      |                                                                  |
| tax_cents              | int      |                                                                  |
| total_cents            | int      |                                                                  |
| lines_json             | jsonb    | same shape as `quote.lines_json`                                 |
| payout_destination_id  | ULID FK? | where the money will go; see resolution below                    |
| exchange_rate_to_default | numeric? | snapshot at approval, like expense_claim                       |
| status                 | enum     | `draft | submitted | approved | rejected | paid | voided`         |
| submitted_at           | tstz?    |                                                                  |
| approved_at            | tstz?    |                                                                  |
| decided_by_manager_id  | ULID FK? |                                                                  |
| decision_note_md       | text?    |                                                                  |
| paid_at                | tstz?    |                                                                  |
| paid_by_manager_id     | ULID FK? |                                                                  |
| paid_reference         | text?    | bank reference, free-form                                        |
| attachment_file_ids    | ULID[]   | PDF / photo of the worker's invoice                              |
| llm_autofill_json      | jsonb?   | see §09 expense autofill; shape is the same, vendor field refers to biller |
| autofill_confidence_overall | numeric? |                                                             |
| created_at/updated_at  | tstz     |                                                                  |
| deleted_at             | tstz?    |                                                                  |

### Invariants

- Exactly one of `vendor_employee_id` / `vendor_organization_id` is
  set; 422 otherwise.
- For an `agency_supplied` employee, the server **rejects** an
  invoice written with `vendor_employee_id = employee.id`: the
  invoice must be written with
  `vendor_organization_id = employee.supplier_org_id`. Rationale:
  the supplying agency bills us, not the individual worker.
  Conversely, a `contractor` employee must be billed via
  `vendor_employee_id`, not through an organization.
- `currency` on submission must equal `work_order.currency` when
  `work_order_id IS NOT NULL`.
- Approving an invoice snapshots the exchange rate against the
  workspace default currency (ECB daily fix at approval time),
  same mechanism as `expense_claim` (§09).

### Payout destination resolution

On approval, if `payout_destination_id` is null the server fills it
by walking:

1. If `vendor_employee_id` is set: the employee's
   `pay_destination_id` (§09). The employee must be `contractor`
   kind; payroll employees' pay destinations are for payslips only
   and using them here is a 422 with
   `error = "payroll_destination_not_billable"`.
2. If `vendor_organization_id` is set: the org's
   `default_pay_destination_id`. 422 if null — the manager must
   set one before approving.

The chosen destination is recorded on the invoice (immutable after
approval). The **approval step itself** is the money-routing
decision, and accordingly `vendor_invoice.approve` is on the
unconditionally approval-gated list (§11) — an agent can submit,
attach, and draft the invoice, but cannot commit payment routing.
`vendor_invoice.mark_paid` (the `approved → paid` transition) is
also unconditionally gated.

### Approval flow

Identical to `expense_claim.approve` in shape (§09) with three
differences:

- The "requester" in approvable-action audit is the submitting
  agent's delegating user (same rule as elsewhere), but the
  invoice's **biller** is captured separately in
  `vendor_employee_id` / `vendor_organization_id` for audit
  clarity.
- Approval with a non-null `payout_destination_id` provided by the
  agent raises the same gate as `expense_claim.set_destination_override`
  — the manager must re-confirm the chosen destination in the
  approval UI.
- Unlike expense_claim, vendor_invoice does **not** roll into a
  payslip. It is paid directly; `paid_at` is set when the manager
  clicks "Mark paid" after pushing funds from their bank. `paid`
  is distinct from `approved` so the workspace can track an
  account-payable queue.

### Relationship to payroll employees

A **payroll** employee (default `engagement_kind`) **cannot** be
the biller of a `vendor_invoice`. Their labour is paid through
payslips, not invoices. Attempts to write such a row with
`vendor_employee_id` pointing at a payroll employee return 422
`error = "payroll_employee_not_billable"`. The manager may change
the employee's `engagement_kind` to `contractor` for a specific
off-cycle job, but that is explicit — no silent promotion.

## Payout destinations for organizations

Extends `payout_destination` (§09) so destinations can be owned by
either an employee **or** an organization. See §09 for the full
extension; in summary:

- `payout_destination.employee_id` becomes nullable.
- A new nullable `payout_destination.organization_id` is added.
- Exactly one of the two must be set; DB-level CHECK constraint.
- The existing per-employee rules (read/write authority scoped to
  the owner, approval gate on mutation, IBAN checksum, snapshot on
  use) apply identically when the owner is an organization, with
  "the org's manager" reading as "any workspace manager".

## Billable-hour rollup and exports

v1 ships **rate capture and CSV export** only. Full PDF client
invoices with a state machine are deferred (§19).

### CSV: billable hours by client

`GET /api/v1/exports/client_billable.csv?client_org_id=...&from=YYYY-MM-DD&to=YYYY-MM-DD`

One row per `(client_org_id, employee_id, role_id, date)`:

```
client_org_id, client_name, employee_id, employee_name, role_key,
date, hours, hourly_cents, currency, subtotal_cents, rate_source
```

Unpriced hours (see "Rate resolution") are exported with
`hourly_cents = null` and `rate_source = unpriced` so they are
visible rather than silently dropped.

### CSV: work-order ledger

`GET /api/v1/exports/work_orders.csv?...`

One row per work_order with aggregate quote and invoice totals,
state, client, and asset. Useful for agency managers reconciling
what was quoted vs. what was billed.

### CLI

Mirrors §13 conventions: `miployees exports client_billable
--client ... --from ... --to ...` and `miployees exports
work_orders ...`.

## Client surface (no portal in v1)

Clients are internal-only records in v1. `organization.portal_user_id`
is reserved as a future seam: when the "Owner-only dashboard" item
in §19 ships (either Beyond-v1 or earlier as a Phase 11), a
`client_user` actor kind can be linked to an organization row
without schema migration, gaining read access to their own
properties, shifts, quotes, and invoices. Adding that seam today
has no runtime effect; it just documents the intended shape so we
don't paint ourselves into a corner.

Existing guest-link mechanics (§04) are unrelated and continue to
serve per-stay welcome pages only.

## Approvable actions added

Appended to §11 "Always-gated (not configurable)":

- `work_order.accept_quote`
- `vendor_invoice.approve`
- `vendor_invoice.mark_paid`
- `organization.update_default_pay_destination`
- `employee.set_engagement_kind` (when switching *to* or *from*
  `payroll`, because it moves the worker between pay pipelines)

The same rationale as existing money-routing gates: agents can
draft, attach, and propose; humans decide who gets paid.

## Webhook events added

Appended to §10's catalog:

- `organization.created`, `organization.updated`,
  `organization.archived`.
- `client_rate.created`, `client_rate.updated`,
  `client_rate.archived`.
- `work_order.state_changed` with `from` / `to` in the payload.
- `quote.submitted`, `quote.accepted`, `quote.rejected`,
  `quote.superseded`.
- `vendor_invoice.submitted`, `vendor_invoice.approved`,
  `vendor_invoice.rejected`, `vendor_invoice.paid`.
- `shift_billing.resolved` (fires when a shift closes and the
  billing row is written; carries the `rate_source` so a dashboard
  can surface unpriced shifts).

## Audit actions added

`organization.create`, `.update`, `.archive`;
`client_rate.create`, `.update`; `client_employee_rate.create`,
`.update`; `work_order.create`, `.state_change`,
`.accept_quote`; `quote.submit`, `.accept`, `.reject`,
`.supersede`; `vendor_invoice.submit`, `.approve`, `.reject`,
`.mark_paid`.

## Out of scope (v1)

- **Split-billing a single property across multiple clients.** One
  property = one client. Co-ownership is modelled as two properties
  if genuinely necessary.
- **Client-facing PDF invoices + dunning + ageing reports.** Ship
  rates + CSV; render PDFs when a real agency asks for it.
- **Multi-currency within a single work_order.** Same rationale as
  §09 single-currency-per-pay-period.
- **Payment execution.** miployees does not move money — vendor
  invoices and payslips alike produce routing metadata; operators
  push funds from their bank and mark the row paid.
- **Real-time tax calculation.** Invoices and quotes carry an
  informational `tax_cents` line; computing VAT / sales tax from
  jurisdiction rules is a future localisation module.
