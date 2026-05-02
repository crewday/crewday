# 07 — Instructions (standing SOPs and knowledge base)

An **instruction** is a standing piece of content that an owner or
manager wants staff (and agents) to reference when performing work:
SOPs, house rules, safety notes, "how we do it here" guides, brand
guidelines, pet quirks, local supplier preferences.

The user requirement: *instructions exist at workspace, property, and
room/area scope (plus narrower per-template / per-asset / per-stay /
per-role scopes), and can be attached to tasks.*

## Properties of the system

- **Scope-aware**: seven scopes — `workspace` (whole workspace, the
  v0 "global" bucket), `property`, `area`, `template` (a single
  task template), `asset`, `stay`, `role` (a `work_role`). Wider
  than the v0 "global / property / area" trio because the worker
  task screen and the agent KB layer want to anchor SOPs at the
  exact unit they apply to (e.g. an oven asset, a stay-length
  bundle, the `nanny` role) without forcing a property-wide
  attachment.
- **Attachable**: can be linked to a task template, a schedule, a
  specific task, an asset, a stay, or a role via `instruction_link`
  (see "Data model"). Links plus scope-based reachability are the
  only connections between an instruction and the work that uses
  it; instructions are not nested inside templates.
- **Versioned**: every edit creates an immutable version. Tasks record
  the version in effect at completion time; the audit trail is exact
  even if instructions are later updated.
- **Retractable**: an instruction can be archived; existing links stay
  but new tasks do not auto-pick it up.
- **Indexable**: content is markdown; images allowed; searchable via
  the unified full-text search (§02).
- **LLM-fed**: instructions are injected into agent prompts when
  relevant, with scoping rules described below.
- **Rendered in context**: on the worker task screen, all applicable
  instructions are collapsed under a single "Instructions" panel,
  ordered by specificity (asset / stay / template / role > area >
  property > workspace).

## Data model

### `instruction`

| field              | type    | notes                                                                         |
|--------------------|---------|-------------------------------------------------------------------------------|
| id                 | ULID PK |                                                                               |
| workspace_id       | ULID FK |                                                                               |
| slug               | text    | URL-safe handle, UNIQUE per workspace                                         |
| title              | text    | short, human-readable                                                         |
| scope_kind         | enum    | `template | property | area | asset | stay | role | workspace`                |
| scope_id           | ULID?   | NULL iff `scope_kind = workspace`; otherwise points at the scoped row         |
| current_version_id | ULID?   | soft-ref to `instruction_version.id`; written atomically on version bump      |
| tags               | text[]  | `safety`, `pets`, `food`, ...; lowercase, deduped, capped at 20               |
| archived_at        | tstz?   | NULL on live rows; set on archive, cleared on restore (replaces v0 `status`)  |
| created_by         | ULID?   | author at create time; nullable for system-actor seeds                        |
| created_at         | tstz    |                                                                               |

Constraints:

- `scope_kind = workspace` → `scope_id` NULL.
- Any other `scope_kind` → `scope_id` set, pointing at the
  appropriate target row (a `task_template`, `property`,
  `area`, `asset`, `stay`, or `work_role`).
- `scope_id` is a soft-ref :class:`str` rather than a polymorphic
  FK because SQLAlchemy does not portably express a column-dependent
  foreign key; the domain layer enforces the kind/id pair.
- UNIQUE `(workspace_id, slug)`.

### `instruction_version`

Immutable. Every edit mints a new row and the domain layer flips
`instruction.current_version_id` to point at it; versions are
append-only.

| field             | type    | notes                                                                         |
|-------------------|---------|-------------------------------------------------------------------------------|
| id                | ULID PK |                                                                               |
| workspace_id      | ULID FK | denormalised from the parent instruction so the tenant filter rides this row directly |
| instruction_id    | ULID FK |                                                                               |
| version_num       | int     | monotonic per instruction; CHECK `version_num >= 1`                           |
| body_md           | text    | markdown; empty allowed (a draft with no body yet)                            |
| body_hash         | text    | SHA-256 hex of post-normalised `body_md` (idempotency check on bump)          |
| summary_md        | text?   | short version for tight UIs                                                   |
| attachment_file_ids | ULID[]| images/PDFs                                                                   |
| author_id         | ULID?   | references `users.id` (§02) when authored by a person; nullable for system-actor seeds (a future seed script, an agent authoring from a capability) |
| change_note       | text?   | optional human-authored revision summary                                      |
| created_at        | tstz    |                                                                               |

