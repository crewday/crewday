# 09 — Time, payroll, expenses

Three tightly-linked features for staff who expect to get paid
correctly and for managers who want to stop keeping shift notes in a
phone's notes app.

## Time tracking (shifts)

### Model

```
shift
├── id
├── household_id
├── employee_id
├── property_id              # optional; unassigned shifts for remote drivers, etc.
├── status                   # enum: open | closed | disputed (§02)
├── started_at               # utc
├── ended_at                 # utc, nullable while status = open
├── expected_started_at      # nullable; set when clock-in is delayed
├── method_in                # enum: pwa | web | manager | agent | qr_kiosk
├── method_out               # same
├── geo_in_lat/lon/accuracy  # nullable, only if capability + consent
├── geo_out_lat/lon/accuracy
├── break_seconds            # manager-entered or self-entered
├── notes_md                 # optional
├── adjusted                 # bool
├── adjustment_reason        # text? when adjusted == true
├── created_by_actor_kind/id
└── deleted_at
```

`status` transitions: `open` on clock-in; `closed` on clock-out or
manager close; `disputed` when the worker auto-closes an orphan open
shift (see "Open shift recovery").

### Clock-in / clock-out

Capabilities (`time.clock_in`, `time.geofence_required`) gate UI.

- **Clock-in.** Employee taps a big green button on the PWA "home"
  screen; the server records `started_at = now()`, property defaulted
  from today's assigned task (or manually picked). If geofence
  required, the browser's Geolocation API is consulted with a
  configured accuracy threshold; if the user denies or GPS is poor,
  clock-in fails with a clear explanation and an option to request
  a manager override.
- **Clock-out.** Green button flips to red "Clock out". Prompts for
  break time if shift > 6h (configurable). Saves `ended_at`.
- **QR kiosk.** Managers can print a property-specific QR that opens
  a simple clock-in/out page with passkey assertion. Useful when
  staff share a family phone.

### Manager adjustments

Any shift can be adjusted via `PATCH /shifts/{id}` (§12). The server
computes whether the patch touches time fields (`started_at`,
`ended_at`, `break_seconds`, `expected_started_at`):

- If yes: sets `adjusted = true` and requires a non-empty
  `adjustment_reason` in the body; returns 422 otherwise.
- If the patch only touches `notes_md` / `property_id`: does **not**
  set `adjusted`; `adjustment_reason` is optional.

Original values are preserved in `audit_log.before_json` either way.
Employees see "(edited by manager)" on shifts with `adjusted = true`.

### Open shift recovery

If a shift stays open > 16h, the worker emails a reminder to the
employee; if still open at 24h, manager is notified and the system
auto-closes at `started_at + 8h` with `status = disputed` so it shows
up in review.

## Pay rules

A `pay_rule` binds an employee (or an employee_role) to a pay model.

| field              | type      | notes                                 |
|--------------------|-----------|---------------------------------------|
| id                 | ULID PK   |                                       |
| employee_id        | ULID FK?  | OR employee_role_id                   |
| employee_role_id   | ULID FK?  |                                       |
| kind               | enum      | `hourly | monthly_salary | per_task | piecework` |
| effective_from     | date      |                                       |
| effective_to       | date?     | null = ongoing                        |
| currency           | text      | ISO 4217                              |
| hourly_cents       | int?      | kind = hourly                         |
| monthly_cents      | int?      | kind = monthly_salary                 |
| per_task_cents     | int?      | kind = per_task                       |
| piecework_json     | jsonb?    | kind = piecework (units/rates)        |
| overtime_rule_json | jsonb?    | thresholds and multipliers            |
| holiday_rule_json  | jsonb?    | dates + multipliers                   |
| weekly_hours       | int?      | for salary → hourly conversion        |
| notes_md           | text?     |                                       |

Exactly one of `employee_id` / `employee_role_id` is set.

### Overtime rule shape

```json
{
  "daily_threshold_hours": 8,
  "daily_multiplier": 1.5,
  "weekly_threshold_hours": 40,
  "weekly_multiplier": 1.5,
  "sundays_multiplier": 2.0
}
```

All fields optional; unset fields disable that dimension. **We do not
ship jurisdiction-specific defaults** — that way we do not imply legal
compliance we do not offer. Managers enter what they know.

