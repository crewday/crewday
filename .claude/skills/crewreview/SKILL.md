---
name: crewreview
description: Full crew.day codebase review with parallel subagents. Runs coverage, fans out domain reviewers, re-verifies every finding, triages with the user, and files paired Beads tasks for accepted items.
---

# Full Codebase Review Skill (crew.day)

Conduct a comprehensive review of the crew.day codebase using parallel
read-only subagents, one per domain. Produce actionable, verified
findings; triage them with the user; and file properly paired Beads
tasks for the accepted ones.

This is the heavyweight periodic sweep — distinct from `/selfreview`,
which is a skeptical pass over *your own recent diff*. Use `crewreview`
when the user wants a broad health check of the whole tree (or a named
surface), not a review of a single change.

## Architecture context (read before scoping)

crew.day is **not** a Django monolith. Reviewers must be scoped to this
stack:

- **Server**: FastAPI, Python 3.14+, `mypy --strict`, hexagonal layout
  under `app/` — `domain/` (pure), `ports/` + `adapters/` (boundaries),
  `services/`, `api/` + `http/` (REST, spec 12), `auth/` `authz/`
  `security/` `tenancy/` (specs 03, 15).
- **Persistence**: SQLAlchemy + Alembic; SQLite default, Postgres 15+
  supported (CI runs both). DB concretions live in
  `app/adapters/db/`; migrations in `migrations/`.
- **App frontend**: `app/web/` — Vite + React + TypeScript strict SPA,
  TanStack Query with optimistic mutations, SSE coherence via one
  `EventSource('/events')`. **No Alpine, Vue, Tailwind, or HTMX.**
  Semantic CSS only (`app/web/src/styles/`, tokens in `tokens.css`);
  design language is normative in `DESIGN.md`.
- **Marketing site**: `site/` — Astro + React islands, FastAPI + SQLite.
  Governed by a *separate* spec tree.
- **Two spec trees**: app specs in `docs/specs/` (govern `app.crew.day`),
  site specs in `docs/specs-site/` (govern `crew.day`). **Specs are the
  source of truth; code follows.** Spec drift is a first-class finding.
- **Task queue**: Beads (`bd`). Every code-work task needs a paired
  `selfreview` task (see Phase 8).

## Workflow Overview

```
1. SCOPE + LOAD DECLINED POLICIES  (docs/reviews/declined-policies.md)
   ↓
2. RUN TEST COVERAGE               (pytest --cov; note vitest gaps)
   ↓
3. SPAWN PARALLEL REVIEWERS        (domain subagents, read-only)
   ↓
4. COLLECT REPORTS                 (each writes to <reports>/)
   ↓
5. CREATE SUMMARY                  (aggregate + coverage → SUMMARY.md)
   ↓
6. RE-VERIFY FINDINGS              (skeptical pass; drop hallucinations)
   ↓
7. TRIAGE WITH USER                (AskUserQuestion, decision-grade)
   ↓
8. DOCUMENT DECLINED               (docs/reviews/declined-policies.md)
   ↓
9. CREATE PAIRED BEADS TASKS       (accepted items + selfreview pairs)
```

Ephemeral reports (`<reports>/`) go in the **session scratchpad**, not
the repo — they are throwaway. The only committed artifact is the
declined-policies file. Do not create a `./reports/` directory in the
worktree.

## Phase 0: Scope the review

Decide breadth before spawning anything. Ask the user with
`AskUserQuestion` only if it is ambiguous; otherwise state your
assumption and proceed:

- **Full sweep** (default) — both surfaces, all domains below.
- **App only** (`app/`, `app/web/`) or **Site only** (`site/`).
- **Single domain** — e.g. "just security" or "just the frontend".

Scope determines which reviewers you launch in Phase 3. Do not run the
site reviewers for an app-only pass, and vice versa.

## Phase 1: Load declined policies (CRITICAL)

Before spawning any reviewer, read the declined list so reviewers skip
already-rejected items:

```bash
cat docs/reviews/declined-policies.md 2>/dev/null || echo "(no declined policies yet)"
```

If the file does not exist yet, there are no declined items — you will
create it in Phase 8 the first time the user declines something. Store
the declined items in context and paste the relevant ones into each
subagent prompt.

## Phase 2: Run test coverage

Backend coverage (`pytest-cov` is a project dependency). The default
`addopts` apply a marker filter and `-n auto`; keep them and just add
coverage flags:

```bash
uv run pytest --cov=app --cov-report=term-missing --cov-report=html:"<reports>/htmlcov"
```

