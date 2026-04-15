# 09 — Time, payroll, expenses

Three tightly-linked features for staff who expect to get paid
correctly and for managers who want to stop keeping shift notes in a
phone's notes app.

## Time tracking (shifts)

### Model

```
shift
├── id
├── employee_id
├── property_id              # optional; unassigned shifts for remote drivers, etc.
├── started_at               # utc
├── ended_at                 # utc, nullable while open
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

Any shift can be adjusted; adjustment sets `adjusted = true`,
`adjustment_reason = required`, preserves original values in
`audit_log.before_json`. Employees see "(edited by manager)" on
affected shifts.

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

`pay_period {id, starts_on, ends_on, frequency, status (open|locked|
paid)}`.

Periods are created per household based on the household's default
frequency (monthly by default; bi-weekly supported). Periods may
overlap across employees when their pay rules diverge.

### Period close

A manager closes a period ("Lock"):

1. Validate: no open shifts remain in the period.
2. Compute `pay_period_entry` rows: per employee, per day, regular
   hours / overtime / holiday / per-task counts / piecework totals.
3. Generate `payslip` rows.
4. Emit `payroll.period_locked` webhook.

Locked periods cannot be edited; manager can "reopen" with an explicit
audit event.

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

### PDF

Rendered with WeasyPrint from a Jinja template. Line items include:

- Base pay (hours × rate or monthly salary),
- Overtime breakdown (by threshold),
- Holiday bonus,
- Per-task or piecework credits (itemized),
- Expense reimbursements (line per approved claim, linking the
  claim id).
- Deductions (rare in a household context; we leave a line for
  manager-entered adjustments with a mandatory reason).

### Distribution

Email to the employee with the PDF attached, or a download link
(signed URL) if the PDF is above a configurable size.

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
├── llm_autofill_json          # full JSON returned by autofill
├── autofill_confidence        # 0..1 aggregate
├── state                      # draft|submitted|approved|rejected|reimbursed
├── decided_by_manager_id
├── decided_at
├── decision_note_md
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
└── source                     # ocr | manual
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
- Approving snaps the exchange rate and files the claim against the
  next open pay period for that employee; the rubber-stamped amount
  appears as a line on the forthcoming payslip.
- Webhook `expense.approved` / `expense.rejected` fires.

### Reimbursement

A claim becomes `reimbursed` when the containing payslip moves to
`paid`. No separate payment integration.

### LLM accuracy & guardrails

- The autofill call is bounded (max 2 images, 5 MB total); oversized
  uploads split into multiple calls.
- Confidence thresholds per field drive UI highlighting:
  - ≥0.9 autofilled, quiet.
  - 0.6–0.9 autofilled, slight yellow border, focus on click.
  - <0.6 left blank with "review" placeholder, never pre-filled.
- All extractions recorded in `llm_call` (§11). Cost is attributed to
  `expenses.autofill` capability.
- If the household disabled `expenses.autofill_llm` for the employee
  or the capability globally, the photo is attached but no extraction
  runs.

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