**Daily + weekly interaction.** If a `weekly_threshold_hours` is set,
weekly overtime is computed and daily thresholds are ignored for that
rule, even if `daily_threshold_hours` is also present. Rationale:
compounding (paying both daily and weekly OT on the same hour) is
rarely legal and the max-only alternative is too surprising for
managers who configured both. If a manager actually needs a
compounding scheme, they encode it in `piecework_json` or file two
rules with disjoint effective dates.

### Piecework shape

`piecework_json` on a `pay_rule` with `kind = piecework`:

```json
{
  "lines": [
    { "unit": "turnover", "label": "Standard turnover", "rate_cents": 2500 },
    { "unit": "deep_clean", "label": "Deep clean", "rate_cents": 6000 }
  ],
  "attribution": "task_template"
}
```

`attribution` is `"task_template"` (count completed tasks whose
template name matches `unit`) or `"manual"` (manager enters counts
when closing the period).

### Pay-rule selection when multiple rules overlap

For a `(employee, period)` pair, the applicable rule is the one where:

- `effective_from ≤ period.ends_on`, and
- `effective_to IS NULL OR effective_to ≥ period.starts_on`.

If more than one row satisfies both, the rule with the **greatest
`effective_from`** wins; ULID-sort breaks any remaining tie (newer
row). A rule authored with `kind = piecework` never loses to an
earlier `hourly` rule and vice versa — the rank is on
`effective_from` only.

### Holiday rule shape

```json
{
  "dates": ["2026-05-01", "2026-12-25"],
  "multiplier": 2.0,
  "country_codes_for_suggestions": ["FR"]
}
```

The UI can suggest public holidays from a bundled data file per
country, but the manager copies them into the rule — nothing is
auto-picked, to avoid surprise.

## Pay period

`pay_period {id, household_id, employee_id?, starts_on, ends_on,
frequency, status}`. `status` is the canonical `pay_period_status`
enum in §02 (`open | locked | paid`). `employee_id` is populated when
a household has divergent per-employee pay rules; otherwise null
(the period applies to all employees).

Periods are created per household based on the household's default
frequency (monthly by default; bi-weekly supported). Periods may
overlap across employees when their pay rules diverge.

### Period close

A manager closes a period ("Lock"):

1. Validate: no open shifts remain in the period.
2. Compute `pay_period_entry` rows: per employee, per day, regular
   hours / overtime / holiday / per-task counts / piecework totals.
3. Generate `payslip` rows (`status = draft`).
4. Emit `payroll.period_locked` webhook.

Locked periods cannot be edited; manager can "reopen" with an explicit
audit event, which also resets the contained payslips to `draft`.

### Transition to `paid`

A period moves from `locked` to `paid` automatically: when the last
payslip contained in the period transitions to `status = paid`, the
period flips in the same transaction and emits
`payroll.period_paid`. Reopening a period (if legal) flips it back to
`locked`. There is no manual "close" action — payslip state is the
source of truth.

## Payslip

A computed pay document for one (employee, pay_period).

| field                   | type    |
|-------------------------|---------|
| id                      | ULID PK |
| employee_id             | ULID FK |
| pay_period_id           | ULID FK |
| currency                | text    |
| gross_total_cents       | int     |
| components_json         | jsonb   |
| expense_reimbursements_cents | int |
| net_total_cents         | int     |
| pdf_file_id             | ULID FK |
| status                  | enum    | `draft | issued | paid | voided` |
| issued_at / paid_at     | tstz?   |
| email_delivery_id       | ULID FK?|
| payout_snapshot_json    | jsonb?  | immutable snapshot of destinations used; null on `draft`, populated at `draft → issued` transition, never modified thereafter. See "Snapshot on the payslip" below. |

### PDF

Rendered with WeasyPrint from a Jinja template. Line items include:

