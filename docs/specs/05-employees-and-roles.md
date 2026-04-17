# 05 — Users, work roles, capabilities

> Historical note: in v0 this document was titled "Employees and
> roles". v1 merges every human login into a single `users` table
> and expresses permissions through `role_grants` (§02). What used
> to be the `employee` entity no longer exists; the people who do
> work are `users` with one or more `user_work_role` rows and a
> `work_engagement` per workspace. This document covers the
> **work** side of that model (which jobs a user performs, which
> capabilities they have); §02 and §03 cover identity, grants, and
> auth.

## User (as worker)

Every human is a `users` row (§02). A user becomes a **worker** in
a given workspace when they hold a `role_grants` row with
`grant_role = 'worker'` on that workspace or one of its properties
**and** at least one `user_work_role` row binding them to a
`work_role` in that workspace.

- A user without any `user_work_role` cannot be granted
  `grant_role = 'worker'` on a workspace — the write fails with
  422 `error = "worker_requires_work_role"`. Property-scoped
  worker grants may exist without a workspace-level work-role
  binding, but only if the grant has an explicit
  `work_role_id` override inline (see below).
- A user **can** hold zero work roles and still exist in the
  system as an owner, manager, or client.
- A user may hold the same `work_role` in more than one
  workspace — each binding is an independent `user_work_role`
  row. Rates, capabilities, and schedules are per (user, workspace).

### Fields that formerly lived on `employee`

Several columns that used to sit on the v0 `employee` row now live
on distinct entities:

- Identity (display name, email, avatar, timezone, language,
  locale, phone, emergency contact, notes) — on `users` (§02).
- Engagement data (engagement_kind, supplier_org_id,
  pay_destination_id, reimbursement_destination_id, started_on,
  archived_on) — on `work_engagement` (§02, §22), scoped per
  (user, workspace).
- Permission / authority — on `role_grants` (§02).

A user who performs work under more than one workspace has one
`users` row, one or more `work_engagement` rows (one per
workspace), zero or more `user_work_role` rows per workspace, and
whichever `role_grants` rows the workspace's owner/manager sees fit.

## Work role

A work role is a named capability-bundle the workspace uses: maid,
cook, driver, gardener, pool_tech, handyman, nanny, personal
assistant, concierge, property_manager, etc. Work roles are
**workspace-defined** — the system ships a starter set but they are
regular rows, renameable and addable.

(In v0 this entity was called `role`. It was renamed to `work_role`
in v1 because `role` is now ambiguous with `grant_role` from §02.)

### Fields

| field             | type     | notes                                   |
|-------------------|----------|-----------------------------------------|
| id                | ULID PK  |                                         |
| workspace_id      | ULID FK  |                                         |
| key               | text     | stable slug: `maid`, `cook`. Unique per `(workspace_id, key)`. Editable but changing it audit-logs as `work_role.rekey` and breaks external references that hard-code the slug. |
| name              | text     | display: "Maid", "Cuisinier/ère"        |
| description_md    | text     |                                         |
| default_capabilities | jsonb | capabilities enabled by default (see below) |
| icon_glyph        | text     | tailwind heroicon name, for the UI      |
| deleted_at        | tstz?    |                                         |

### Starter roles

Seeded on first boot; each is just a row, editable/removable later:

`maid`, `cook`, `driver`, `gardener`, `handyman`, `nanny`,
`pool_tech`, `concierge`, `personal_assistant`, `property_manager`.

## User work role

Links a user to a work role **within a workspace**, with per-assignment
overrides, so the same person can be both cook (full pay rate) and
driver (lower rate) in the same workspace, or `maid` in Workspace A
without being `maid` in Workspace B.

(In v0 this entity was called `employee_role`.)

### Fields

| field               | type     | notes                                   |
|---------------------|----------|-----------------------------------------|
| id                  | ULID PK  |                                         |
| user_id             | ULID FK  | references `users.id`                   |
| workspace_id        | ULID FK  | the workspace this job applies to       |
| work_role_id        | ULID FK  | references `work_role.id`               |
| started_on          | date     |                                         |
| ended_on            | date?    |                                         |
| pay_rule_id         | ULID FK? | override the default pay rule on the user's `work_engagement` in this workspace |
| capability_override | jsonb    | sparse, shallow-merged on top of work role defaults |

