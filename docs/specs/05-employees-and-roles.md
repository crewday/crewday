# 05 — Employees, roles, capabilities

## Employee

A person who performs tasks for the household.

### Fields

| field              | type      | notes                            |
|--------------------|-----------|----------------------------------|
| id                 | ULID PK   |                                  |
| household_id       | ULID FK   |                                  |
| display_name       | text      | shown to everyone                |
| full_legal_name    | text      | payroll only; manager-visible    |
| email              | text      | magic links and digest           |
| phone_e164         | text      | optional, manager-visible only   |
| avatar_file_id     | ULID FK?  | file in `storage`                |
| timezone           | text      | defaults to default property's tz |
| languages          | text[]    | BCP-47 tags; informational in v1 |
| started_on         | date      | employment start                 |
| ended_on           | date?     | set when off-boarded; soft-off   |
| notes_md           | text      | manager-visible                  |
| emergency_contact  | jsonb     | `{name, phone_e164, relation}`   |
| deleted_at         | tstz?     |                                  |

An employee without any role is invalid; creation requires at least
one `employee_role`.

### Off-boarding

"Ending" an employee sets `ended_on = today`, revokes all passkeys,
revokes active sessions, and removes them from forward-looking task
assignments. Historical assignments and payslips are preserved.
Manager can "rehire" by clearing `ended_on` and re-issuing a magic
link.

## Role

A role is a named capability-bundle the household uses: maid, cook,
driver, gardener, pool_tech, handyman, nanny, personal_assistant,
concierge, etc. Roles are **household-defined** — the system ships a
starter set but they are regular rows, renameable and addable.

### Fields

| field             | type     | notes                                   |
|-------------------|----------|-----------------------------------------|
| id                | ULID PK  |                                         |
| household_id      | ULID FK  |                                         |
| key               | text     | stable slug: `maid`, `cook`             |
| name              | text     | display: "Maid", "Cuisinier/ère"        |
| description_md    | text     |                                         |
| default_capabilities | jsonb | capabilities enabled by default (see below) |
| icon_glyph        | text     | tailwind heroicon name, for the UI      |
| deleted_at        | tstz?    |                                         |

### Starter roles

Seeded on first boot; each is just a row, editable/removable later:

`maid`, `cook`, `driver`, `gardener`, `handyman`, `nanny`,
`pool_tech`, `concierge`, `personal_assistant`, `property_manager`.

## Employee role assignment

Links an employee to a role **with per-assignment overrides**, so the
same person can be both cook (full pay rate) and driver (lower rate).

### Fields

| field               | type     | notes                                   |
|---------------------|----------|-----------------------------------------|
| id                  | ULID PK  |                                         |
| employee_id         | ULID FK  |                                         |
| role_id             | ULID FK  |                                         |
| started_on          | date     |                                         |
| ended_on            | date?    |                                         |
| pay_rule_id         | ULID FK? | override the employee's default pay rule |
| capability_override | jsonb    | sparse, shallow-merged on top of role defaults |

## Property role assignment

An employee_role may be constrained to one or more properties. A maid
might work both Villa Sud and Apt 3B at different rates.

| field                    | type     | notes                                   |
|--------------------------|----------|-----------------------------------------|
| id                       | ULID PK  |                                         |
| employee_role_id         | ULID FK  |                                         |
| property_id              | ULID FK  |                                         |
| schedule_ruleset_id      | ULID FK? | which default schedule applies at this property |
| property_pay_rule_id     | ULID FK? | rarer: per-property rate override       |

If no property assignments exist, the employee_role is eligible for
**all** properties of the household — useful for generalists.

## Capabilities

Capabilities are per-employee feature toggles the manager flips based
on the role's needs. They shape UI and scheduling. Capabilities are a
**sparse JSON blob**; unset means "inherit from role default", which
itself may be unset, meaning "feature off".

### Canonical catalog

| key                           | default off/on | meaning                                        |
|-------------------------------|----------------|-------------------------------------------------|
| `time.clock_in`               | off            | Can clock in/out                                |
| `time.geofence_required`      | off            | Must be within property radius to clock in     |
| `time.manager_edit_only`      | off            | Shifts editable only by manager                 |
| `tasks.photo_evidence`        | off            | Can attach photos to completions                |
| `tasks.photo_evidence_required` | off          | Must attach photo to complete                   |
| `tasks.checklist_required`    | off            | All checklist items must be ticked to complete  |
| `tasks.allow_skip_with_reason`| on             | Can skip a task with a reason                   |
| `tasks.allow_complete_backdated` | off         | Can complete with `completed_at < now`          |
| `messaging.comments`          | on             | Can comment on tasks                            |
| `messaging.report_issue`      | on             | Can open issue reports                          |
| `inventory.adjust`            | off            | Can adjust stock levels                         |
| `inventory.consume_on_task`   | on             | Completions can deduct stock                    |
| `expenses.submit`             | off            | Can submit expense claims                       |
| `expenses.photo_upload`       | on             | Can attach receipts                             |
| `expenses.autofill_llm`       | on             | Receipts may be OCR'd by the configured model   |
| `chat.assistant`              | off            | Gets the staff chat assistant (§11)             |
| `voice.assistant`             | off            | Chat assistant accepts voice input              |
| `pwa.offline_queue`           | on             | Offline completion queue enabled on their PWA   |
| `notifications.email_digest`  | on             | Receives their own daily digest email           |

### Resolution order

For a given (employee, task) pair, resolve a capability as:

1. Per-`property_role_assignment.capability_override`
2. Per-`employee_role.capability_override`
3. Per-`role.default_capabilities`
4. Compile-time default in the catalog above.

First non-null wins.

### UI

Each capability is shown as a three-state control: **On / Off /
Inherit**, with a live preview of the resolved value underneath. The
same blob drives both manager UI and API.

## Permissions (web UI)

Employees see only:

- Their own profile (read + limited update: display name, avatar,
  timezone, emergency contact, language).
- Tasks assigned to them, plus unassigned tasks at their properties
  that match their roles.
- Instructions scoped to their properties/areas/global (read-only).
- Their own shifts, payslips (read-only), and expense claims.
- Staff-visible subset of property notes (§04) — not access codes or
  wifi passwords unless the manager explicitly shares.
- Comments on tasks they can see, plus authoring comments on those.
- The staff chat assistant if the capability is on.

Employees never see:

- Other employees' wages, hours, or pay rules.
- Managers' invite links.
- The API token list.
- The audit log.
- Financial aggregates.

## Permissions (API tokens)

Covered in §03. Orthogonal to employee permissions — an agent's
capabilities follow from its token scopes, not from any employee
record.

## Example (real world)

> Maria is a maid at Villa Sud (twice a week) and nanny at the main
> residence (once a week). The manager expects photo evidence for
> cleaning but not for nannying; at Villa Sud she clocks in, at the
> main residence she does not.

This is modeled as:

- 1 `employee` (Maria)
- 2 `employee_role` rows: (maid), (nanny)
- 2 `property_role_assignment` rows:
    - (maid → Villa Sud), capability_override:
      `{time.clock_in: true, tasks.photo_evidence_required: true}`
    - (nanny → Main Residence), capability_override:
      `{time.clock_in: false}` (explicit off)

And pay rules separate (§09).