- Base pay (hours × rate or monthly salary),
- Overtime breakdown (by threshold),
- Holiday bonus,
- Per-task or piecework credits (itemized),
- Expense reimbursements (line per approved claim, linking the
  claim id, grouped by payout destination — see "Payout
  destinations" below).
- Deductions (rare in a household context; we leave a line for
  manager-entered adjustments with a mandatory reason).

### Distribution

Email to the employee with the PDF attached, or a download link
(signed URL) if the PDF is above a configurable size.

## Payout destinations

An employee can receive **pay** and **expense reimbursements** at
different destinations. A common case: the household opens a small
pre-funded account in the employee's name for operational expenses
so they don't have to front cash; reimbursements land there while
their main paycheque lands in their personal account.

**Payout execution is out of scope for v1** — miployees does not move
money. Destinations are metadata rendered on the payslip PDF and
returned in API responses so the operator knows where to push funds
from their bank or treasury tool. Even so, routing is
**security-critical**: a tampered destination silently redirects
someone's pay. The rules below are written with that threat in mind.

### `payout_destination`

| field          | type     | notes                                                         |
|----------------|----------|---------------------------------------------------------------|
| id             | ULID PK  |                                                               |
| household_id   | ULID FK  | scoping                                                       |
| employee_id    | ULID FK  | **row belongs to exactly one employee**; every read/write validates the caller has rights to that employee |
| label          | text     | "Personal BNP", "Expense float — Revolut" — display only      |
| kind           | enum     | `bank_account | card_reload | wallet | cash | other`          |
| currency       | text     | ISO 4217; required for all non-`cash` kinds                   |
| display_stub   | text     | public-safe short form: IBAN last-4 + country (`•• FR-12`), card last-4, wallet handle. Never the full number. NULL for `cash`. |
| secret_ref_id  | ULID FK? | pointer to the `secret_envelope` row holding the full account number. Required for `bank_account` and `card_reload`; NULL for `cash`. The full number is **only** ever decrypted to render the payslip PDF, never returned over the API. |
| country        | text?    | ISO-3166; required for `bank_account`                         |
| verified_at    | tstz?    | set when a manager hand-verifies the full number against a paper/photo artifact; `null` means unverified |
| verified_by    | ULID FK? | manager id                                                    |
| notes_md       | text?    | manager-visible, not rendered on PDF                          |
| created_at / updated_at | tstz |                                                         |
| archived_at    | tstz?    | non-null → cannot be selected as a new default; see below     |

**Validation per `kind`** (server-side, at write time):

- `bank_account`: `country` required; `display_stub` must match the
  country's IBAN format rules; full number is IBAN-checksummed before
  being stored in `secret_envelope`.
- `card_reload`: `display_stub` must be 4 digits; full PAN is Luhn-
  checked before being stored; PAN is **write-only** (never returned).
- `wallet`: `display_stub` is the handle or masked id.
- `cash`: `display_stub` and `secret_ref_id` must be NULL.
- `other`: `display_stub` free-form; `secret_ref_id` optional.

### Where the full number is allowed

- It is supplied only via `POST/PATCH /payout_destinations` body
  field `account_number_plaintext`, which the server encrypts into a
  new `secret_envelope` row in the same transaction and then discards
  from memory.
- The plaintext is **never** echoed back in the response, listed in
  `GET`, returned in webhook payloads, or written to any log. API
  clients never see it again.
- The payslip PDF is the only place the full number is rendered, and
  only when the PDF is generated server-side by the WeasyPrint worker.
  The PDF is stored as a regular `file` blob (§15); that blob carries
  the sensitivity: it is served over an authenticated signed URL with
  short TTL, inherits CSP, and is subject to the same retention as
  other `payslip.pdf_file_id` references.

### Who can mutate destinations

All mutations (`POST`, `PATCH`, archive) write an audit_log row and
fire the `payout_destination.{created,updated,archived,verified}`
webhook. In addition:

- An **employee** can create/edit their own destinations only if the
  capability `payroll.self_manage_destinations` is on (default
  **off**). When off, only managers can write.
- **Agent tokens** cannot mutate destinations without manager
  approval. `payout_destination.create`, `.update`,
  `.set_default_pay`, `.set_default_reimbursement`, and
  `expense_claim.set_destination_override` are added to §11's
  approvable-action list unconditionally — no household setting
  disables the gate.
- Setting or changing an `employee.pay_destination_id` or
  `employee.reimbursement_destination_id` to a row that does not yet
  have `verified_at` raises a non-fatal warning in the manager UI
  and daily digest until verification is recorded. The PDF still
  renders unverified destinations; the warning is about operator
  hygiene, not a system block.

### Default pointers on `employee`

- `pay_destination_id` — where payslips land.
- `reimbursement_destination_id` — where approved expense
  reimbursements land. If null, falls back to `pay_destination_id`.

Both must reference a non-archived destination whose
`employee_id = employee.id` and whose `household_id` matches — the
FK is enforced with a `CHECK` trigger in SQLite and a constraint
function in Postgres. Attempting to set a pointer to another
employee's destination is a 422.

Archiving a destination that is currently referenced as a default
nulls the relevant pointer(s) in the same transaction and emits an
`employee.default_destination_cleared` audit event + webhook. The
next payslip for that employee renders "Payout: arranged manually"
unless a new default is set first.

Either pointer may be null (cash-in-hand, or not yet configured);
the payslip PDF then renders "Payout: arranged manually" on the
corresponding line — explicit, not silently defaulted to zero.

### Per-claim override

An `expense_claim` carries an optional
`reimbursement_destination_id`. When a manager approves the claim,
the server validates that the referenced destination:

- has `employee_id = claim.employee_id`,
- is not archived,
- has `currency = claim.currency` (or the manager has acknowledged
  an explicit FX conversion note on the claim, similar to the rate
  snapshot in the approval flow above),
- is one of: the employee's defaults, or a destination the
  **approving manager** selected in the approval dialog.

An agent cannot approve a claim with a new `reimbursement_destination_id`
— that field on the approval payload forces the approvable-action
gate even if the agent also holds `expenses:approve`.

### Snapshot on the payslip

Destinations can change after a period is locked but before a
payslip is issued, or between issue and payment. To keep the pay
record honest, the payslip captures an **immutable snapshot** of the
destinations in use:

```
payslip.payout_snapshot_json = {
  "pay": {
    "destination_id": "pd_…",
    "label": "Personal BNP",
    "kind": "bank_account",
    "display_stub": "•• FR-12",
    "currency": "EUR",
    "verified": true
  },
  "reimbursements": [
    { "claim_id": "exp_…",
      "destination_id": "pd_…",
      "label": "Expense float — Revolut",
      "display_stub": "•• 4499",
      "currency": "EUR",
      "amount_cents": 3412 }
  ]
}
```

The snapshot is written when the payslip transitions from `draft` to
`issued` (see `payslip_status` §02). Changes to the underlying
`payout_destination` rows after that point do **not** modify the
snapshot. If the destinations referenced in the snapshot are later
archived, the snapshot remains as-is (it is historical evidence).

The PDF is rendered from the snapshot, never from the live pointers.
Reimbursements on the payslip group by snapshot `destination_id` so
the employee and operator both see what is going where.

### Currency mismatch

Issuing a payslip whose computed gross is in currency `X` with a
`pay_destination` whose currency is `Y` is blocked at the
`draft → issued` transition with a 422 `currency_mismatch` error.
Managers resolve by choosing a same-currency destination or by
explicitly marking the payslip "pay by cash" (clears the pointer
for this payslip only via the snapshot).

Same rule for reimbursements: a claim in currency `X` cannot be
attached to a destination in currency `Y` without an explicit
conversion acknowledgement recorded on the claim.

### Audit, approval, and webhook events

- `payout_destination.*` events: `created`, `updated`, `archived`,
  `verified`.
- `employee.default_destination_set`, `.default_destination_cleared`.
- `payroll.payslip_destination_snapshotted` (fires at issue time).
- All of the above are added to §10's webhook catalog.

## Expense claims

### Submission flow (employee)

The central user requirement: *submitting should be super easy, with
the LLM auto-populating from a receipt photo.*

1. Employee opens "New expense" on the PWA (or web).
2. Tap **"Add receipt"** → camera opens, takes photo (or picks from
   library). Multiple pages allowed.
3. Upload begins in the background; simultaneously the server calls
   the `expenses.autofill` LLM capability (§11) with the image(s).
4. Within ~3 seconds, the form is pre-populated:
   - `vendor` (e.g. "Monoprix")
   - `purchased_at` (date + approximate time if legible)
   - `total_amount` in the currency from the receipt
   - `currency` (from symbols / locale heuristics)
   - suggested `category` (from vendor type)
   - a set of `expense_line` rows (one per line item) with
     descriptions, quantities, unit prices
   - a suggested `note_md` summary
   - a `confidence` per field
5. Employee reviews; fields with low confidence are highlighted.
6. Submit. State becomes `submitted`, manager gets a notification
   (email + webhook).

Offline capture: the photo is queued locally; OCR runs on reconnect.

### Model

```
expense_claim
├── id
├── employee_id
├── submitted_at
├── vendor
├── purchased_at               # date (+ time if known)
├── currency
├── exchange_rate_to_default   # snapshot at submission; editable by manager
├── total_amount_cents         # in claim currency
├── category                   # supplies|fuel|food|transport|maintenance|other
├── property_id                # optional
├── note_md
├── llm_autofill_json          # full JSON returned by autofill (shape below)
├── autofill_confidence_overall # 0..1, derived min() of per-field scores
├── state                      # draft|submitted|approved|rejected|reimbursed
├── decided_by_manager_id
├── decided_at
├── decision_note_md
├── reimbursement_destination_id # ULID FK? override; null → use employee default
└── deleted_at
```

```
expense_line
├── id
├── claim_id
├── description
├── quantity
├── unit_price_cents
├── line_total_cents           # derived
├── source                     # ocr | manual (§02)
└── edited_by_user             # bool; set when a user mutates an ocr row
```

```
expense_attachment
├── id
├── claim_id
├── file_id                    # photo / pdf
├── kind                       # receipt | invoice | other
├── pages                      # int (for multi-page PDFs)
```

### Approval (manager)

- Review claim, edit any field, approve or reject with reason.
- Approving snaps the exchange rate (ECB daily fix fetched at
  approval time; cached in memory per currency/day by the worker,
  and stored on the claim in `exchange_rate_to_default` for
  reproducibility). If the ECB fetch fails, the UI blocks approval
  with a "no exchange rate available, try again or enter manually"
  error and the manager may type the rate by hand.
- The claim attaches to the pay period whose `[starts_on, ends_on]`
  contains `purchased_at`. If that period is already `locked` for the
  employee, it attaches to the next open period; a note is added to
  the claim so the employee can see why reimbursement is delayed.
- Webhook `expense.approved` / `expense.rejected` fires.

### Reimbursement

A claim becomes `reimbursed` when the containing payslip moves to
`paid`. No separate payment integration.

### LLM accuracy & guardrails

- The autofill call is bounded (max 2 images, 5 MB total); oversized
  uploads split into multiple calls.
- Confidence is **per-field**, not aggregate. `llm_autofill_json`
  shape:

  ```json
  {
    "vendor":        { "value": "Monoprix",    "confidence": 0.94 },
    "purchased_at":  { "value": "2026-04-15",  "confidence": 0.88 },
    "currency":      { "value": "EUR",         "confidence": 0.99 },
    "total_amount_cents": { "value": 3412,     "confidence": 0.72 },
    "category":      { "value": "supplies",    "confidence": 0.55 },
    "lines": [
      { "description": {"value": "Detergent 2L", "confidence": 0.9},
        "quantity":    {"value": 1,              "confidence": 0.95},
        "unit_price_cents": {"value": 899,       "confidence": 0.8} }
    ],
    "note_md":       { "value": "2-item grocery receipt", "confidence": 0.7 }
  }
  ```

  `autofill_confidence_overall` on `expense_claim` is derived as the
  minimum confidence across all populated top-level fields.

- Per-field UI thresholds:
  - ≥0.9 autofilled, quiet.
  - 0.6–0.9 autofilled, slight yellow border, focus on click.
  - <0.6 left blank with "review" placeholder, never pre-filled.
- All extractions recorded in `llm_call` (§11). Cost is attributed to
  `expenses.autofill` capability.
- If the household disabled `expenses.autofill_llm` for the employee
  or the capability globally, the photo is attached but no extraction
  runs.
- When a user edits an OCR line, `expense_line.source` stays `ocr`
  and `edited_by_user` flips to `true`. Fully user-created lines have
  `source = manual` from the start.

## Reports and exports

- **Timesheets** — CSV per pay period: employee, date, property,
  hours, overtime, holiday, notes.
- **Payroll register** — CSV per pay period: employee, gross, net,
  expenses, currency.
- **Expense ledger** — CSV by date range: claim id, employee, vendor,
  category, amount (claim + base currency), state.
- **Hours by property** — rollup useful for owners: hours consumed at
  each property for budgeting.

Exports: `GET /api/v1/exports/...csv` (streamed) or via CLI
`miployees export ...`.

## Out of scope (v1)

- Tax withholding, social contributions, statutory filings.
- Tip pooling, shift differentials beyond the overtime/holiday rules.
- Direct bank transfers or payment execution.
- Multi-currency payroll (claims can be multi-currency; payslips are
  per-currency, one per employee).
