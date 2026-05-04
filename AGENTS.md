# AGENTS.md

Working rules for coding agents (Claude Code, Codex, Cursor, Hermes,
OpenClaw, etc.) operating on this repository.

> **Operating the running system as an LLM agent** (acting on behalf of
> the household manager)? This file is not for you — see
> [`docs/specs/11-llm-and-agents.md`](docs/specs/11-llm-and-agents.md)
> and [`docs/specs/13-cli.md`](docs/specs/13-cli.md). This file is for
> agents writing code in the repo.

## Environments

- **Dev**: <https://dev.crew.day> is gated by Pangolin badger
  forward-auth and is the user's remote entry point. Agents on this
  host can't pass badger and must use the loopback equivalent
  <http://127.0.0.1:8100> (same Vite container, paths 1:1). Point
  `curl`, Playwright, and scripted verification there.
- **Production**: not yet deployed. The production app code lives
  under `app/`; high-fidelity mocks remain under `mocks/`. See
  `docs/specs/19-roadmap.md`.
- **Bring the dev stack up**: `./scripts/dev-stack-up.sh` (wraps
  `docker compose -f mocks/docker-compose.yml up -d --build`, waits
  for `/readyz`, and surfaces migration / heartbeat / root-key drift
  loudly with a one-line remediation hint). The raw compose command
  still works if you want to skip the drift gate.
- **Never bind to the public interface.** Use `127.0.0.1` or the
  `tailscale0` interface only — a misbound port is a blocker bug.
  See `docs/specs/16`.

## Ask first

- **Ask before any non-obvious decision.** Use `AskUserQuestion` or
  the current runtime's equivalent. Batch related questions, but
  never silently guess at ambiguous requirements — especially in auth,
  privacy, payroll, and anything touching PII.
- **Ask before any irreversible operation** (delete, purge,
  force-push, overwrite committed work or production data). Confirmed
  per-invocation, not once-per-session.
- **Ask when an instruction is in doubt.** Do not silently reinterpret
  repo principles, workflow rules, or quality bars when trying to
  simplify them.
- **Shared worktree**: multiple agents may be working concurrently.
  Run `git status --short` before editing and before any destructive
  operation; never discard changes you didn't make; stop and ask if
  you see unexpected edits mid-task.

## Operating loop

- **Use judgment before process.** Trivial answers do not need full
  bootstrap, Beads, or role workflows. Coding work does need enough
  context to avoid trampling the shared tree.
- **Default to delivering working code**, not a plan. Make the
  reasonable assumption, state it, proceed, unless the decision is
  non-obvious or high-impact enough to require asking first.
- **Prefer the simplest complete change.** No speculative features,
  configurability, future-proofing, or abstractions for one caller. If a
  solution is growing faster than the request, stop and simplify.
- **Make scoped edits.** Every changed line should trace to the user's
  request, the relevant spec, or cleanup caused by your change. Do not
  use "surgical" as an excuse to leave the code worse; ask before
  expanding into a larger refactor.
- **Define success before looping.** For non-trivial work, keep a short
  mental or written goal like "reproduce bug -> fix -> run focused test
  -> smoke real path." Stop if you catch yourself rereading or reediting
  without new information.
- **Verify before calling it done.** Type checks and unit tests prove
  only part of the work. Exercise the real code path with Playwright,
  `curl`, CLI, or a direct script when that is the behavior being changed.
  If you cannot verify, say exactly what is missing.

These rules intentionally mirror the high-signal parts of the
Karpathy-style guidance: surface uncertainty, keep changes small, avoid
speculation, and make success verifiable.

## Partner in thought

The user expects pushback, not compliance. Flag before acting when:

- The change is materially larger than the user seems to expect.
- You see unintended consequences (perf, security, PII, cross-module
  coupling, spec drift).
- The request contradicts a spec, a recent decision, or itself.
- A simpler or cheaper alternative exists.

Say what you'd do instead in one or two lines. Don't silently "fix"
a request you disagree with.

## Start-of-task checks

- Skim recent log or worktrees only when branch context matters. Use:
  `git log --oneline -5` and `git worktree list`.
- Read the relevant spec before changing behavior. App specs live under
  `docs/specs/`; marketing-site specs live under `docs/specs-site/`.
  **Specs are the source of truth; code follows** unless an ADR or
  postmortem says otherwise.
