#!/usr/bin/env python3
"""Fail CI when a built HTML file carries an un-nonced inline <script>/<style>.

The production CSP (``docs/specs/15-security-privacy.md`` §"HTTP security
headers") allows inline ``<script>`` and ``<style>`` blocks only when the
tag carries the per-request nonce the server stamped into the
``Content-Security-Policy`` header. Any other inline is a CSP violation
that bricks the SPA in the browser.

This script walks the built SPA under ``app/web/dist/`` (or a path
supplied on the command line) and rejects any ``<script>`` or ``<style>``
tag that is both:

* inline (no ``src=`` / no ``href=`` — external references are out of
  scope; their integrity is covered by SRI, §15 "Subresource
  integrity"); and
* un-nonced (no ``nonce="..."`` attribute).

Usage::

    uv run python scripts/check_csp_inlines.py
    uv run python scripts/check_csp_inlines.py app/web/dist
    uv run python scripts/check_csp_inlines.py some_fixture.html

Exit codes:

* ``0`` — every inline is nonced (or there are no inlines at all).
* ``1`` — at least one un-nonced inline; offending file:line paths
  printed to stderr.
* ``2`` — usage error (no files found, path unreadable, HTML that
  cannot be parsed).

The parser is the stdlib :class:`html.parser.HTMLParser` — no third-
party dep — so this script can run inside the container image without
pulling ``beautifulsoup4`` in alongside the test matrix.
"""

from __future__ import annotations

import argparse
import html.parser
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

__all__ = ["Finding", "find_violations", "main"]


# Tags we care about. HTMLParser normalises tag names to lower case,
# so we match case-insensitively by lowering ourselves too.
_INLINE_TAGS: frozenset[str] = frozenset({"script", "style"})


# Default search root when the caller supplies no argument. Matches the
# prod SPA build directory (cd-q1be retired the mocks/web/dist
# fallback, so we point at the only canonical location).
_DEFAULT_SPA_DIST = Path("app/web/dist")


class Finding:
    """One un-nonced inline tag occurrence.

    ``line`` is 1-indexed (matches the convention used by every
    editor and by :class:`html.parser.HTMLParser.getpos`).
    """

    __slots__ = ("line", "path", "tag")

    def __init__(self, path: Path, tag: str, line: int) -> None:
        self.path = path
        self.tag = tag
        self.line = line

    def __repr__(self) -> str:
        return f"{self.path}:{self.line}: un-nonced inline <{self.tag}>"


class _InlineFinder(html.parser.HTMLParser):
    """Collect :class:`Finding` rows while parsing a single HTML string.

    We rely on :meth:`HTMLParser.handle_starttag` to see each open tag
    with its attribute list. A tag is flagged when it is a
    ``<script>`` or ``<style>`` **and** has no ``src=``/``href=``
    attribute **and** has no ``nonce=`` attribute. External refs
    (``src=...``) are intentionally ignored — this script's contract
    is "un-nonced INLINE", not "any external fetch without SRI"; SRI
    is a separate gate (§15 "Subresource integrity").
    """

    def __init__(self, path: Path) -> None:
        super().__init__(convert_charrefs=True)
        self._path = path
        self.findings: list[Finding] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name not in _INLINE_TAGS:
            return

        attr_names = {k.lower() for k, _ in attrs}
        # An external reference is out of scope for this gate.
        # ``<script src="...">`` → SRI check; ``<link rel="stylesheet"
        # href="...">`` → same. Inline is what the gate is about.
        if "src" in attr_names or "href" in attr_names:
            return

        if "nonce" in attr_names:
            return

        line_no, _col = self.getpos()
        self.findings.append(Finding(self._path, name, line_no))


def _iter_html_files(path: Path) -> Iterator[Path]:
    """Yield every ``.html`` file under ``path`` (or ``path`` itself).

    A single file argument is surfaced as-is so an operator can point
    the script at a specific fixture. Directories are walked
    recursively; symlinks are followed at default ``rglob`` behaviour,
    which in a build directory is fine — the build writes real files.
    """
    if path.is_file():
        yield path
        return

    if path.is_dir():
        yield from path.rglob("*.html")
        return

    raise FileNotFoundError(f"{path}: not a file or directory")


def find_violations(paths: Iterable[Path]) -> list[Finding]:
    """Scan ``paths`` and return every un-nonced inline ``<script>``/``<style>``.

    Each path may point to a file or a directory; directories are
    walked recursively for ``*.html``. The return shape is a flat
    list so callers can ``len()``-check or group as they prefer.
    """
    findings: list[Finding] = []
    for root in paths:
        for html_path in _iter_html_files(root):
            finder = _InlineFinder(html_path)
            finder.feed(html_path.read_text(encoding="utf-8"))
            findings.extend(finder.findings)
    return findings


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail CI when a built HTML file carries an un-nonced inline "
            "<script> or <style>. See docs/specs/15-security-privacy.md "
            '"HTTP security headers".'
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=(
            "Files or directories to scan. Defaults to app/web/dist/ "
            "(the prod SPA build)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns the process exit code.

    Kept pure so tests can call it directly with a crafted
    ``argv`` and inspect the return value instead of wrapping
    :func:`sys.exit`.
    """
    args = _parse_args(argv)
    paths: list[Path] = list(args.paths) or [_DEFAULT_SPA_DIST]

    # Silently tolerate a missing default path — a fresh checkout
    # without a built SPA must not fail the CI gate. If the caller
    # named a path explicitly and it's missing, that IS an error.
    missing = [p for p in paths if not p.exists()]
    if missing and args.paths:
        for p in missing:
            print(f"{p}: not a file or directory", file=sys.stderr)
        return 2
    paths = [p for p in paths if p.exists()]

    try:
        findings = find_violations(paths)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if findings:
        for f in findings:
            print(repr(f), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
