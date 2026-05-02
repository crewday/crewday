"""Regenerate ``app/abuse/data/disposable_domains.txt`` from upstream.

Spec §15 "Self-serve abuse mitigations" — *"The bundled list MUST be
refreshed weekly by a CI job ``refresh-disposable-domains.yml`` that
pulls from the pinned upstream dataset, regenerates the in-repo file
… and opens a PR."*

This is the regeneration step that runs inside that CI job (see
``.github/workflows/refresh-disposable-domains.yml``). The workflow
downloads ``disposable_email_blocklist.conf`` from a pinned upstream
SHA and hands the path to this script; the script merges the new
domain set with the existing in-repo header so the explanatory
comments stay put and only the ``# generated YYYY-MM-DD`` pin bumps.

File shape (preserved):

    line 1     : "# generated YYYY-MM-DD"   (machine-read freshness pin)
    lines 2..N : explanatory comments / blank lines (kept verbatim)
    lines N+1+ : one canonical lowercase domain per line, sorted unique

Idempotent: if the upstream domain set matches the in-repo set AND
the in-repo file is comfortably within the 30-day freshness budget
(see ``_FRESHNESS_REWRITE_DAYS``), the file is left untouched and the
script exits 0 silently. The CI job then sees a clean ``git diff`` and
skips the PR step. Once the pin is approaching the staleness gate the
helper rewrites the file with today's date so the freshness unit test
(``tests/unit/abuse/test_disposable_domains.py``) keeps passing even
during long quiet stretches upstream — otherwise a 5-week-quiet
upstream would silently age the file out of the gate's 30-day budget
without ever opening a PR.

Exit codes:

    0  Success — either the file was rewritten with a fresh pin, or
       the upstream set matched and the in-repo file is still well
       within the freshness budget so nothing was written.
    2  ``--upstream`` or ``--target`` path is missing / not a file.
    3  Upstream feed parsed to zero domains (refusing to truncate the
       in-repo blocklist on a corrupt or empty download).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_PATH = REPO_ROOT / "app" / "abuse" / "data" / "disposable_domains.txt"

# Staleness gate (unit test) is 30 days. Force a rewrite — even when
# upstream is unchanged — once the in-repo pin is at least this many
# days old, so the freshness CI gate never trips during quiet upstream
# stretches. Set comfortably below 30 so a single missed weekly run
# (e.g. GitHub Actions outage) still leaves headroom.
_FRESHNESS_REWRITE_DAYS = 21

_PIN_RE = re.compile(r"^# generated (\d{4}-\d{2}-\d{2})\s*$")


def _is_header_line(line: str) -> bool:
    """Return True for lines that belong to the leading header block.

    The header block is everything from the start of the file up to
    (but not including) the first non-comment / non-blank line — i.e.
    the first domain entry. We treat any line starting with ``#`` or
    consisting only of whitespace as part of the header.
    """
    stripped = line.strip()
    return stripped == "" or stripped.startswith("#")


def _split_header_and_domains(text: str) -> tuple[list[str], set[str]]:
    """Split an existing file into (header_lines, domain_set).

    The first line is dropped from ``header_lines`` because the caller
    will rewrite it with the freshly-generated date pin. The remaining
    header lines are returned verbatim so explanatory comments and
    blank spacing are preserved across refreshes.
    """
    lines = text.splitlines()
    header: list[str] = []
    domains: set[str] = set()
    saw_first_domain = False
    for idx, raw in enumerate(lines):
        if not saw_first_domain and _is_header_line(raw):
            if idx == 0:
                # Drop the existing pin; the caller writes a fresh one.
                continue
            header.append(raw)
            continue
        saw_first_domain = True
        cleaned = raw.strip().lower()
        if cleaned:
            domains.add(cleaned)
    return header, domains


def _parse_upstream_domains(text: str) -> set[str]:
    """Return the unique lowercase domain set from an upstream feed.

    Upstream's ``disposable_email_blocklist.conf`` is one domain per
    line, sorted, with no comments. We still strip whitespace, drop
    blanks, drop ``#``-prefixed lines, and lowercase defensively in
    case the file shape ever picks up a header.
    """
    domains: set[str] = set()
    for raw in text.splitlines():
        cleaned = raw.strip().lower()
        if not cleaned or cleaned.startswith("#"):
            continue
        domains.add(cleaned)
    return domains


def _render(header: list[str], domains: set[str], today: str) -> str:
    """Render the canonical on-disk shape of the blocklist file."""
    pin = f"# generated {today}"
    body_lines = [pin, *header, *sorted(domains)]
    return "\n".join(body_lines) + "\n"


def _existing_pin_age_days(text: str, now: datetime) -> int | None:
    """Return how many days old the in-repo pin is, or ``None`` if absent.

    A negative result (future-dated pin) is returned as-is; the caller
    treats "any non-None value < the freshness budget" as fresh.
    """
    first_line = text.splitlines()[0] if text else ""
    match = _PIN_RE.match(first_line)
    if match is None:
        return None
    pinned = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=UTC)
    return (now - pinned).days


def _format_target(target: Path) -> str:
    """Format ``target`` for log output — repo-relative when possible."""
    try:
        return os.fspath(target.relative_to(REPO_ROOT))
    except ValueError:
        return os.fspath(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate app/abuse/data/disposable_domains.txt from an "
            "upstream disposable_email_blocklist.conf, preserving the "
            "in-repo header and bumping the freshness pin."
        )
    )
    parser.add_argument(
        "upstream",
        type=Path,
        help="Path to a freshly-downloaded disposable_email_blocklist.conf.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=TARGET_PATH,
        help="Path to the in-repo blocklist file (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    if not args.upstream.is_file():
        sys.stderr.write(f"upstream file not found: {args.upstream}\n")
        return 2
    if not args.target.is_file():
        sys.stderr.write(f"target file not found: {args.target}\n")
        return 2

    existing_text = args.target.read_text(encoding="utf-8")
    header, existing_domains = _split_header_and_domains(existing_text)
    upstream_domains = _parse_upstream_domains(
        args.upstream.read_text(encoding="utf-8")
    )

    if not upstream_domains:
        sys.stderr.write(
            "upstream feed parsed to zero domains; refusing to truncate "
            f"{_format_target(args.target)}\n"
        )
        return 3

    now = datetime.now(UTC)
    pin_age = _existing_pin_age_days(existing_text, now)
    nearing_staleness = pin_age is None or pin_age >= _FRESHNESS_REWRITE_DAYS

    if upstream_domains == existing_domains and not nearing_staleness:
        # Nothing to do — domain set unchanged AND the freshness pin is
        # still comfortably within budget. CI sees a clean diff and
        # skips the PR step.
        return 0

    today = now.strftime("%Y-%m-%d")
    fresh = _render(header, upstream_domains, today)
    args.target.write_text(fresh, encoding="utf-8")
    sys.stderr.write(
        f"wrote {_format_target(args.target)} "
        f"({len(upstream_domains)} domains, pin {today})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
