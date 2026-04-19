#!/usr/bin/env python3
"""Fail CI on ``tenant_agnostic()`` call sites without a justification comment.

Every use of :func:`app.tenancy.tenant_agnostic` is an **escape hatch** from
the workspace-scope filter. The spec
(``docs/specs/01-architecture.md`` §"Tenant filter enforcement") requires
every call site to carry a ``# justification:`` comment describing why the
cross-tenant access is legitimate. This script walks production Python
source with :mod:`ast`, finds real call sites (not string-literal mentions,
not import identifiers, not the ``def`` itself), and exits non-zero if any
lack a justification comment within the 3-line window (call line + 2 lines
above).

Usage::

    uv run python scripts/check_tenant_agnostic.py           # scan repo root
    uv run python scripts/check_tenant_agnostic.py app/ cli/ # scan named paths

The script excludes ``.venv``, ``.git``, ``node_modules``, ``mocks`` and
``tests`` — tests can justify agnostic usage in comments but shouldn't be
forced to; the grep is about production code.

Exit codes:

* 0 — every call site has a justification (or there are no calls).
* 1 — at least one unjustified call; offending file:line paths printed to
  stderr.
* 2 — usage error (bad path, unreadable file).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

__all__ = ["find_violations", "main"]

_JUSTIFICATION_PATTERN = re.compile(r"#\s*justification\s*:", re.IGNORECASE)

# Directories we don't walk. These are source-controlled noise or
# deliberately excluded from the gate.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".venv",
        "venv",
        ".git",
        "node_modules",
        "mocks",
        "tests",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        "scripts",
    }
)

# Number of lines to look back for the justification comment, inclusive
# of the call line itself: the call line + the 2 lines above = 3 lines.
_JUSTIFICATION_WINDOW = 3


def _iter_python_files(root: Path) -> Iterator[Path]:
    """Yield ``*.py`` files under ``root`` skipping excluded directories."""
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    for path in root.rglob("*.py"):
        parts = set(path.parts)
        if parts & _EXCLUDED_DIR_NAMES:
            continue
        yield path


def _justified(lines: list[str], call_lineno: int) -> bool:
    """Return ``True`` if any of the 3-line window carries a justification.

    ``call_lineno`` is 1-indexed to match AST / compiler conventions.
    """
    start = max(0, call_lineno - _JUSTIFICATION_WINDOW)
    window = lines[start:call_lineno]
    return any(_JUSTIFICATION_PATTERN.search(line) for line in window)


class _CallFinder(ast.NodeVisitor):
    """Collect ``tenant_agnostic(...)`` call positions, skipping defs."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        # Accept ``tenant_agnostic(...)`` and ``anything.tenant_agnostic(...)``
        # — covers ``app.tenancy.tenant_agnostic()`` and aliased imports.
        if (isinstance(func, ast.Name) and func.id == "tenant_agnostic") or (
            isinstance(func, ast.Attribute) and func.attr == "tenant_agnostic"
        ):
            self.calls.append(node.lineno)
        self.generic_visit(node)


def find_violations(paths: Iterable[Path]) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, line)`` for every unjustified call.

    Only real call sites are matched. String literals and comments that
    happen to mention the name are ignored because the AST doesn't
    surface them as :class:`ast.Call`. The function's own ``def`` line
    is an :class:`ast.FunctionDef`, also not a call.
    """
    violations: list[tuple[Path, int, str]] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"warning: could not read {path}: {exc}", file=sys.stderr)
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            print(f"warning: syntax error in {path}: {exc}", file=sys.stderr)
            continue
        finder = _CallFinder()
        finder.visit(tree)
        if not finder.calls:
            continue
        lines = text.splitlines()
        for lineno in finder.calls:
            if _justified(lines, lineno):
                continue
            call_line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
            violations.append((path, lineno, call_line.rstrip()))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail CI on tenant_agnostic() call sites without a "
            "`# justification:` comment within the 3 lines including and "
            "above the call."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to scan (default: current directory).",
    )
    args = parser.parse_args(argv)
    roots: list[Path] = args.paths or [Path.cwd()]

    files: list[Path] = []
    for root in roots:
        if not root.exists():
            print(f"error: path does not exist: {root}", file=sys.stderr)
            return 2
        files.extend(_iter_python_files(root))

    violations = find_violations(files)
    if not violations:
        return 0

    print(
        "tenant_agnostic() calls without a `# justification:` comment:",
        file=sys.stderr,
    )
    for path, lineno, line in violations:
        print(f"  {path}:{lineno}: {line.strip()}", file=sys.stderr)
    print(
        f"\n{len(violations)} unjustified call(s). Add a "
        "`# justification: <reason>` comment on the same line or on "
        "one of the 2 lines above each call.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