Frontend has its own suites — note their existence for the tests
reviewer rather than forcing a full run every time:

- App SPA units: `app/web` via `vitest` (`npm --prefix app/web test`).
- E2E: `tests/e2e/` (Playwright against the dev compose stack; see
  `tests/e2e/README.md`).

Parse backend output for: overall %, per-package % (`app/domain`,
`app/api`, `app/services`, `app/auth`, `app/adapters/db`, …), and the
lowest-covered files. Save to `<reports>/coverage-report.md`:

```markdown
# Test Coverage Report
**Run Date:** {date}  **Overall (backend):** {XX.X}%

## Coverage by Package
| Package | Statements | Missing | Coverage |
|---------|-----------|---------|----------|
| app/domain | ... | ... | ...% |
| app/api    | ... | ... | ...% |
...

## Lowest-Coverage Files (Bottom 10)
| File | Coverage | Missing Lines |
|------|----------|---------------|
...
```

**Coverage severity heuristic** (adapt to the file's risk — auth,
payroll, tenancy, and money paths are higher stakes):
- CRITICAL if a security/payroll/tenancy path is materially untested.
- HIGH if a domain/service module sits well below the package norm.
- MEDIUM for ordinary gaps.

Pass this file to the tests reviewer so it targets real gaps.

## Phase 3: Spawn parallel reviewers

Launch the in-scope reviewers **in a single message with parallel tool
calls**. Each is a read-only agent (`subagent_type: "Explore"`), focused
on one domain, that writes only **improvements needed** (never a
"what's good" section) with `file:line` locations and a priority.

Shared requirements for every reviewer prompt:

- **Output** to `<reports>/{domain}.md` as a table:
  `Priority | Location (file:line) | Issue | Suggested Fix`, grouped by
  category, with a priority count at the top.
- **Priorities**: CRITICAL / HIGH / MEDIUM / LOW.
- **Read the owning spec first** (named per reviewer) — specs are the
  source of truth; report code that drifts from them.
- **Honor conventions** in `CLAUDE.md` and `DESIGN.md`.
- **Exclusions**: paste the declined items from Phase 1 and instruct the
  reviewer to skip anything matching them.

### Reviewer set

Pick the subset that matches the Phase 0 scope.

**App server (`app/`):**

1. `domain-services` — `app/domain/`, `app/services/`, `app/ports/`,
   `app/adapters/`. Focus: hexagonal boundary violations (domain
   importing adapters, Protocol seams per spec 01/02), complexity,
   duplication, error handling (no bare `except`, no silent
   `except Exception: pass`), dead code. Specs: `docs/specs/01`, `02`.

2. `api-http` — `app/api/`, `app/http/`, `app/agent/`, `app/cli`.
   Focus: REST contract vs `docs/specs/12` and `docs/api/openapi.json`
   drift, status codes, validation, pagination, error envelopes, CLI
   parity (spec 13). Flag anything needing `make openapi`.

3. `auth-security-pii` — `app/auth/`, `app/authz/`, `app/security/`,
   `app/tenancy/`, `app/abuse/`, `app/mail/`. Focus: authN/authZ gaps,
   tenant isolation, token handling (spec 03), **PII leaving to
   upstream LLMs without opt-in / redaction layer**, session/cookie
   posture, CSP/security headers. Specs: `docs/specs/03`, `15`, `11`.
   Match controls to crew.day's real threat model — do **not** invent
   enterprise security theater.

4. `db-migrations` — `app/adapters/db/`, `migrations/`, `alembic.ini`.
   Focus: missing indexes/constraints, N+1 patterns, non-portable SQL
   (must work on SQLite **and** Postgres), migration correctness and
   reversibility, `TIMESTAMP WITH TIME ZONE` / ISO-8601-UTC discipline
   (time is UTC at rest, local for display). Spec: `docs/specs/02`.

5. `tests-coverage` — `tests/`, `app/web` vitest specs, `tests/e2e/`.
   Focus: real coverage gaps (reference `<reports>/coverage-report.md`),
   fixture duplication, missing edge cases, brittle/flaky/slow tests
   (the suite is a first-class asset; target full unit suite under a few
   minutes). Spec: `docs/specs/17`. Prioritize untested
   auth/payroll/tenancy paths as CRITICAL.

**App frontend (`app/web/`):**

6. `react-frontend` — `app/web/src/` (excluding `styles/`). Focus: React
   correctness, TanStack Query cache/optimistic-mutation bugs, SSE
   invalidation coherence, TS-strict violations, `any`/`as` casts,
   accessibility, error/loading states, import-boundary breaks
   (`importBoundaries.test.ts`). Run/anticipate `./scripts/react-doctor-gate.sh`
   findings. Spec: `docs/specs/14`.