Unique: `(user_id, workspace_id, work_role_id, started_on)`.

**Invariant.** Every active `user_work_role` row must correspond to
a `work_role` whose `workspace_id` matches the row's `workspace_id`
— a user cannot borrow a work role definition across workspaces;
each workspace defines its own catalog.

**Invariant.** If the user holds a `role_grants` row with
`grant_role = 'worker'` on this workspace, they must have ≥ 1
`user_work_role` row here. The inverse is not required — a user
may hold only a property-scoped worker grant plus the
corresponding `user_work_role`, with no workspace-scope grant.

## Property work role assignment

A `user_work_role` may be constrained to one or more properties. A
maid might work both Villa Sud and Apt 3B at different rates. If no
property assignments exist, the `user_work_role` is eligible for
**all** properties of the workspace — useful for generalists.

(In v0 this entity was called `property_role_assignment`.)

| field                    | type     | notes                                   |
|--------------------------|----------|-----------------------------------------|
| id                       | ULID PK  |                                         |
| user_work_role_id        | ULID FK  | replaces v0's `employee_role_id`        |
| property_id              | ULID FK  |                                         |
| schedule_ruleset_id      | ULID FK? | which default schedule applies at this property |
| property_pay_rule_id     | ULID FK? | rarer: per-property rate override       |
| capability_override      | jsonb    | sparse, shallow-merged on top of the user_work_role override |

## Work engagement (pointer)

`work_engagement` carries the per-(user, workspace) pay pipeline
data (engagement_kind, supplier_org_id, pay_destination_id,
reimbursement_destination_id, started_on, archived_on). Its
canonical definition is in §02; the pipeline behaviour it drives
(payslips, vendor invoices) is in §09 and §22.

A user who holds one or more `user_work_role` rows in a workspace
**must** have a `work_engagement` row in that workspace (active or
archived). The write-side invariant is: creating the first
`user_work_role` for a (user, workspace) creates the
`work_engagement` row if missing. Archiving every `user_work_role`
for that (user, workspace) does not auto-archive the engagement —
the operator does that explicitly.

## Archive / reinstate

v1 archive semantics distinguish three scopes:

1. **Archive a `user_work_role`** — the user is no longer eligible
   for that job in that workspace. Existing assignments for that
   row are unassigned on the next generation tick; historical
   completions stay. No auth change.
2. **Archive a `work_engagement`** — the user is off-boarded from
   one workspace. Sets `archived_on = today` on the engagement,
   archives every `user_work_role` they hold in that workspace,
   and removes them from forward-looking task assignments for that
   workspace. Fires `work_engagement.archived`. Historical pay,
   shifts, and payslips are preserved. Other workspaces where the
   same user has engagements are untouched.
3. **Archive a `users` row** — the person is off-boarded
   deployment-wide. Revokes passkeys and sessions immediately
   (§03), archives every `work_engagement` they hold, and records
   `users.archived_at`. `role_grants` rows persist for audit but
   resolve as inactive. Fires `user.archived`. Archiving a user
   while they hold the **sole** `owner` grant on any scope is
   blocked (see §02 `users.archived_at` invariant).

Reinstatement follows the same hierarchy: reinstate a
user_work_role, a work_engagement, or the whole user. Reinstating
a whole user issues them a fresh magic link (since their prior
passkeys are gone) and fires `user.reinstated`.

The words "end", "terminate", "off-board", "rehire", "soft-off" are
**not** used in the schema, API, or UI. When writing new code or
docs, use archive/reinstate.

## Capabilities (work-scoped)

Capabilities are per-user feature toggles the workspace flips based
on the user's work role needs. They shape UI and scheduling for the
worker surface. Capabilities are a **sparse JSON blob**; unset
means "inherit from the next layer", which itself may be unset,
meaning "feature off".

(Grant-scoped capabilities — like `users.invite` or
`quotes.accept` — live on `role_grants.capability_override` and
are catalogued in §02.)

### Canonical catalog

