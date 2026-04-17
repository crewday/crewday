# Playwright MCP Screenshots

This directory holds screenshots captured by agents using the Playwright
MCP tools (`mcp__playwright__browser_take_screenshot` and friends) while
verifying UI changes or filing bugs.

The directory itself is gitignored — only this README is committed — so
agents can drop arbitrary `.png` / `.jpeg` files here without polluting
the repo root or the diff.

## Why it exists

Without this convention, screenshots tend to land at the worktree root
and either get accidentally committed or have to be cleaned up by hand.
Mirroring the pattern from the sibling `fj2` repo, all Playwright output
goes here instead.

## How to use it

When you take a screenshot, always pass an explicit `filename` under
`.playwright-mcp/`:

```python
mcp__playwright__browser_take_screenshot(
    filename=".playwright-mcp/shift-timeline-overflow.png",
)
```

Close the browser when you are done verifying:

```python
mcp__playwright__browser_close()
```

## Naming convention

- `{page}-{viewport}.png` — general page screenshots
  (`payroll-summary-desktop.png`, `task-card-mobile.png`).
- `bug-{description}.png` — bug documentation referenced from Beads
  tasks (`bug-shift-timeline-clipped-mobile.png`).
- `i18n-{page}-{issue}.png` — translation / locale issues.
- `style-{component}-{issue}.png` — styling / layout issues.

Agents may include the Beads task id as a prefix when the screenshot
is attached to one (`mp-1234-shift-timeline-mobile.png`).

## Cleanup

Stale screenshots from prior sessions can be wiped before a new review:

```bash
rm -f .playwright-mcp/*.png .playwright-mcp/*.jpeg
```

The README itself is gitignored back in via `!.playwright-mcp/README.md`,
so it survives the cleanup.