7. `design-css` — `app/web/src/styles/`, and JSX `className` usage.
   Focus: **semantic-class discipline** (no utility/atomic classes, no
   inline `style=""`, no presentational attrs), token usage vs
   hardcoded values, `DESIGN.md` conformance (palette, type scale,
   radii, elevation), responsiveness. **If `DESIGN.md` and the CSS
   disagree on a value, flag it for the user to arbitrate — never pick a
   side silently.** Spec: `docs/specs/14`, `DESIGN.md`.

**Marketing site (`site/`):**

8. `site` — `site/web/`, `site/api/`. Focus: Astro/React-island
   correctness, SEO/meta/structured data, landing + suggestion-box
   behavior, FastAPI+SQLite backend, deployment/security posture.
   Specs: `docs/specs-site/` (its own tree — do not apply app specs).

**Cross-cutting (run for any scope):**

9. `spec-drift` — walk `docs/specs/` (and `docs/specs-site/` when in
   scope) against the code. Focus: behavior in code that no spec
   describes, and spec'd behavior missing from code. This is crew.day's
   highest-signal reviewer — specs are the contract.

10. `i18n` — `app/i18n/`, `app/web/src/i18n/`, `babel.cfg`, locale
    catalogs. Focus: hardcoded user-facing strings, missing extraction,
    untranslated keys, locale fallbacks. Spec: `docs/specs/18`. Note the
    `i18n_extract.py` script.

11. `dry` — whole in-scope tree. Focus: duplicated Python logic, repeated
    React components/hooks, repeated CSS patterns that should be semantic
    classes, restated prose in docs. Apply the repo rule: extract when
    two copies share a reason to change; **flag at the third use**.
    Report all locations per duplication.

12. `config-deploy` — `docker-compose*.yml`, `Makefile`, `scripts/`,
    `deploy/`, `pyproject.toml`, CI. Focus: settings/env handling,
    dependency posture, reproducibility, the bind-guard rule (nothing on
    the public interface). Spec: `docs/specs/16`.

### Subagent prompt template

```
# {Domain} Review — crew.day

## Objective
Find ALL improvements needed in {scope}. Do NOT list what is good.

## Scope
{files / packages / patterns}

## Read first (source of truth)
- Spec(s): {docs/specs/NN ...}
- Conventions: CLAUDE.md, DESIGN.md (for visual work)
- Coverage data: <reports>/coverage-report.md  (tests reviewer only)

## Output
Write to <reports>/{domain}.md as a markdown table:
| Priority | Location (file:line) | Issue | Suggested Fix |
Group by category; put a CRITICAL/HIGH/MEDIUM/LOW count at the top.

## Exclusions — already declined by the user, DO NOT REPORT
{paste matching items from docs/reviews/declined-policies.md}

Skip anything matching a declined item. Report only concrete, located,
actionable issues; skip speculative or "nice to have" abstractions —
crew.day values the simplest complete change over future-proofing.
```

## Phase 4: Create summary

After reviewers finish, write `<reports>/SUMMARY.md`: coverage table,
per-domain issue counts, the full CRITICAL list across domains, a
prioritized action list, and links to each report. Keep it skimmable.

## Phase 5: Re-verify findings (CRITICAL — before any user question)

Every finding shown to the user must be real, reproducible, and grounded
in the current code/specs. Delegate verification in **topic batches**
(3–4 skeptical subagents total), never one per finding:

1. **Security + auth + tenancy + config**
2. **Domain + services + api + db + tests + DRY (Python)**
3. **React + CSS/design + i18n + site + spec-drift**

Each verifier must try to **disprove** each finding: confirm the
`file:line` evidence and real behavior, reject false positives, downgrade
overstated severity, and flag duplicates for merge. Confirm each survivor
does not match `docs/reviews/declined-policies.md`.

Write `<reports>/verification-{topic}.md`:

```
| Status | Domain | Location | Recommendation | Evidence | Rationale |
| VERIFIED | ... |
| REJECTED | ... |
| NEEDS_MANUAL_VALIDATION | ... |
```

Consolidate only `VERIFIED` items into `<reports>/VERIFIED-FINDINGS.md`.
Only verified findings go to triage.

## Phase 6: Triage with the user (AskUserQuestion required)