| key                           | default off/on | meaning                                        |
|-------------------------------|----------------|-------------------------------------------------|
| `time.clock_in`               | off            | Can clock in/out                                |
| `time.clock_mode`             | `manual`       | `manual | auto | disabled` — see §09. `manual`: worker taps clock-in/out. `auto`: first checklist tick or task action of the day opens a shift; idle timer closes it. `disabled`: hours not tracked. |
| `time.auto_clock_idle_minutes`| `30`           | Integer; inactivity window (minutes) that closes an `auto` shift. Ignored unless `time.clock_mode = auto`. |
| `time.geofence_required`      | off            | Must be within property radius to clock in     |
| `time.manager_edit_only`      | off            | Shifts editable only by a user with `manager`/`owner` grant |
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
| `payroll.self_manage_destinations` | off       | Can self-create/edit `payout_destination` rows owned by their own `work_engagement` (§09). Off by default so only users with manager/owner grants can route their pay. |

### Resolution order

**Capabilities vs settings cascade.** Capabilities are per-user
feature toggles resolved through the work-role / property-assignment
hierarchy (below). The **settings cascade** (§02 "Settings cascade")
is a separate, unified framework for entity-level configuration
(values like `evidence.policy`, `time.clock_mode`,
`scheduling.horizon_days`) that resolves workspace → property →
unit → work_engagement → task. Where a key appears in both systems
(e.g. `time.clock_mode`), the settings cascade takes precedence;
its resolution is: task → work_engagement → unit → property →
(capability chain) → workspace → catalog default.

For a given (user, task) pair, resolve a capability as:

1. Per-`property_work_role_assignment.capability_override`
2. Per-`user_work_role.capability_override`
3. Per-`work_role.default_capabilities`
4. Compile-time default in the catalog above.

**First "present" wins, where present includes explicit `false`.**
Sparse JSON semantics: a key being absent means "inherit"; a key
being `false` means "explicitly off, stop inheritance here". This
lets an owner/manager disable a capability at a specific property
for a specific user even when the work-role default is on. Setting
a key back to `null` (or deleting it) re-enables inheritance.

### UI

Each capability is shown as a three-state control: **On / Off /
Inherit**, with a live preview of the resolved value underneath. The
same blob drives both the owner/manager UI and the API.

### Evidence-policy stack

The evidence-policy stack is an instance of the **settings cascade**
(§02 "Settings cascade"), canonical key `evidence.policy`, scope
`W/P/U/WE/T`. The description below documents the domain-specific
semantics; the cascade mechanics (layer columns, override shape,
resolution order) are canonical in §02.

A separate resolution stack, parallel to the capability stack above,
computes whether a task needs photo evidence. Five layers, in order
from broadest to most specific:

1. **Workspace default** — always concrete (`require | optional |
   forbid`), seeded at first boot; never `inherit`.
2. **Property** — `inherit | require | optional | forbid`.
3. **Unit** — `inherit | require | optional | forbid`. Single-unit
   properties see no behavioural change; the unit inherits from the
   property.
4. **Work engagement** (per (user, workspace)) —
   `inherit | require | optional | forbid`.
5. **Task** (template-derived, with per-task override) —
   `inherit | require | optional | forbid`.

**Most specific wins.** Resolution walks from the task inward:
task → work_engagement → unit → property → workspace, stopping at
the first **concrete** (non-`inherit`) value; layers set to
`inherit` (the non-root default) pass through. The common case is
"follow the workspace default unless a property, a unit, a specific
engagement, or a specific task deliberately narrows or widens the
rule." `forbid` at any layer is absolute — even a later `require`
on a more specific layer cannot override it (see §06 "Evidence
policy inheritance" for the override-vs-forbid interaction and §09
for how `require | optional | forbid` interact with completion).

## Permissions and the grant catalog

Authority in the application comes from `role_grants` rows (§02).
This section documents the defaults that each `grant_role` ships
with. Every default is overrideable per-grant via
`role_grants.capability_override`.

### Grant roles at a glance

