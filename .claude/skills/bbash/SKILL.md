---
name: bbash
description: Interactive bug bashing session for crew.day. Verifies reports, clarifies only when needed, and creates paired Beads bug tasks for async implementation.
---

# Bug Bash Skill

Run an **interactive bug bashing session**. Your role is to collect,
verify, and document bugs. Do not turn the session into a general
implementation sprint.

## Target Environment

- **Default app URL**: `http://127.0.0.1:8100`
- **Remote dev URL**: `https://dev.crew.day`, but agents on this host
  cannot pass Pangolin badger forward-auth. Use the loopback URL above;
  paths are 1:1.
- **Production**: not deployed yet.

Unless the user explicitly names another environment, assume bugs are in
the loopback dev app.

## Responsibilities

1. Listen to bug reports from the user.
2. Verify the bug yourself with `curl`, repo wrappers, Playwright, or a
   focused script.
3. Ask clarifying questions only when verification is insufficient or
   user-specific context is required.
4. Create well-documented Beads tasks for fixes.
5. Pair every created non-selfreview task with the local `$beads`
   selfreview task pattern.
6. Continue the session; implementation happens asynchronously.

## Critical Rules

### Single-Line Fixes Only

You may implement directly only when the fix is a literal single-line
change, such as:

- Fixing one typo in one line of text.
- Changing one obvious value.
- Adding one missing import.
- Fixing one obvious syntax error.

If the fix needs more than one changed line, create a Beads task.

Single-line quick-fix workflow:

1. Verify the bug.
2. Confirm it is truly one changed line.
3. Make the one-line edit.
4. Run the narrow verification.
5. Tell the user what changed and ask for the next bug.

### Everything Else Becomes Tasks

For anything beyond a single-line change, you are a reporter. Create
atomic Beads tasks and keep the bug bash moving. This includes:

- Multi-line UI, CSS, API, database, or logic fixes.
- Changes across files.
- Refactors.
- New tests or fixtures.
- New feature behavior.

### Verify Before Asking

Autonomous verification is preferred over asking. Ask only when you
cannot reproduce the issue, cannot infer the intended behavior from
specs/code, or need data that is private to the user.

## Verification Tools

### Stack Status

When the app should be running, start with:

```bash
./scripts/agent-status.sh
```

If needed, bring the dev stack up:

```bash
./scripts/dev-stack-up.sh
```

Trust the `endpoints:` line from `agent-status.sh` over the compose
container health line, per root `AGENTS.md`.

### Authenticated API Checks

Prefer the wrapper for API paths:

```bash
./scripts/agent-curl.sh dev GET /w/dev/api/v1/employees
./scripts/agent-curl.sh dev POST /w/dev/api/v1/tasks '{"title":"smoke"}'
```

Use raw `curl` for public pages, health checks, redirects, and response
headers:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8100/readyz
curl -sI http://127.0.0.1:8100/w/dev/
```

### Visual And Interaction Checks

Use Playwright for:

- Layout and responsive bugs.
- JavaScript-dependent behavior.
- Forms, buttons, modals, menus, drag/drop, and keyboard interaction.
- Browser console or network errors.

Use the available Playwright MCP tools:

1. `mcp__playwright__browser_navigate` to open the loopback URL.
2. `mcp__playwright__browser_snapshot` to inspect page structure.
3. `mcp__playwright__browser_take_screenshot` for visual evidence.
4. `mcp__playwright__browser_console_messages` and
   `mcp__playwright__browser_network_requests` for runtime evidence.
5. `mcp__playwright__browser_close` when finished.

Save screenshots under `.playwright-mcp/` with descriptive names.

For authenticated browser checks, follow
`docs/dev/playwright-auth.md`; loopback Playwright needs the alias cookie
rather than the `__Host-` curl cookie.

## Session Flow

```text
User reports bug
    |
    v
Verify with curl / agent-curl / Playwright / focused script
    |
    +-- Not reproduced or needs user context -> ask a short clarifying question
    |
    +-- Confirmed
        |
        +-- Literal single-line fix -> fix, verify, report
        |
        +-- Anything larger -> create Beads task via `$beads`
                             -> ensure selfreview pair exists
                             -> report task id, continue
```

## Task Standards

Each Beads task must be atomic: one specific bug, one clear boundary,
one independent test plan. If investigation reveals multiple issues,
split them.

Each task body must include:

````markdown
## Problem / goal
[One specific bug and why it matters.]

## Evidence
[What you observed: URL, status, response body, console error,
screenshot path, logs, or exact UI state.]

## Expected behavior
[What should happen instead, grounded in specs/code when available.]

## Steps to reproduce
1. [Step]
2. [Step]
3. Observe: [bug]

## Environment
- URL: `http://127.0.0.1:8100/...`
- Workspace/user state:
- Browser/device if relevant:

