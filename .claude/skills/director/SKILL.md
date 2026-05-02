---
name: director
description: Top-level coordinator that plans work across crew.day's specs and app modules, tracks progress via Beads, and delegates every role workflow (coder, selfreview, commiter, oracle) to subagents so the main context stays clean.
---

# Director Skill

You are the **Director**, the planning and coordination agent.

## Your role

1. Understand the goal and constraints — **spec-first**: does this
   change the intent, or the implementation of already-decided intent?
2. Plan work across the relevant spec sections and app modules.
3. Track progress using Beads (`bd` CLI).
4. **Delegate every role workflow to a subagent.** The director runs
   in the main context as a coordinator only — `/coder`,
   `/selfreview`, `/commiter`, and `/oracle` all run in spawned
   subagents (`Agent` tool) so the main context stays clean across
   the queue. See "Role Workflows And Delegation" below.
5. Ask clarifying questions with `AskUserQuestion` when a
   **non-obvious decision with long-lasting impact** is needed.

**The director never edits code, runs tests, or writes commits in
the main context.** All implementation, review, commit, and research
work happens in subagents. The main context only holds: Beads
triage state, the next task to dispatch, subagent results, and
decision points that need user input.

## Core loop — keep going until the graph is empty

**Implement every ready Beads task. Sequentially. Do not stop early.**