| role     | typical user                                  | primary surface                                              |
|----------|-----------------------------------------------|--------------------------------------------------------------|
| `owner`  | the head of household or agency operator      | everything in the scope; cannot be demoted by peers          |
| `manager`| a peer co-manager or agency staff             | like owner, minus the right to demote/transfer the owner     |
| `worker` | a maid, driver, cook, contractor              | assigned tasks, own shifts, own expenses, own profile        |
| `client` | a villa owner who pays an agency              | shifts/invoices billed to them; accept/reject quotes         |
| `guest`  | a short-term stay occupant (post-v1)          | reserved; v1 uses tokenized `guest_link` (§04) not grants    |

### Worker permissions (web UI / PWA)

Users whose highest grant in a scope is `worker` see only:

- Their own profile (read + limited update: display name, avatar,
  timezone, emergency contact, language).
- Tasks assigned to them, plus unassigned tasks at properties in
  their scope that match their `user_work_role`s.
- Instructions scoped to those properties/areas/global (read-only).
- Their own shifts, payslips (read-only), and expense claims.
- Staff-visible subset of property notes (§04) — not access codes or
  wifi passwords unless the scope's owner or manager explicitly shares.
- Comments on tasks they can see, plus authoring comments on those.
- The staff chat assistant if `chat.assistant` is on.

Workers never see:

- Other users' wages, hours, or pay rules.
- Owner/manager invite links.
- The API token list.
- The audit log.
- Financial aggregates.

### Client permissions (web UI)

Users whose grant in a scope is `client` see only:

- Properties they are billed for (property-scope grant) or
  properties tagged with their `binding_org_id` (workspace-scope
  grant). Read-only view: name, address, a sanitized work log.
- Shifts at those properties: date, role_key, duration, rate,
  amount (via `shift_billing` rollups).
- Work orders and quotes billed to them: full detail so they can
  accept (gated — see §11) or reject.
- Vendor invoices billed to them: full detail, including
  `payout_destination` redacted beyond last 4 IBAN digits (§15).

Clients never see:

- Other clients of the workspace.
- Workers' pay rules (they see agency billing rates, not worker
  compensation).
- Staff-only instructions, staff chat, audit log, API tokens,
  workspace settings.
- Any data tagged to a `binding_org_id` other than their own.

### Owner and manager permissions

Owners and managers see everything in the scope with the exception
of strict ownership invariants:

- Managers cannot revoke, demote, or archive the owner.
- Only the owner may transfer the owner role (gated by §11).
- Destructive operations across a workspace (`workspace.archive`,
  `admin:purge`) are owner-only.

### Grant-scoped capability catalog

The canonical catalog lives in §02 under `role_grants` → "Catalog
of grant-scoped capabilities". Cross-reference rather than
duplicate.

## Permissions (API tokens)

Covered in §03. A **scoped standalone token** carries an explicit
scope list and bypasses `role_grants` entirely. A **delegated
token** inherits the delegating user's `role_grants` and work-role
bindings at request time; when the user's grants change, the
delegated token's authority changes immediately.

## Example (real world)

> Maria is a maid at Villa Sud (twice a week) and a nanny at the
> main residence (once a week). The manager expects photo evidence
> for cleaning but not for nannying; at Villa Sud Maria clocks in,
> at the main residence she does not.

Both properties live in the same workspace `HomeOps`. This is
modeled as:

- 1 `users` row (Maria)
- 1 `role_grants` row:
  `(user=Maria, scope='workspace', scope_id=HomeOps, grant_role='worker')`
- 1 `work_engagement` row:
  `(user=Maria, workspace=HomeOps, engagement_kind='payroll', ...)`
- 2 `user_work_role` rows:
  `(user=Maria, workspace=HomeOps, work_role=maid)`,
  `(user=Maria, workspace=HomeOps, work_role=nanny)`.
- 2 `property_work_role_assignment` rows:
    - `(user_work_role=maid, property=Villa_Sud)`, `capability_override:
      {time.clock_in: true, tasks.photo_evidence_required: true}`
    - `(user_work_role=nanny, property=Main_Residence)`,
      `capability_override: {time.clock_in: false}`.

Pay rules are separate and attach to `pay_rule.work_engagement_id`
pointing at Maria's `HomeOps` engagement (§09).

## Example (multi-workspace: Vincent)