## Technical context
- Key files:
- Spec reference:
- Follow pattern:

## Acceptance criteria
- [ ] [Specific, binary criterion]
- [ ] [Specific command or manual verification]

## Test plan

### Automated
```bash
[focused command]
```

### Manual
1. [Step]
2. [Expected result]
````

Prefer crew.day paths and commands in test plans, such as:

```bash
./scripts/agent-quality.sh --skip-tests
pytest tests/path/to/test_file.py -x -q
./scripts/agent-curl.sh dev GET /w/dev/api/v1/...
```

Use `./scripts/agent-quality.sh` instead of spelling out individual
Ruff/mypy commands unless the task has a narrower established command.

## Beads And Selfreview Pairing

Create Beads tasks using the local `$beads` skill standards in
`.claude/skills/beads/SKILL.md`.

Critical local rule: every non-selfreview task must have a paired
selfreview task:

- Label the paired task `selfreview`.
- Title it `Self-review: <main task title>`.
- Make it depend on the main task with `bd dep <main> --blocks <review>`.
- The selfreview task body must instruct the implementer to run
  `/selfreview` in autofix mode.
- Never pair a task that already has the `selfreview` label.

Minimal pattern:

```bash
main_id="$(bd create "bug(scope): fix specific issue" --body "..." --type bug --silent)"

review_id="$(bd create "Self-review: bug(scope): fix specific issue" --body "$(cat <<EOF
## Problem / goal
Auto-fixing self-review of the changes made under ${main_id}. Catch
bugs, missing pieces, and unintended consequences before they ship.

**Depends on: ${main_id}** (main task must be complete first).

## How to run
Run \`/selfreview\` in **autofix mode** against the working-tree changes
from ${main_id}.

- Do NOT enter plan mode.
- Do NOT ask the user to triage findings.
- Apply fixes for every BUGS, MISSING, and RISKY finding.
- Skip NITPICKS unless trivially safe.
- Run the quality gates after fixing.
- Do NOT commit, push, or close Beads tasks; \`/commiter\` will close
  ${main_id} and this selfreview task in one commit.

See [\`.claude/skills/selfreview/SKILL.md\`](../selfreview/SKILL.md).

## Acceptance criteria
- [ ] All BUGS from the self-review fixed
- [ ] All MISSING pieces completed
- [ ] All RISKY items mitigated or justified in a task comment
- [ ] Linter, formatter, type checker, and affected tests pass
- [ ] Working tree ready for \`/commiter\`
EOF
)" --labels "selfreview" --type chore --silent)"

bd dep "${main_id}" --blocks "${review_id}"
bd export -o .beads/issues.jsonl
```

If you create several bug tasks, create the blocker/dependency graph for
the main tasks first, then create a selfreview pair for each main task.

## Dependencies

Use dependencies only when one task literally cannot start before
another:

- Schema or migration work must land before code uses a column.
- Domain service work must exist before API or UI consumers use it.
- Shared utility work must exist before dependents use it.

Do not add dependencies because two bugs are related or touch the same
files.

## Duplicate Check

Before creating a task, check for duplicates:

```bash
bd list --title "keyword" --all
```

If a likely duplicate exists, ask whether to add evidence to the
existing task or create a new one.

## Priority

Use Beads priorities and labels consistently:

- `priority:critical`, `--priority 0`: app unusable, data loss,
  security/privacy exposure, payroll/timekeeping blocker.
- `priority:high`, `--priority 1`: major workflow broken, no reasonable
  workaround.
- `priority:medium`, `--priority 2`: workflow degraded, workaround
  exists.
- `priority:low`, `--priority 3`: cosmetic or minor annoyance.

## Reporting During The Session

After each confirmed task, report:

- Main task id and title.
- Paired selfreview task id.
- Key evidence captured.
- Whether it is ready now or blocked by another task.

Keep asking for the next bug until the user ends the session.

## End Of Session

When the user is done:

1. Summarize all main tasks and paired selfreview tasks created.
2. Show the dependency graph if there are dependencies.
3. Note which tasks can be worked on in parallel.
4. Run `bd ready`.
5. Confirm `.beads/issues.jsonl` was exported after Beads changes.

## Common Mistakes

- Creating one broad task for multiple bugs.
- Creating vague acceptance criteria such as "works correctly".
- Asking the user before trying to reproduce the bug.
- Forgetting the paired selfreview task.
- Pairing a selfreview task with another selfreview task.
- Using `dev.crew.day` from this host instead of `http://127.0.0.1:8100`.
- Saving Playwright screenshots outside `.playwright-mcp/`.
