# crewday CLI

The `crewday` command is a thin Click-based client over the crew.day
REST API. Everything a user can do in the web UI is also a CLI verb —
the command tree is generated from the API's OpenAPI schema at build
time (see `cli/crewday/_surface.json`).

See [`docs/specs/13-cli.md`](../docs/specs/13-cli.md) for the full
spec: command tree, global flags, profile config, exit codes, output
formats, streaming / piping conventions, and agent UX rules. Entry
point is `crewday._main.main`, wired into `[project.scripts]` in the
top-level `pyproject.toml`; internal modules use a leading
underscore so they never collide with generated command names.

## Regenerating the command surface

`_surface.json` (workspace / bare-host verbs) and `_surface_admin.json`
(`/admin/api/v1/*` verbs) are committed JSON files produced by
`cli/crewday/_codegen.py`. The pipeline imports the FastAPI app,
walks `app.openapi()`, applies the exclusions in
[`_exclusions.yaml`](./crewday/_exclusions.yaml), and serialises the
result with stable key ordering.

```bash
# Rewrite both surface files from the live OpenAPI schema.
uv run python -m crewday._codegen

# CI parity gate — exit 1 if committed != fresh, with a unified diff.
uv run python -m crewday._codegen --check

# Preview what would be written to stdout without touching disk.
uv run python -m crewday._codegen --dry-run
```

Run `--check` in CI alongside the OpenAPI parity gate. Any drift is a
blocker — either re-run the write mode and commit the updated JSON, or
add a justified entry to `_exclusions.yaml` (every entry requires a
`reason:` field; the loader rejects unjustified exclusions).
