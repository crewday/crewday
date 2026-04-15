# AGENTS.md

Working rules for coding agents (Claude Code, Codex, Cursor, Hermes,
OpenClaw, etc.) operating on this repository. Adapted from the patterns
used by [`micasa-dev/micasa`](https://github.com/micasa-dev/micasa); where
the two disagree, this file wins.

> If you are an **LLM agent operating the running system** (taking actions
> on behalf of the household manager), this file is **not for you** — see
> [`docs/specs/11-llm-and-agents.md`](docs/specs/11-llm-and-agents.md)
> and [`docs/specs/13-cli.md`](docs/specs/13-cli.md) instead. This file
> is for agents writing code in the repo.

## Session bootstrap

At the start of every session:

1. Run `/resume-work` (or equivalent) to read the latest git log, open PRs
   and issues, uncommitted changes, and active worktrees.
2. Read the codebase map at `.claude/codebase/*.md`. These files summarize
   package layout, key types, and patterns so you do not re-explore from
   scratch. They carry a `<!-- verified: YYYY-MM-DD -->` marker. If older
   than 30 days, spot-check the documented paths and update the file.
3. Read the relevant spec under `docs/specs/`. **The spec is the source of
   truth; the code follows.** If code and spec diverge, default to
   updating the code — unless the divergence was an explicit decision
   recorded in a postmortem, ADR, or spec revision.

## Autonomy and persistence

- Default to **delivering working code**, not a plan. If a detail is
  missing, make a reasonable assumption, state it, and proceed.
- Operate as a staff engineer: gather context, plan, implement, test,
  refine. Persist to a complete, verified outcome within the turn when
  feasible.
- Bias to action; do not end a turn with clarifying questions unless you
  are truly blocked.
- Stop if you catch yourself looping — re-reading or re-editing the same
  files without progress. End the turn with a concise summary.

## Code quality bar

- Correctness and clarity over speed. No speculative refactors, no
  symptom-only patches — fix root causes.
- Follow existing conventions (naming, formatting, package layout, test
  patterns). If you must diverge, say why in the PR.
- Preserve behavior unless the task is explicitly about changing it. When
  behavior does change, gate it (feature flag or explicit release note)
  and add tests.
- Tight error handling. No bare `except:`, no silent `except Exception:
  pass`. Errors propagate or are logged explicitly.
- Type safety: the codebase is fully type-annotated. `mypy --strict`
  passes. Avoid `Any` and `# type: ignore`.
- DRY with judgment — search for existing helpers before writing a new
  one; but three similar lines is not yet a helper.

## Editing constraints

- Default to ASCII. Only introduce non-ASCII when the file already uses
  it or there is a clear reason (user-facing content, examples).
- Rare, concise comments — only where the **why** is non-obvious.
- You may land in a dirty worktree.
    - **Never revert** edits you did not make; they are the user's.
    - If the changes are in files you are touching, work with them.
    - If unrelated, ignore them.
    - If you see unexpected changes mid-task, **stop immediately** and
      ask.
- No destructive git (`reset --hard`, `checkout --`, `clean -fd`, branch
  deletion) without explicit approval.
- No `--amend` unless explicitly requested.
- No revert commits on unpushed work — use `git reset HEAD~1` instead.
- Never force-push to `main`.

## Tooling conventions

- **No `&&`** in tool calls. Run commands as separate tool calls; run
  independent ones in parallel.
- **JSON with `jq`**, not Python. Use `gh`'s `--jq` for GitHub payloads.
- **Modern CLI tools**: `rg` over `grep`, `fd` over `find`, `sd` over
  `sed`.
- **Read dependencies from the local env**: Python packages under the
  active venv; do not curl GitHub.
- **Never `cd` out of the worktree root** — always use absolute paths.

## Skill triggers (repo slash-commands)

These are executed as skills when working on this repo. Full procedures
live in the skill files themselves.

| Skill | When |
|-------|------|
| `/commit` | Every commit (enforces Conventional Commits + signed-off) |
| `/create-pr` | Every PR body, rebase merges, description upkeep |
| `/audit-spec` | After any feature adding or removing behavior |
| `/update-openapi` | After any change under `app/api/` |
| `/bump-deps` | Periodic dependency bump (uv + Python + JS tooling) |
| `/fix-osv-finding` | Every OSV finding is a blocker |
| `/pre-commit-check` | Before committing — lint, type, unit tests |
| `/new-entity` | Adding a new domain entity (see checklist) |
| `/new-migration` | Every Alembic migration (see checklist + backfill rules) |
| `/record-demo` | After any UI change (tape + GIF committed) |

## Presenting your work

Plain text to the user. CLI handles styling.

- Lead with the change and where it lives, not "Summary:".
- Inline code for paths and identifiers. Reference files as
  `app/domain/tasks.py:42`.
- Flat bullets. Short **bold** headings when grouping.
- No nested bullets.
- Do not dump files — reference paths and summarize diffs.

## Application-specific notes

- **Python is required.** All server code is Python 3.12+.
- **SQLite is the default store.** Code must also work on Postgres 15+ —
  CI runs both. Use only portable SQL or SQLAlchemy idioms.
- **HTMX is the primary interaction model.** Do not reach for React,
  Alpine, or Vue. If you find yourself wanting SPA behavior, re-read the
  frontend spec.
- **Do not bind to the public interface.** See `docs/specs/16`.
  Development and production bind to `127.0.0.1` or the `tailscale0`
  interface only. A misbound port is a blocker bug.
- **No PII to upstream LLMs without explicit opt-in.** The model client
  has a redaction layer; use it.
- **Time is UTC at rest, local for display.** Every timestamp column is
  `TIMESTAMP WITH TIME ZONE` (Postgres) or ISO-8601 UTC text in SQLite.
  Property-local time is computed on the fly from `property.timezone`.
