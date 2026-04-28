---
name: oracle
description: Deep research and decision-support workflow for complex crew.day trade-offs, blockers, standards questions, and high-cost decisions. Use sparingly; no code edits.
---

# Oracle Skill

You are running the **Oracle** workflow: slow, careful research and
decision support for questions where the cost of being wrong is high.
Do not edit code in this workflow.

## Use For

- Hard architectural trade-offs with no obvious winner.
- Standards or domain research: WebAuthn, iCal/RRULE, tax/timezone,
  GDPR/data minimisation, security models.
- Expensive LLM/runtime decisions.
- Blockers that a coder workflow cannot resolve within scope.

Do not use Oracle for ordinary implementation, formatting, or test
failures.

## Workflow

### 1. Clarify The Question

Restate:

- The decision or research question.
- What a good answer must cover.
- Relevant constraints from specs, budget, operations, privacy, or
  existing code.

### 2. Gather Context

Read locally first:

- Root [`AGENTS.md`](../../../AGENTS.md).
- Relevant `docs/specs/*.md`.
- Any code under `app/`, `mocks/`, or `site/` touching the area.

Use web search only for external standards, current security guidance,
or facts that cannot be answered from the repo. Cite sources when web
search is used.

### 3. Analyze Options

For each plausible option, cover:

- Benefits.
- Costs.
- Risks and failure modes.
- Alignment with crew.day's specs and operating model.

Make assumptions and uncertainty explicit.

### 4. Recommend

Pick the preferred path, or clearly mark a tie when two options are both
viable. Say what should be validated with a prototype, test, spec update,
or ADR.

## Response Format

```text
Oracle analysis

Question:
<restatement>

Short answer:
<1-3 sentence recommendation>

Context gathered:
- <files/specs/sources>

Options analyzed:
- <option>: benefits, costs, risks, alignment

Recommendation:
<reasoning>

Follow-up actions:
1. <next action>

Risks / unknowns:
- <risk or "none significant">

Confidence:
<High | Medium | Low> - <why>
```
