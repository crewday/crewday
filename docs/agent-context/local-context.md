# Local Agent Context

This project keeps public agent instructions generic. Maintainer-specific
details live outside the open-source repository and are loaded only when
present on a developer machine.

## Public Contract

Committed files such as `AGENTS.md`, `SETUP.md`, and compose files should be
safe for open-source contributors. They may describe extension points and
portable defaults, but they should not require private hostnames, personal seed
accounts, VPN routes, or maintainer-only auth systems.

## Optional Private Context

Agents should read these gitignored files after the public instructions when
they exist:

```text
.agents/local/AGENTS.local.md
.agents/local/SETUP.local.md
docker-compose.override.yml
```

The recommended layout is a private companion repository with one directory per
public project:

```text
/home/ubuntu/git/dev-context/<project>/
```

Then link a project into the public worktree:

```bash
ln -s /home/ubuntu/git/dev-context/crewday .agents/local
```

Harnesses that support `@` file expansion can inline
`.agents/local/AGENTS.local.md` from `AGENTS.md`. Harnesses without that feature
should treat the include as a plain pointer and read the file manually.

Local context may add private facts such as maintainer hostnames, loopback
translations, compose override values, seed account notes, and shared-dev-box
port reservations. It must not weaken repository quality gates or redefine
public product behavior.
