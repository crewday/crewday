# 20 — Glossary

Terms used across the spec. Definitive form; if code or doc disagrees,
fix the offender.

- **Actor.** The kind of principal responsible for an action, recorded
  on `audit_log` and shift/claim rows: `manager | employee | agent |
  system`.
- **Agent.** A non-human actor. Standalone agents are authenticated
  by scoped API tokens (`actor_kind = 'agent'`). Embedded agents use
  **delegated tokens** that act with the full authority of their
  delegating user (`actor_kind` = the user's kind). See §03, §11.
- **Agent (embedded).** The manager-side or employee-side chat
  agent described in §11. Default model `google/gemma-4-31b-it`;
  tool surface is the full CLI + REST surface of the delegating user
  (no filtered catalog). Voice input is capability-gated.
- **Auto-clock.** The `auto` value of `time.clock_mode` (§05, §09).
  First checklist tick or task action of the day opens a shift;
  `time.auto_clock_idle_minutes` of inactivity closes it. Per-villa
  override can force `manual` or `disabled`. See §09 "Disputed
  auto-close" for the re-open semantics.
- **Anomaly suppression.** A manager-recorded rule that silences a
  specific `(anomaly_kind, subject_id)` pair until a required
  `suppressed_until` timestamp (§11). Permanent suppression is not
  offered by design.
- **Approvable action.** A write that requires manager approval
  regardless of token scope (§11). Default TTL 7 days.
- **Archive / reinstate.** The canonical verbs for off-boarding and
  bringing back an employee (§05). Replaces "end", "terminate", "rehire".
- **Area.** A subdivision of a property (kitchen, pool, Room 3).
  Optionally scoped to a specific unit (`area.unit_id`); null means
  shared/property-level.
- **Asset.** A tracked physical item installed at a property — an
  appliance, piece of equipment, or vehicle. Carries condition, status,
  warranty, and QR token. See §21.
- **Asset action.** A maintenance operation defined on an asset
  (e.g. "clean filter every 30 days"). Can be linked to the task
  scheduling system; completion updates `last_performed_at`. See §21.
- **Asset document.** A file attached to an asset or a property —
  manual, warranty, invoice, certificate, etc. Carries optional
  `expires_on` and `amount_cents` for TCO tracking. See §21.
- **Asset type.** A catalog entry describing a category of equipment
  (e.g. "Air conditioner", "Pool pump"). System-seeded types ship
  with default maintenance actions; managers add workspace-custom
  types. See §21.
- **Assignment.** The linkage of an employee to a role+property
  (`property_role_assignment`) and, per task, the pointer
  `task.assigned_employee_id`. There is no separate `task_assignment`
  entity — task assignment is just a column.
- **Audit log.** Append-only ledger of all state-changing actions.
- **Availability override.** A date-specific override of an
  employee's weekly availability pattern. Adding work is self-service
  (auto-approved); reducing availability requires manager approval.
  Only approved overrides affect the assignment algorithm. See §06
  `employee_availability_overrides`.
- **Break-glass code.** Manager-only single-use recovery code that
  generates exactly one magic link on redemption (§03).
- **Capability.** A per-employee, per-property-role feature flag,
  resolved from a four-level sparse JSON stack (property_role_
  assignment → employee_role → role → catalog default). Explicit
  `false` blocks inheritance; absent keys inherit. Canonical catalog
  in §05.
- **Condition (asset).** The physical state of an asset: `new | good |
  fair | poor | needs_replacement`. Changes are audit-logged as
  `asset.condition_changed`. See §21.
- **Checklist item.** A row in `task_checklist_item` — one tickable
  line on a task, seeded from the template's
  `checklist_template_json`. Per-item tick state is authoritative.
- **Completion.** Terminal state for a task; has evidence and an
  employee. Under concurrent writes, last-write-wins with a
  `task.complete_superseded` audit entry for the displaced one (§06).
- **Correlation ID.** Per-request identifier (or caller-supplied
  `X-Correlation-Id`) that groups audit rows. Not a workflow-lifetime
  concept.
- **Digest run.** A single execution of the daily summary
  email/notification pipeline (§10). Records the digest template,
  recipients, send time, and delivery outcomes.
- **Employee leave.** Approved absence window (§06). Unapproved
  requests do not affect assignment.
- **Evidence.** Artifact attached to a completion — photo, note, or
  checklist snapshot.
- **Evidence policy.** Photo-evidence requirement resolved by
  walking a five-layer stack workspace → villa → unit → employee →
  task (§05 "Evidence-policy stack", §06 "Evidence policy
  inheritance"). Values are `inherit | require | optional | forbid`;
  the workspace root is always concrete. First concrete value,
  root-first, wins. `forbid` at any layer is absolute.
- **File.** Shared blob-reference row (§02 `file`). Pluggable backend;
  local disk in v1.
- **Guest link.** A tokenized URL sent to a stay guest that opens a
  welcome page showing property info, wifi, house rules, and a
  guest-visible checklist. See §04.
- **Handle.** Optional user-friendly slug (`maid-maria`) stored in a
  per-entity `handle` column where useful. Unique per parent scope.
- **Household.** **v0 term; replaced by Workspace in v1.** Retained
  here so historical references in migrations, ADRs, and older code
  remain resolvable. New code and new docs use Workspace.
- **iCal feed.** An external calendar subscription (Airbnb, VRBO,
  Booking, generic) polled periodically to import stays into a unit.
  Feed URLs are stored in `secret_envelope` (§15). See §04.
- **Instruction.** A standing SOP attached at global / property /
  area / link scope (§07). `instruction_link` is canonical;
  `task.linked_instruction_ids` is a denormalized cache.
- **Inventory movement.** An append-only ledger row recording a change
  to `inventory_item.on_hand`. Reason enum: `restock | consume |
  adjust | waste | transfer_in | transfer_out | audit_correction`.
  See §08.
- **Issue.** An employee-reported problem tracked with state
  (`open | in_progress | resolved | wont_fix`) and possibly converted
  to a task.
- **Magic link.** Single-use, signed URL used to enroll or recover a
  passkey. Consumes a break-glass code (if that's the source)
  regardless of whether the link is later clicked.
- **Manager.** Human with elevated scope. All managers are peers in v1.
- **Model assignment.** The capability → model mapping (§11).
- **Off-app reach-out.** Agent-initiated WhatsApp or SMS message to an
  employee for low-stakes checks. Requires the employee's
  `preferred_offapp_channel` to be set. See §10.
- **Passkey.** WebAuthn platform or roaming authenticator credential.
- **Pay period.** A date-bucket inside which shifts roll up into a
  payslip. `open → locked → paid`; `paid` is set automatically when
  every contained payslip reaches `paid`.
- **Payout destination.** A per-employee record naming where money
  lands: bank account, reloadable card, wallet, cash, or other.
  Employees may hold more than one; the employee row carries default
  pointers for pay and for reimbursements separately (§09). Full
  account numbers live in `secret_envelope`; only a `display_stub`
  (IBAN last-4 + country, card last-4, wallet handle) is returned
  over the API. Creating, editing, or changing a default is always
  approval-gated for bearer tokens (scoped and delegated) —
  miployees does not execute
  payments, but routing decisions are security-critical and treated
  accordingly.
- **Payout snapshot.** The immutable `payout_snapshot_json` captured
  on a payslip at the `draft → issued` transition. Records where pay
  and each reimbursement went (`display_stub` only — never full
  account numbers), independent of later destination edits or
  archives. The stored payslip PDF is rendered from the snapshot, so
  the PDF is always safe to keep long-term.
- **Payout manifest.** A streaming, not-stored JSON artifact from
  `POST /payslips/{id}/payout_manifest` that decrypts full account
  numbers at the moment the operator pushes funds. **Manager-
  session only** (no bearer tokens, even via approval — see
  "Interactive-session-only endpoint" below). Every fetch is audit-logged; no
  blob is persisted; the idempotency cache does not retain the
  response; a second fetch within 5 minutes raises a digest alert.
  Once the payout secrets are GDPR-erased, the endpoint returns 410
  Gone (§09, §15).
- **Interactive-session-only endpoint.** An HTTP endpoint that
  refuses all bearer tokens (scoped and delegated) and requires a
  live passkey session, because the response contains decrypted
  secret material that must not land in any persisted store
  (including `agent_action.result_json`). v1 list (§11): the payout
  manifest. Manager passkey session only.
- **Delegated token.** A bearer token created by a logged-in user
  (manager or employee) that inherits their full permissions. Audit
  records use the delegating user's identity (`actor_kind`,
  `actor_id`), with `agent_label` and `agent_conversation_ref`
  fields flagging the action as agent-executed and linking back to
  the triggering conversation. See §03.
- **Host-CLI-only administrative command.** A `miployees admin`
  verb with no HTTP surface at all, agent or human: envelope-key
  rotation, offline lockout recovery, hard-delete purge (§11). Run
  on the deployment host; authorisation is by shell access.
- **Payslip.** A computed pay document for one (employee, pay_period).
- **Pending (task).** A task whose `scheduled_for_utc` is within the
  next hour (or already past for a one-off). Distinct from
  `scheduled`; used to populate the employee "today" list (§06).
- **Property.** A managed physical place containing one or more
  units. `kind` (§04) gates stay lifecycle rule seeding: `residence`
  none, `str`/`vacation` default `after_checkout` rule, `mixed` same
  with `guest_kind_filter`.
- **QR token.** A unique 12-character Crockford base32 identifier
  assigned to every asset at creation. Encodes into a QR code for
  phone scanning; the URL pattern is
  `https://<host>/asset/scan/<qr_token>`. See §21.
- **Property closure.** A dated blackout on a property (or a specific
  unit) that prevents schedule generation (§06). iCal "Not available"
  VEVENTs become rows of this kind automatically.
- **Property kind.** Classification of a property: `residence |
  vacation | str | mixed`. Drives default area seeding, lifecycle rule
  templates, and scheduling behaviour. See §04.
- **Public holiday.** A workspace-managed holiday date with
  configurable scheduling effect (`block | allow | reduced`) and
  optional payroll multiplier. Manager-configured per holiday. See
  §06 `public_holidays`.
- **Pull-back (scheduling).** The process of moving a pre-arrival task
  to an earlier date when the ideal date falls on an unavailable day
  (leave, holiday, day off). Bounded by `max_advance_days` on the
  lifecycle rule. See §06 "Pull-back logic for before_checkin tasks".
- **Role.** A named capability bundle (maid, cook, …). `role.key` is a
  stable slug but editable; external integrations should prefer the
  ULID.
- **Schedule.** Description of when tasks materialize (RRULE).
  `paused_at` wins over `active_from/active_until`.
- **Scope (instruction).** The visibility level of an instruction:
  `global` (all properties), `property` (one property), `area` (one
  area within a property), or linked via `instruction_link`. See §07.
- **Session.** Browser-bound server-side record tied to a passkey.
- **Shift.** A clocked-in interval for an employee. `status` is
  `open | closed | disputed`.
- **SKU / item.** An inventory entry per property.
- **Stay.** A reservation of a unit within a property (guest, owner,
  staff, other — see `guest_kind`) for a date range. Overlap
  detection is per-unit. Lifecycle rules generate task bundles around
  stay events.
- **Stay lifecycle rule.** A trigger-based configuration that
  generates task bundles around stay events: `before_checkin`,
  `after_checkout`, or `during_stay`. Replaces the simpler
  turnover-template pointer. See §06.
- **Stay status.** Lifecycle state of a guest stay: `tentative |
  confirmed | in_house | checked_out | cancelled`. See §04.
- **Stay task bundle.** A set of tasks generated by a stay lifecycle
  rule for a specific stay. Replaces `turnover_bundle`. Tasks in a
  bundle share `stay_task_bundle_id`. See §06.
- **TCO (total cost of ownership).** The all-in cost of an asset:
  purchase price + expense lines + document invoices, divided by years
  owned for an annual figure. Reported per asset and aggregated per
  property. See §21.
- **Template (task template).** Reusable task definition.
- **Token.** API token; `mip_<keyid>_<secret>` on the wire.
- **Turnover.** The set of tasks generated around a stay — now
  modeled as `stay_task_bundle` rows generated by `stay_lifecycle_rule`
  entries. The `after_checkout` trigger is the direct successor of the
  former `turnover_bundle`. Gating depends on the rule's
  `guest_kind_filter` and the property's `kind`.
- **Unit.** A bookable subdivision of a property. Every property has
  at least one unit; single-unit properties auto-create a default
  unit with the unit layer hidden in the UI. Stays, lifecycle bundles,
  and iCal feeds are unit-scoped. See §04.
- **Unavailable marker.** Historical name for iCal blocks that are
  not stays; these are now modeled as `property_closure` rows with
  `reason = ical_unavailable`.
- **Welcome link.** Tokenized public URL exposing the guest welcome
  page for a stay. Revocation or expiry both serve a 410 with the
  same layout; wording differs.
- **Workspace.** The tenancy boundary in v1. One workspace = one
  employer entity. Every user-editable row carries `workspace_id`.
  All uniqueness constraints on user-editable rows are scoped to
  `workspace_id`. The v1 deployment ships a **single workspace**
  seeded at first boot, but the schema, auth, and API surface are
  already multi-tenant-ready — see §02 "Migration" and §19 "Beyond
  v1" for the path to true multitenancy. Replaces the v0
  "household."