- If `bd` is available, check it when the task is more than a tiny
  same-file fix. Claim an exact matching issue with
  `bd update <id> --claim`; skip Beads if no issue fits or the tool is
  unavailable.

## Dev verification helpers

Use these when the task needs a running app, API smoke, or UI smoke.

**Default to the wrappers** — they cache + refresh the dev session and
replace several tool calls per check:

- `./scripts/agent-status.sh` — compose health, `/readyz`, `/healthz`,
  alembic current vs head, git branch + dirty count. Exit 0 when the
  stack is ready. Run first when a smoke fails.
  - Trust the `endpoints:` line over the compose `stack:` line. The
    `app-api` container's Docker healthcheck flips to `unhealthy`
    when the LLM budget refresh tick logs `OperationalError` per
    workspace (dev DB has stale workspace rows that the refresh
    can't read), but `/readyz` + `/healthz` keep returning 200 and
    every API path keeps serving — don't chase a fake outage.
- `./scripts/agent-curl.sh <ws> <METHOD> <path> [body]` — authenticated
  curl against `http://127.0.0.1:8100`. Caches the cookie per
  workspace+email, auto-refreshes on stale sessions, pretty-prints JSON,
  writes `[<status> <METHOD> <path>]` to stderr, exits non-zero on
  4xx/5xx. Path is appended verbatim — include `/w/<slug>/api/v1/...`.
  ```bash
  ./scripts/agent-curl.sh dev GET  /w/dev/api/v1/employees
  ./scripts/agent-curl.sh dev POST /w/dev/api/v1/tasks '{"title":"smoke"}'
  ```
- `./scripts/agent-code-health.py` — lizard-backed digest of the worst
  per-function offenders by cyclomatic complexity, function length, and
  parameter count, plus token-based duplicate code blocks. Defaults
  scan `app/` + `app/web/src/`; pass paths to narrow. Not a gate
  (always exits 0 on success) — used to find batches of refactor
  targets. Override thresholds via `CCN_THRESHOLD` / `NLOC_THRESHOLD`
  / `PARAM_THRESHOLD`. Use `--json-out <path>` for the complete
  machine-readable report; follow-up refactor tasks should prove zero
  unsuppressed findings in their target area by checking
  `.summary.<category>.unsuppressed == 0` for the relevant categories.
  Suppressions are inline `code-health: ignore[ccn] <reason>` comments
  inside the target function or `code-health: ignore[duplicate] <reason>`
  inside a duplicate block; the reason is required and remains visible
  in JSON output.
  ```bash
  ./scripts/agent-code-health.py                   # default scan
  ./scripts/agent-code-health.py app/domain/tasks  # specific subtree
  ./scripts/agent-code-health.py --no-dup --top 20
  ./scripts/agent-code-health.py --json-out /tmp/code-health.json
  ```

Reach for the primitives below when you need the raw cookie (Playwright,
multi-step scripts) or the wrappers misbehave.

- Bring the stack up with:
  ```bash
  ./scripts/dev-stack-up.sh
  ```
  (`docker compose -f mocks/docker-compose.yml up -d --build` is the
  raw equivalent; the wrapper adds a `/readyz` drift gate that names
  the failing check and prints a remediation hint instead of letting
  the next test session inherit a stale alembic head.)
- Use the loopback app, not `dev.crew.day`: `http://127.0.0.1:8100`.
- Create a dev session inside the compose stack:
  ```bash
  docker compose -f mocks/docker-compose.yml exec app-api \
    python -m scripts.dev_login --email me@dev.local --workspace smoke
  ```
  Stdout is `__Host-crewday_session=<value>`. Feed it to
  `curl -b "$cookie" http://127.0.0.1:8100/w/smoke/api/v1/...`.
- **Playwright on loopback needs an alias cookie**, not the curl one
  (`__Host-` prefix requires `Secure`, which browsers refuse on
  plain-HTTP). See [`docs/dev/playwright-auth.md`](docs/dev/playwright-auth.md)
  for the `--output playwright` recipe and the e2e helper that wraps it.
- Host-side login variant:
  ```bash
  CREWDAY_DEV_AUTH=1 ./scripts/dev-login.sh <email> <slug>
  ```
  Requires `uv sync` or `pip install -e .`.
- **Personal passkey re-seed.** After a DB reset, run
  `./scripts/dev-seed-personal.sh apply` to rehydrate your user +
  workspace + passkey rows from `scripts/dev_seed_personal.json` so
  your physical authenticator still works at `https://dev.crew.day`.
  See the script docstrings for `capture` and other flags.
- If `dev_login`, `/readyz`, or a smoke request fails with a missing
  column/table, run:
  ```bash
  docker compose -f mocks/docker-compose.yml exec app-api alembic upgrade head
  ```
  If the disposable dev DB is still broken, reset only the dev app volume.
  Do not reset any non-dev database.

## Keep this file fresh

Treat `AGENTS.md` / `CLAUDE.md` (same file; `CLAUDE.md` is a symlink
for Claude compatibility), `.claude/skills/`, and `.claude/agents/` as
living instructions. Update them in the same turn when:

- An instruction was wrong, stale, or missing and cost you a retry.
- A skill's procedure failed or produced the wrong output shape.
- You discovered a convention or trap the next agent will also hit.
- The user corrected you on something that will recur.

Prefer editing existing files over adding new ones. Mention the
update in your wrap-up.

## Code quality bar

- **DRY is first-class.** Search (`rg`, `fd`) for an existing helper
  or pattern before writing. Extract when two copies
  share a reason to change; wait for the third use otherwise. Same
  for prose — docs reference code, they don't restate it.
- **Quality over speed, always.** Do it the *right* way even when
  slower — fix root causes, refactor the rough edge, write the
  missing test. Shortcuts rot the codebase; disciplined craft
  compounds into one that stays easy to change. Refactor when it
  genuinely improves things — but **confirm intent before starting**,
  so scope creep is conscious.
- **Fix what you find.** If you hit a broken test, stale type, dead
  import, or bit-rotted helper while working, fix it — don't route
  around it because "I didn't write this". The one exception is a
  dirty file you didn't touch: another agent may own it, so leave
  it alone and flag it. Everything else is yours now.
- **Don't fear refactoring.** The codebase should get better with
  each pass, not rot. If a module is fighting you, clean it up in
  the same turn (or file a Beads task if it's out of scope). Small
  improvements compound; silent avoidance guarantees decay.
- **Test suite is a first-class asset.** Brittle, flaky, or slow
  tests are bugs — fix them, don't work around them. Target: full
  unit suite under a few minutes. If you add tests that push past
  that, refactor (parallelise, split integration from unit, trim
  fixtures) rather than accepting the slowdown.
- **Scope stays honest.** Karpathy-style simplicity means no speculative
  features or abstractions, not lower quality. If the clean solution is
  larger than expected, flag the tradeoff and ask.
- Follow existing conventions. If you must diverge, say why in the
  PR.
- Preserve behavior unless the task is explicitly about changing it.
  When behavior changes, gate it (feature flag or release note) and
  add tests.
- Tight error handling. No bare `except:`, no silent
  `except Exception: pass`.
- Type safety: `mypy --strict` passes. Avoid `Any`, `cast(...)`, and
  `# type: ignore` unless there is no better typed boundary and the
  reason is documented.

## Git and editing rules

- Default to ASCII; only introduce non-ASCII when the file already
  uses it or there's a clear reason (user-facing content, examples).
- Rare, concise comments — only where the **why** is non-obvious.
- **Dirty worktree:** never revert edits you didn't make; work with
  overlapping changes; ignore unrelated ones; stop and ask if
  unexpected changes appear mid-task.
- **No destructive git** (`reset --hard`, `checkout --`, `clean -fd`,
  branch deletion) without explicit approval.
- No `--amend` unless requested. No revert commits on unpushed
  work — use `git reset HEAD~1`.
- **Push after every commit.** Run `git push` directly; only reach
  for `git pull --rebase` if the push is rejected as
  non-fast-forward (an unconditional rebase can collide with another
  agent's in-progress work).
- **Never force-push.** Commit directly to the current branch by
  default, even when it is `main`; only cut a branch + PR if the user
  asks for review or the change is risky enough to warrant one.
- If a push fails, diagnose the root cause; do not `--no-verify`,
  don't bypass hooks.

## Tooling conventions

- **No `&&` in agent tool calls.** Run commands as separate calls;
  run independent ones in parallel. Existing shell scripts and
  workflow YAML may use normal shell control flow.
- **JSON with `jq`**, not Python. Use `gh`'s `--jq` for GitHub
  payloads.
- **Regenerate OpenAPI with `make openapi`.** Wraps
  `python -m scripts.regen_openapi` (see its docstring for the
  stable-formatting choices). Use `make openapi-check` in CI / before
  pushing to fail when `docs/api/openapi.json` has drifted.
- **Modern CLI tools**: `rg` over `grep`, `fd` over `find`, `sd` over
  `sed`.
- **Quality gate wrapper.** Run `./scripts/agent-quality.sh` to apply
  `ruff format` + `ruff check --fix` autofixes, re-check the remaining
  lint/format issues, and run `mypy --strict app` (CI parity). Exit 0
  means clean; non-zero prints exactly what still needs a manual fix.
  Use it instead of running the individual `uv run ruff …` / `uv run
  mypy …` commands by hand.
- **Read dependencies from the local env**: Python packages under
  the active venv; do not curl GitHub for dependency source.
- **Never `cd` out of the worktree root** — always use absolute
  paths.

## Skills and Beads

Procedures live in `.claude/skills/<name>/`; Claude-compatible wrappers
live in `.claude/agents/`. Use a skill when the user names it or when it
clearly matches the work. Do not turn every small task into a workflow.

- `/specs`: interactive spec and mock co-evolution while there is no prod
  code.
- `/audit-spec`: after a feature adds or removes behavior.
- `/selfreview`: skeptical pass on non-trivial edits before handoff.
- `/security-check`: auth, permission, PII, or privacy review.
- `/gap-finder`: pre-implementation spec-gap walk.
- `/director`: larger cross-module planning and delegation.
- `/coder`: scoped implementation workflow.
- `/commiter`: close Beads, stage explicit paths, commit, and push.
- `/oracle`: hard decisions; no edits.
- `/beads`: create atomic follow-up tasks.
- `/frontend-design:frontend-design`: mandatory before creative frontend
  work under `mocks/web/` or `app/web/`; not needed for exact mock copying.
- `/ai-slop`: remove overcomplication, noisy comments, speculative code,
  and bloated prose before shipping.

For larger changes, the usual sequence is `/director` -> `/coder` ->
`/selfreview autofix` -> `/commiter`. Use it when the scope justifies the
overhead or the user asks for that flow.

**Every implementation plan must end with `/selfreview`** — regardless
of scope — to catch bugs, missing pieces, and unintended consequences
before pushing. Pure explanation, investigation, or brainstorming does
not need a selfreview step unless it leads to edits.

crew.day uses **Beads** (`bd`) as its task queue. If it is not on `PATH`,
skip it and do not install tooling unless asked. Use Beads for claimed
work and follow-ups that should survive the session; do not create issues
for tiny same-file fixes.

```bash
./scripts/agent-next-task.sh           # next ready non-selfreview task + its paired selfreview
./scripts/agent-next-task.sh <id>      # specific task + its paired selfreview
bd update <id> --claim
bd close <id>
bd export -o .beads/issues.jsonl
```

`agent-next-task.sh` prefers `bv --robot-next` rankings (matching the
director skill's "pick from the top of the triage list" rule), falls
back to `bd ready` when `bv` is missing, skips entries labelled
`selfreview`, and surfaces the paired selfreview task (dependent with
the `selfreview` label) via `bd show`. The output banner names the
ranking source it used. `--id-only` prints just the main id (handy to
pipe into `bd update <id> --claim`); `--no-bv` forces the `bd ready`
path.

**Pair a selfreview with every code-work Beads task you create.** Even
when not using `/beads` or `/director`. The selfreview is a separate
`chore` task with the `selfreview` label and a `blocks` edge from the
selfreview to the parent (`bd dep add <selfreview-id> <parent-id>`).
`agent-next-task.sh` relies on this pairing to surface the review step;
parents created without it will be picked up bare. Skip pairing only
for non-code tasks (docs-only, ops, tracking issues).

After any Beads change, export `.beads/issues.jsonl` and include it in
the same commit. Link dependencies only when one task literally cannot
start before another.

## Presenting your work

Plain text to the user; CLI handles styling.

- Lead with the change and where it lives, not "Summary:".
- Inline code for paths and identifiers. Reference files as
  `app/domain/tasks.py:42`.
- Flat bullets. Short **bold** headings when grouping. No nested
  bullets.
- Don't dump files — reference paths and summarize diffs.

## Application-specific notes

- **Python 3.14+** for all server code. Note that 3.14 accepts
  `except ValueError, IndexError:` as a multi-class `except` clause
  (no parens). It is **valid syntax** here — do not "fix" it back to
  the parenthesized form. Agents have repeatedly mis-corrected this;
  if you see it in the codebase, leave it alone.
- **SQLite default; Postgres 15+ supported** — CI runs both. Use
  portable SQL or SQLAlchemy idioms.
- **React frontend.** Mocks (and the upcoming production frontend)
  are a Vite + React + TypeScript strict SPA served by FastAPI from
  `mocks/web/dist`. TanStack Query with optimistic mutations;
  cross-client coherence is SSE-driven (one
  `EventSource('/events')` feeds `queryClient.invalidateQueries`).
  No Alpine, Vue, Tailwind, or HTMX. See `docs/specs/14`.
- **Two spec trees, two surfaces.** App specs live under
  `docs/specs/` and govern everything at `app.crew.day` (and the
  demo at `demo.crew.day`). Marketing-site specs live under
  [`docs/specs-site/`](docs/specs-site/) and govern everything at
  `crew.day` — the landing pages and the agent-clustered
  suggestion box. Keep substantive changes in their own tree;
  cross-tree pointers are fine when one surface needs to know
  the other exists (e.g. the app's env-var table mentioning the
  feedback bridge), but actual content lives where it's owned.
  Site is optional for self-hosters and has its own build +
  deploy under `site/` — see [`docs/specs-site/00-overview.md`](docs/specs-site/00-overview.md)
  for the stack (Astro + React islands, FastAPI + SQLite).
- **Semantic CSS classes only.** Name after the thing
  (`task-card`, `shift-timeline`, `payroll-summary`), not the look.
  No utility/atomic classes (Tailwind-style), no inline `style=""`,
  no presentational attributes (`bgcolor`, `align`). Reuse before
  inventing; promote variants via modifiers (`task-card--overdue`).
  Justify one-offs in the PR.
- **Design language is in [`DESIGN.md`](DESIGN.md)** at the repo
  root — palette, type scale, radii, elevation, component shapes,
  do's and don'ts. It carries normative tokens in YAML frontmatter
  and prose rationale below. Read it before any visual change.
  The living CSS source tree is `mocks/web/src/styles/`
  (`tokens.css`, `globals.css`, `fonts.css`, `reset.css`);
  `app/web/src/styles/` is the reviewed mirror for promoted production
  UI, per
  `docs/specs/14-web-frontend.md` "App / Mock Ownership". **If
  `DESIGN.md` and the CSS disagree on any value, stop and ask the user
  which side is correct, then fix the wrong side in the same turn. Never
  silently match one to the other.
- **No PII to upstream LLMs without explicit opt-in.** Use the
  model client's redaction layer.
- **Time is UTC at rest, local for display.** Timestamp columns are
  `TIMESTAMP WITH TIME ZONE` (Postgres) or ISO-8601 UTC text
  (SQLite). Property-local time is computed on the fly from
  `property.timezone`.
- **Playwright screenshots go to `.playwright-mcp/`.** Always pass
  `filename` under that directory with a descriptive name (see
  `.playwright-mcp/README.md`). Close the browser
  (`mcp__playwright__browser_close`) when done.
- **End-to-end Playwright suite (`tests/e2e/`)** runs against the
  dev compose stack plus an e2e override that aligns WebAuthn with
  the loopback origin. Fast (~10 s on Chromium). For the full
  invocation (compose override, `playwright install`, pytest flags
  that actually emit traces/videos), see
  [`tests/e2e/README.md`](tests/e2e/README.md).

## Session wrap-up

- Run the narrowest quality gates that prove the change. Say what you
  could not run.
- Close or block any Beads issue you claimed, then export
  `.beads/issues.jsonl`.
- Commit and push only when the user asked for it or the workflow requires
  it. Use `/commiter` for that path.
- Summarize briefly: what changed, where it lives, verification, and any
  follow-up already filed.