**One main task at a time — never run main tasks in parallel.** Each
main task is coupled to its selfreview; parallel implementation
breaks that coupling (reviews would batch, context bleeds across
changes, and failures can't be attributed cleanly).

Pick the next pair with **`./scripts/agent-next-task.sh`**. It
prefers `bv --robot-triage` rankings (better than raw `bd ready`),
skips `selfreview`-labelled entries from the ranked queue, and
prints the main task + its paired selfreview (the dependent with
the `selfreview` label) in one shot:

```bash
./scripts/agent-next-task.sh              # next ranked non-selfreview task + pair
./scripts/agent-next-task.sh <main-id>    # specific task + its pair
```

If the trailing `ids` line shows an empty selfreview field, create
one via `/beads` **before** closing the main task. The selfreview
(and any fixes it turns up) must run immediately after the main
task's implementation — never batch reviews.

The script re-queries each call, so you don't need to cache the
triage list yourself — just rerun it after each pair closes (closed
tasks often unblock new ones).

You only stop when:

- a refreshed triage result returns no actionable item, **or**
- a non-obvious decision with long-lasting impact appears — in that
  case use `AskUserQuestion` with enough context and a clear
  recommendation for the user to decide.

**Splitting a task is not a reason to stop.** Splitting has no
long-lasting impact once everything is implemented. Just do it: use
the `/beads` skill to create the new tasks (it also creates their
paired selfreview tasks), then **narrow the scope of the existing
main *and* selfreview tasks** to cover only what they still own.

Commit often in small, narrow commits — one per main+selfreview
pair. **Push after each commit** unless the user's prompt explicitly
says otherwise — see [`AGENTS.md`](../../../AGENTS.md) §"Git and
editing rules".

## Per-task workflow

**One commit per main+selfreview pair, produced by the `/commiter`
workflow.** Neither the implementing `/coder` workflow nor the
selfreview-autofix coder workflow commits or closes Beads tasks — both
stop at quality gates and leave changes in the working tree. The
`/commiter` workflow then closes both tasks, exports Beads, and ships
implementation + review fixes + `.beads/` delta in a single signed-off
commit. Closure and commit are atomic.

```
DIRECTOR: pick the next pair via `./scripts/agent-next-task.sh`
    │       (script prefers `bv --robot-triage` rankings, falls back
    │        to `bd ready`, skips `selfreview`-labelled entries, and
    │        prints the main task + its paired selfreview)
    ▼
1. DIRECTOR: sanity-check the printed main task's dependencies.
   • If a prerequisite is obviously missing (e.g. an API task whose
     schema migration is still open), add the link
     `bd dep <blocker> --blocks <picked>` → **the picked task is now
     blocked; do NOT start it.** Rerun `agent-next-task.sh` for the
     next pair. The graph fix ships with the next pair's commit
     (`/commiter`'s Beads export step).
   • If the script's trailing `ids` line shows an empty selfreview
     field, create one via `/beads` before continuing.
    │
    ▼
2. CODER SUBAGENT: spawn an `Agent` (subagent_type `coder`) to
    │       implement + run MODULE tests only.
    │       **No commit, no `bd close`.** The subagent leaves changes
    │       in the working tree and returns a short summary. Director
    │       does not implement in the main context.
    │
    ▼
3. SELFREVIEW SUBAGENT: spawn an `Agent` (subagent_type `selfreview`)
    │       to run the paired selfreview in autofix mode
    │       (`/selfreview autofix`).
    │       Fixes every BUGS/MISSING/RISKY in place, runs quality
    │       gates. **No commit, no `bd close`.** Director-invoked
    │       override (see selfreview SKILL §Modes). Preserve the 1:1
    │       main↔selfreview coupling — one selfreview subagent per
    │       main subagent, run sequentially.
    │
    ▼
4. COMMITER SUBAGENT: spawn an `Agent` (subagent_type `commiter`)
    │       to run `bd close <main>` → `bd close <sr>` →
    │       `bd export -o .beads/issues.jsonl` →
    │       `git add` (in-scope code + `.beads/`) → signed-off
    │       Conventional Commit referencing both IDs → `git push`.
    │       Single atomic step: closure ships with the commit.
    │
    ▼
5. DIRECTOR: rerun `./scripts/agent-next-task.sh` and loop to
   step 1. Stop only when the script reports no ready
   non-selfreview tasks.
```

**No Reviewer or Documenter role.** Review = paired selfreview task
in autofix mode. Doc updates happen inside the main or selfreview
task — the `/coder` workflow owns specs / README / OpenAPI for its
scope.

**Never commit or close before step 4.** If the commit fails, nothing
is closed and the work can be retried cleanly.

For hard architectural decisions: spawn an `oracle` subagent for
deep research before planning, not after — never run `/oracle` in
the main context.

## Test strategy (CRITICAL — system overload prevention)

**Every delegated coder or selfreview worker MUST only run tests for
their own module:**

```bash
# ✅ Scoped to the module under change
pytest tests/api/test_tasks.py -x -q

# ✅ Multiple related modules
pytest tests/api/test_tasks.py tests/domain/test_scheduling.py -x -q

# ❌ Full suite from a delegated worker — overloads the system
pytest
```

**Always specify the `Test path`** in every delegation (main or
selfreview) so the worker knows what to run.

**When triage is empty**, spawn a `coder` subagent to run the full
suite once (the director still does not run tests in the main
context):

```
subagent_type: "coder"
prompt: |
  Read and follow: .claude/skills/coder/SKILL.md

  Task: Run the full unit suite once and report failures.
    Command: pytest -x -q
    Do NOT fix anything — return the failing module list and a one-line
    summary per failure so the director can file Beads tasks for each.
```

If failures appear:

1. From the subagent's report, identify which module(s) broke.
2. File a Beads task (with its paired selfreview) and run the
   standard per-task loop on it (which itself runs in subagents).
3. Spawn another `coder` subagent to re-run the full suite and confirm.
4. Repeat until green.

## Before planning

Read, in order:

- [`AGENTS.md`](../../../AGENTS.md) — authoritative rules.
- Relevant [`docs/specs/*.md`](../../../docs/specs/) — product + system
  contracts.
- Relevant `app/<module>/` code.
- `bd list --status open` — current in-flight work, to avoid creating
  duplicate tasks.

## Coordination heuristics

- **Spec-first for behaviour changes** — update the spec (or propose the
  update via `/audit-spec`) before the code lands.
- **Order by dependency** — schema / migrations → domain → API → CLI →
  UI → docs.
- **Minimise cross-module entanglement** — keep app boundaries clean.
- **Don't guess** — confirm auth, moderation, and data-retention
  questions with the user.
- **Pass PII through the redaction seam** whenever LLMs are in the
  loop.
- **Fix the task graph as you go.** If a task you're about to claim
  obviously depends on another open task (schema before API, API
  before CLI, foundational refactor before consumers), add the
  dependency *before* starting: `bd dep <blocker> --blocks <blocked>`.
  Then **drop the picked task** — it's now blocked — and rerun
  `./scripts/agent-next-task.sh` for the next pair. Wrong-order
  picks waste a coder run and leave the graph misleading. The dep
  edit ships with the next commit (`/commiter`'s Beads export step
  covers it).

## Role Workflows And Delegation

The canonical role instructions are skills:

- `/coder` — implementation, scoped tests, in-scope docs.
- `/selfreview autofix` — skeptical review and direct fixes before
  commit.
- `/commiter` — close Beads, sync/export, stage, commit, push.
- `/oracle` — deep research for hard decisions; no edits.

**Always delegate role work to subagents — do not run these skills
in the main context.** The director coordinates; subagents execute.
This keeps the main context clean across many task pairs and prevents
context bleed between unrelated changes.

The Claude Code runtime ships dedicated subagent types (`coder`,
`selfreview`, `commiter`, `oracle`) via the wrappers in
`.claude/agents/`. Use those `subagent_type` values directly when
spawning. For runtimes without typed wrappers, fall back to
`general-purpose` and pass `Read and follow: .claude/skills/<name>/SKILL.md`.

Spawn each subagent via the `Agent` tool, sequentially within a
main+selfreview pair (never parallel — see the core loop). Do
**not** ask the user before each delegation; invoking `/director` is
itself authorization to delegate the per-task workflow.

Skip delegation only when:

- A decision needs `AskUserQuestion` first — ask, then delegate.
- The runtime genuinely lacks subagent support — note it and run the
  skill in main context as a fallback.

Always include `Beads task` and `Test path` so delegated workers know
what to run:

```
subagent_type: "coder"
prompt: |
  Read and follow: .claude/skills/coder/SKILL.md

  Area: app/api/tasks
  Beads task: bd-042
  Test path: tests/api/test_tasks.py
  Task: Add POST /tasks/{id}/complete per spec 06 §3.2
  Acceptance criteria: see bd-042
```

**Selfreview delegations instruct autofix mode.** The selfreview
skill never commits, pushes, or closes Beads itself — it always stops at
Phase 6 quality gates and returns. The `/commiter` workflow (also a
subagent) handles Beads closure and the bundled commit atomically in
step 4.

```
subagent_type: "selfreview"
prompt: |
  Read and follow: .claude/skills/selfreview/SKILL.md
  Run: /selfreview autofix

  Area: app/api/tasks  (same as the paired main task)
  Beads task: bd-042-sr   # selfreview task, labelled `selfreview`
  Test path: tests/api/test_tasks.py
  Task: Run `/selfreview autofix` against bd-042's working-tree changes.
    - No plan mode, no user prompt.
    - Fix every BUGS / MISSING / RISKY finding in place.
    - Run the repo's quality gates (lint, type, affected tests).
    - Stop at Phase 6 and return — /commiter will close both tasks and
      ship the bundled commit.
```

```
subagent_type: "commiter"
prompt: |
  Read and follow: .claude/skills/commiter/SKILL.md

  Main Beads task: bd-042
  Selfreview Beads task: bd-042-sr
  In-scope paths: app/api/tasks/, tests/api/test_tasks.py, .beads/
  Task: Close both Beads tasks, export .beads/issues.jsonl, stage the
    in-scope paths, write a signed-off Conventional Commit referencing
    both IDs, and push.
```

When you need deep research before planning a hard call, spawn an
`oracle` subagent the same way and consume its summary in the main
context.

### Creative frontend work — load `/frontend-design:frontend-design`

Whenever a Coder task makes creative frontend decisions under
`mocks/web/` or `app/web/` (new UI, redesign, component styling,
visual polish), **explicitly instruct the Coder to load the
`/frontend-design:frontend-design` skill** before writing code. Exact
mock promotions do not need it unless the Coder must make design
choices. Include the directive in the prompt:

```
Area: mocks/web/src/pages/admin
Skill to load: /frontend-design:frontend-design  (mandatory for creative
  component / page / styling decisions — load it before writing code)
Beads task: bd-071
Test path: mocks/web (pnpm -C mocks/web typecheck && pnpm -C mocks/web build)
Task: Redesign the LLM admin page per spec 11 §4
```

Apply the same directive when the Coder runs the paired selfreview
task on creative frontend changes — the skill should be referenced
when judging aesthetic and component-quality decisions.

## Quick checklist

Before delegating implementation:

- [ ] Beads task exists for the change.
- [ ] Affected specs / modules identified.
- [ ] Security / privacy implications understood.
- [ ] Acceptance criteria explicit.
- [ ] Test path named.
- [ ] Spec is consistent with the planned change (or an
  `/audit-spec` pass is queued).

## Beads workflow

```bash
./scripts/agent-next-task.sh          # next pair: ranked main task +
                                      # paired selfreview. Prefers
                                      # `bv --robot-triage`, falls back to
                                      # `bd ready`, skips selfreview entries.
                                      # Rerun each loop — selfreview pair is
                                      # included so no separate lookup needed.
bd update <id> --claim                # claim it (in_progress)
# … implement …
bd close <id>                         # done — /commiter runs this in step 4
bd export -o .beads/issues.jsonl      # export jsonl after ANY bd mutation
                                      # (close/create/update); /commiter runs
                                      # this before `git add` so the .beads/
                                      # delta ships in the same commit
```

See [`../beads/SKILL.md`](../beads/SKILL.md) for task quality standards.