> Vincent owns a villa "Villa du Lac". He runs his own
> operations there (one live-in driver, Rachid, on payroll)
> and also pays an agency "CleanCo" to send a maid, Joselyn,
> twice a week. Vincent also owns a seaside apartment that he
> manages entirely on his own, with no agency involvement.

This needs two workspaces:

- `VincentOps` — Vincent's own workspace.
- `AgencyOps` — CleanCo's workspace (serves many clients).

### Users

- `users(Vincent)` — one row.
- `users(Rachid)` — one row.
- `users(Joselyn)` — one row.
- `users(Julie)` — CleanCo's manager.

### Organizations

- `organization(DupontFamily)` — Vincent's billing legal entity
  (tax id, pay destination, etc.). `is_client = true`,
  `is_supplier = false`. Lives in `AgencyOps`'s scope as a client
  row.
- `organization(CleanCo)` — the agency itself, as a counterparty
  only when viewed from Vincent's side. Not needed unless
  `VincentOps` wants to bill-back its own costs.

### Role grants

- `role_grants(Vincent,  scope='workspace',    scope_id=VincentOps, role='owner')`
- `role_grants(Rachid,   scope='workspace',    scope_id=VincentOps, role='worker')`
- `role_grants(Vincent,  scope='organization', scope_id=DupontFamily, role='owner')`
- `role_grants(Vincent,  scope='workspace',    scope_id=AgencyOps,  role='client', binding_org_id=DupontFamily)`
- `role_grants(Julie,    scope='workspace',    scope_id=AgencyOps,  role='manager')`
- `role_grants(Joselyn,  scope='workspace',    scope_id=AgencyOps,  role='worker')`

### Properties

- `property(Villa_du_Lac)`:
  `owner_user_id = Vincent`, `client_org_id = DupontFamily` (§22).
- `property(Seaside_Apt)`:
  `owner_user_id = Vincent`, `client_org_id = NULL` (self-managed).

### `property_workspace`

- `(Villa_du_Lac, VincentOps,  membership_role='owner_workspace')`
- `(Villa_du_Lac, AgencyOps,   membership_role='managed_workspace')`
- `(Seaside_Apt,  VincentOps,  membership_role='owner_workspace')`

### Work engagements

- `work_engagement(Rachid,   workspace=VincentOps, kind='payroll', ...)`
- `work_engagement(Joselyn,  workspace=AgencyOps,  kind='payroll', ...)`
- `work_engagement(Julie,    workspace=AgencyOps,  kind='payroll', ...)`
  (optional — only if Julie draws pay from CleanCo through this system.)

### Work roles

- `user_work_role(Rachid,  workspace=VincentOps, work_role=driver)`
- `user_work_role(Joselyn, workspace=AgencyOps,  work_role=maid)`

Plus `property_work_role_assignment` rows narrowing Joselyn's maid
role to `Villa_du_Lac` only (she has other clients too), and
Rachid's driver role to `Villa_du_Lac` and `Seaside_Apt`.

### What each person sees

- **Vincent** logs in and has a workspace switcher:
    - `VincentOps` (owner) — full view of Rachid, both properties,
      inventory, assets, finances, and a "billed from CleanCo"
      panel fed from his client grant on `AgencyOps`.
    - `AgencyOps` (client, binding DupontFamily) — read-only view
      of Joselyn's shifts at Villa du Lac, vendor invoices
      CleanCo has raised against DupontFamily, accept/reject
      quotes.
- **Rachid** logs in and sees only `VincentOps`, his assigned
  tasks at Villa du Lac and Seaside Apt, his shifts, his profile.
- **Joselyn** logs in and sees only `AgencyOps`, her assigned
  tasks at the properties she has `property_work_role_assignment`
  rows for (including Villa du Lac), her shifts, her profile. She
  does not see Rachid or Vincent's direct operation; the shared
  property `Villa_du_Lac` appears in her list because it sits in
  `AgencyOps` via the junction, but its `owner_workspace` metadata
  is hidden from her worker view.
- **Julie** logs in, sees `AgencyOps` as a manager: every CleanCo
  worker, every CleanCo client property (including Villa du Lac
  as billed to DupontFamily), every vendor invoice.