UNIQUE `(instruction_id, version_num)` — the same instruction
cannot mint two v3 rows.

### `instruction_link`

Explicit many-to-many between instructions and the things they apply
to. Plus one implicit link type: **scope-based automatic inclusion**
(see "Resolution" below).

| field             | type    | notes                                                                                |
|-------------------|---------|--------------------------------------------------------------------------------------|
| id                | ULID PK |                                                                                      |
| workspace_id      | ULID FK | denormalised so the tenant filter rides this row directly                            |
| instruction_id    | ULID FK |                                                                                      |
| target_kind       | enum    | `task_template | schedule | work_role | task | asset | stay`                         |
| target_id         | ULID    | soft-ref :class:`str` — points at the kind-appropriate row; resolved in application  |
| added_by          | ULID    | user_id of owner, manager, or agent                                                  |
| added_at          | tstz    |                                                                                      |

UNIQUE `(workspace_id, instruction_id, target_kind, target_id)` —
the same instruction cannot mint two link rows pointing at the same
target. The schema and table land under cd-bce + cd-q3b; the
service-layer CRUD surface (insert / list / delete) is owned by
cd-oyq's instructions service, on which the resolver and authoring
UI depend.

## Resolution: which instructions apply to a given task?

For a task with `property_id = P`, `area_id = A`, `template_id = T`,
`expected_role_id = R`, and (when applicable) a linked `asset_id`
or `stay_id`, the set of applicable instructions (live rows —
`archived_at IS NULL`) is the **union** of:

1. All `workspace`-scoped instructions — universal.
2. All `property`-scoped instructions where `scope_id = P`.
3. All `area`-scoped instructions where `scope_id = A` (and
   therefore the area belongs to `P`).
4. All `template`-scoped instructions where `scope_id = T`.
5. All `role`-scoped instructions where `scope_id = R`.
6. All `asset`-scoped / `stay`-scoped instructions where
   `scope_id` matches the task's linked asset or stay, when the
   task carries one.

The union also absorbs link-based reachability via the
`instruction_link` table — any explicit `task_template` /
`schedule` / `work_role` / `task` / `asset` / `stay` row pointing
at the instruction. The schema is in place under cd-bce + cd-q3b;
the resolver service that materialises the union ships under
cd-oyq, after which `occurrence.linked_instruction_ids` reflects
both implicit (scope) and explicit (link) reachability.