Present verified findings with `AskUserQuestion`, **one question per tool
call** (batching similar items within a question is fine). Before each
question give a short plain-language context block: what was verified,
why it matters now, the trade-off of each option, and your recommendation
with confidence level (high/moderate/low). Follow crew.day's
"partner in thought" rule — lead with the strongest counter-argument, do
not validate premises, do not anchor on the user's framing.

Order: CRITICAL individually → HIGH batched by domain → MEDIUM/LOW offer
to batch or skip a whole category.

```yaml
- question: "{issue}? — {one-line consequence if unfixed}"
  header: "{Domain}"
  multiSelect: false
  options:
    - label: "Fix now (Recommended)"
      description: "File task + paired selfreview. {benefit / effort / risk}"
    - label: "Fix later"
      description: "File low-priority task. {tradeoff of delay}"
    - label: "Decline permanently"
      description: "Skip in future reviews. {downside of not fixing}"
```

Decision handling:
- **Fix now** → Phase 8 task at appropriate priority.
- **Fix later** → Phase 8 task at `--priority 3` (or 4) with a note.
- **Decline permanently** → Phase 7 (documented + committed).

## Phase 7: Document declined items

Append declined items to `docs/reviews/declined-policies.md` (create it,
and `docs/reviews/` if absent) so future reviews skip them, then commit
so the decision persists:

```markdown
### {Issue title} — Declined {YYYY-MM-DD}
- **Reason**: {user's reason or "User preference — deprioritized"}
- **Location**: {file:line if applicable}
- **Domain**: {domain}
```

```bash
git add docs/reviews/declined-policies.md
git commit -m "docs(reviews): record declined items from $(date +%Y-%m-%d) review"
git push
```

(Per CLAUDE.md, push after every commit.)

## Phase 8: Create paired Beads tasks

For each accepted item, create an **atomic** task, then its required
**paired selfreview** task. Skip Beads only for tiny same-file fixes you
would just do inline, or if `bd` is not on `PATH`.

```bash
# Parent work task
bd create "fix: [auth] tighten session cookie lifetime" \
  --type bug --priority 1 \
  --description "$(cat <<'EOF'
## Context
{why it matters — grounded in the verified finding}
## Location
- app/auth/session.py:169
## Current behavior
{...}
## Expected behavior
{...}
## Acceptance criteria
- [ ] {testable}
- [ ] {testable}
## Test plan
{how to verify — pytest path, curl, Playwright}
## References
- docs/specs/15-security-privacy.md
EOF
)"

# Paired selfreview task (chore + label + blocks edge from review to parent)
bd create "chore: selfreview [auth] session cookie lifetime" \
  --type chore --labels selfreview \
  --description "Skeptical /selfreview autofix pass over the parent's diff."
bd dep add <selfreview-id> <parent-id>   # selfreview blocks parent close
```

Rules (from CLAUDE.md):
- Every code-work task gets a paired selfreview task; docs-only / ops /
  tracking tasks do not.
- Link real dependencies only when one task literally cannot start
  before another (`--deps blocks:<id>` / `bd dep add`).
- After any Beads change, **export and commit** the state:

```bash
bd export -o .beads/issues.jsonl
git add .beads/issues.jsonl
git commit -m "chore(beads): tasks from $(date +%Y-%m-%d) codebase review"
git push
```

## Quality checklist

- [ ] Scope decided (Phase 0); only in-scope reviewers launched.
- [ ] Declined policies loaded and passed to every reviewer.
- [ ] Backend coverage report generated; frontend suites noted.
- [ ] All in-scope reviewers wrote to the scratchpad `<reports>/`.
- [ ] SUMMARY.md aggregates findings + coverage.
- [ ] Verification pass done; only VERIFIED findings triaged.
- [ ] User triaged via AskUserQuestion, one question per call.
- [ ] Declined items appended to `docs/reviews/declined-policies.md`,
      committed, and pushed.
- [ ] Accepted items became atomic Beads tasks, each with a paired
      selfreview task and a `blocks` edge.
- [ ] `.beads/issues.jsonl` exported, committed, and pushed.

## Tips

- Run coverage first so the tests reviewer has real data.
- Reviewers are read-only (`Explore`) — this skill never edits product
  code; it only produces reports, declined-policy entries, and Beads
  tasks.
- Prefer the simplest complete fix in every suggestion; reject
  speculative abstractions during verification.
- Spec drift and DRY-at-third-use are the two highest-signal domains for
  this codebase — weight them.
- Never guess at ambiguous scope or priority — ask (Phase 0 / triage).
- Keep the shared dev stack up; nothing in this review requires
  restarting it.
