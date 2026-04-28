# Claude Code configuration for crew.day

This directory holds agent-development configuration for anyone operating on
the crew.day codebase with an AI coding tool (Claude Code, Codex, Cursor,
OpenClaw, etc.).

> Authoritative rules live in the top-level [`AGENTS.md`](../AGENTS.md).
> The files in this directory are the *how* — standards, playbooks, role
> workflows, and Claude compatibility wrappers — that support those
> rules.

## Status

crew.day is **pre-implementation**. The repo currently contains specs only
(see [`docs/specs/`](../docs/specs/)). The agents and skills here are sized
for that reality: the most useful ones right now are `audit-spec`,
`selfreview`, `director`, `gap-finder`, `security-check`, and the role
workflow skills (`coder`, `commiter`, `oracle`). More app-specific
skills will land as code does.

## Directory structure

```
.claude/
├── README.md              # This file
├── agents/                # Claude compatibility wrappers
│   ├── oracle.md          # Loads skills/oracle
│   ├── coder.md           # Loads skills/coder
│   └── commiter.md        # Loads skills/commiter
├── skills/                # Reusable playbooks loaded per task
│   ├── audit-spec/        # Spec ↔ code drift audit (matches `/audit-spec` trigger)
│   ├── selfreview/        # Skeptical review of your own recent changes
│   ├── director/          # Top-level planning across specs / apps
│   ├── coder/             # Implementation workflow
│   ├── commiter/          # Beads close + commit + push workflow
│   ├── oracle/            # Deep research / hard decisions
│   ├── security-check/    # Red-team pass on a feature or spec
│   └── gap-finder/        # Pre-impl: find holes and contradictions in specs
└── commands/
    └── ai-slop.md         # Slash command: strip AI-generated noise from a branch
```

## Skills vs. agents

- **Skills** describe *how* to do something — a playbook loaded for a
  particular kind of task.
- **Agents** are Claude-specific wrappers for role execution. They load
  the matching skill and should not contain canonical workflow rules.

Most day-to-day work goes through skills. The Director may run those
skills in the current agent or delegate them to subagents when the
runtime supports it and the user has authorized delegation.

## Typical workflow

```
/director → /coder → /selfreview autofix → /commiter
```

See [`.claude/skills/director/SKILL.md`](skills/director/SKILL.md) for
the full per-task loop.

For hard problems, run `/oracle` for deep research.