Order in the UI: more specific first (asset / stay / template /
role > area > property > workspace), each with a badge showing why
it applies ("House-wide", "Villa Sud", "Pool", "Pinned to this
template", etc.).

Duplicates (same instruction reached by two routes) are shown once with
the highest-specificity label.

## Editing semantics

- Every save creates a new `instruction_version` and points
  `current_version_id` at it.
- Tasks do **not** own the instruction list as a source of truth;
  scope-based reachability and `instruction_link` rows together
  are canonical. `occurrence.linked_instruction_ids` is a
  denormalized cache of the resolved set for a given occurrence,
  refreshed on every reachability-affecting write in the same
  transaction. Readers must not write to it directly. Until
  cd-oyq's resolver lands, the cache reflects scope-based
  reachability only; the link contribution flips on with that
  service.
- Tasks capture the **version in effect at task creation** in the
  audit log, but the cached `linked_instruction_ids` array stores
  instruction ids, not version ids — see below.
- Evidence of which **version** was surfaced at completion time lives
  in the audit log (`instruction.render` action with
  `instruction_id + version_id`).

### Why link to instruction, not version, on tasks

Staff and agents expect "the latest safety note" — pinning a task to a
version that has since been corrected would defeat the purpose. The
audit trail (version id seen at task render) gives enough forensic
clarity without freezing tasks on stale content.

If an instruction is **retroactively updated** with a critical change,
the manager can click "Mark as critical" on the new version, which
triggers an email digest entry: "Instruction 'Pool chemical handling'
was updated after the last task completion; review".

## Authoring UI (manager)

- Rich-text markdown editor with live preview; supports images
  (uploaded via the same file pipeline as evidence), headings, lists,
  callouts (`> ⚠️ Warning:` renders as a warning block).
- Scope picker at the top: **Workspace / Property / Area /
  Template / Asset / Stay / Role**, with a live-filtered selector
  per scope kind.
- Tag chips (free-form; auto-complete from existing tags).
- "Link to..." picker (UI ships with cd-oyq's instructions service
  on top of the existing `instruction_link` table): task templates,
  schedules, work roles, specific tasks, assets, stays.
- Preview shows exactly how it will render on the worker PWA.

## Reader UI (worker PWA)

On a task screen, an **"Instructions"** accordion shows, ordered
most-specific first:

- Asset / stay / template / role-scoped instructions (when the
  task carries a matching scope), each badged with what they apply
  to.
- Area-scoped instructions
- Property-scoped instructions
- Workspace-scoped instructions
- Explicit links via `instruction_link` (badge: "Linked to this
  task" / "Linked to this template" / "Linked to this asset", etc.).
  Schema in place; the resolver that surfaces them in the reader
  ships with cd-oyq.

Each entry is collapsible; the first one is expanded by default.
Images inline. Markdown rendered to HTML server-side; no client-side
markdown compilation.

## LLM use

§11 details how instructions participate in the assistant. Short
version: when the assistant is invoked in a task context, the in-scope
instructions are injected into the system prompt as a labeled
knowledge block. Instruction bodies are never sent upstream unless
the workspace's `llm.send_instructions` setting is on (default:
**on**, as they are manager-authored and rarely sensitive).
Workspace-scoped instructions are injected into every assistant call
even without a task context.

The **worker-side chat agent** (§11) is a first-class reader of
instructions. When a worker asks a question in the chat page —
"how do I reset the pool pump?", "what temperature for the linens?",
"what do I do if the oven alarm goes off?" — the agent resolves the
applicable instruction set using the same scope rules above (asset /
stay / template / role > area > property > workspace, plus
explicit `instruction_link` overrides once the cd-oyq resolver
materialises them), injects the relevant bodies into its context,
and answers inline. When the worker
is on a task screen, the agent also reads the instructions reachable
via that task's template / role / asset / stay scope. Instructions
are therefore the
primary grounding context for the worker agent; missing or out-of-
date instructions directly degrade answer quality.

## Search

- Free-text across title, tags, body.
- Scope filter (workspace / property / area / template / asset /
  stay / role).
- "Applies to <this task>" quick filter in the task view.

Instructions are also surfaced through the unified
**knowledge-base search** (§02 "Full-text search ranking —
knowledge base") that powers the agent's `search_kb` tool (§11
"Agent knowledge tools"). Instructions appear in the same ranked
result list as extracted asset-document text, each row tagged
with its `kind`. The KB tools are **additive** to the existing
in-prompt injection: instructions linked to the current task or
scoped to the user's reachable properties still flow into the
system prompt at turn start; the KB tools let the agent reach
*other* instructions on demand without inflating every turn.

## Bulk operations

- Archive (soft, reversible).
- Rescope (rare): e.g., promote a property-scoped instruction to
  workspace-scoped. A rescope is just a new version with a metadata
  marker; the audit log records the transition.

## Examples

- **Workspace**: "All employees must wear closed-toed shoes in service
  areas."
- **Property** (Villa Sud): "Keys are under the terracotta pot to the
  left of the front door. Return them there before leaving."
- **Area** (Villa Sud → Pool): "Pool chemicals are stored in the shed
  to the left. Never mix chlorine and pH-down. If you see cloudy
  water, call the manager before adding anything."
- **Role** (nanny): "Never post pictures of the children on social
  media."
- **Template** (turnover cleaning): "Change the duvet covers even if
  they look clean. Guests often sleep on top of them."
