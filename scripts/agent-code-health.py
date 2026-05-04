#!/usr/bin/env -S uv run python
"""Code health digest backed by lizard.

Surfaces the worst per-function offenders by cyclomatic complexity,
function length, and parameter count, then lists duplicate code blocks
(token-based clone detection). Mirrors the "concise digest, file:line
refs" style of ``scripts/agent-status.sh`` so an agent can read the
output and immediately know which functions to refactor next.

Defaults scan the production tree (``app/`` Python + ``app/web/src/``
TypeScript). Pass paths explicitly to widen or narrow the scan.

Usage:
  scripts/agent-code-health.py                 # default scan
  scripts/agent-code-health.py app/domain      # specific subtree
  scripts/agent-code-health.py --no-dup        # skip duplicate scan (faster)
  scripts/agent-code-health.py --top 20        # show top 20 of each list
  scripts/agent-code-health.py --csv-out h.csv # also dump full per-function CSV
  scripts/agent-code-health.py --json-out h.json

Suppressions:
  Add ``code-health: ignore[ccn] reason`` inside the target function, or
  ``code-health: ignore[duplicate] reason`` inside a duplicated block.
  The reason is required. Suppressed findings are omitted from the
  concise digest and retained in JSON output with their reason.

Thresholds (override via env):
  CCN_THRESHOLD=15      cyclomatic complexity warning
  NLOC_THRESHOLD=60     non-comment lines per function
  PARAM_THRESHOLD=6     positional + keyword parameters

Exit code is 0 when lizard ran successfully, regardless of how many
warnings were found — this is a digest, not a gate. Wire it into
``agent-quality.sh`` later if the team wants a hard threshold.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_PATHS = ["app", "app/web/src"]
DEFAULT_TOP = 10

# Vendored or generated trees that always pollute results. Lizard's -x
# expects glob-style patterns; "*/node_modules/*" matches at any depth.
DEFAULT_EXCLUDES = [
    "*/node_modules/*",
    "*/dist/*",
    "*/.vite/*",
    "*/__pycache__/*",
    "*/.venv/*",
    "*/migrations/*",
]

SUPPRESSION_RE = re.compile(
    r"code-health:\s*ignore\[(?P<categories>[a-zA-Z0-9_, -]+)\]\s+(?P<reason>\S.*)"
)
DUP_LOC_RE = re.compile(
    r"^(?P<file>.+):(?P<start>\d+)\s*~\s*(?P<end>\d+)(?P<suffix>.*)$"
)


@dataclass(frozen=True)
class Func:
    nloc: int
    ccn: int
    tokens: int
    params: int
    length: int
    file: str
    name: str
    start: int
    end: int

    @property
    def loc(self) -> str:
        return f"{self.file}:{self.start}"


@dataclass(frozen=True)
class DuplicateLocation:
    file: str
    start: int
    end: int
    raw: str


@dataclass(frozen=True)
class Suppression:
    category: str
    file: str
    line: int
    reason: str

    @property
    def target(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(frozen=True)
class InvalidSuppression:
    file: str
    line: int
    text: str
    problem: str

    @property
    def target(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(frozen=True)
class Finding:
    category: str
    metric: str
    value: int
    threshold: int | None
    target: str
    file: str
    line: int
    symbol: str
    suppressed: bool
    suppression_reason: str | None = None
    suppression_target: str | None = None
    locations: list[dict[str, int | str]] | None = None


def run_lizard(paths: list[str], extra: list[str]) -> tuple[str, int]:
    excludes: list[str] = []
    for pattern in DEFAULT_EXCLUDES:
        excludes.extend(["-x", pattern])
    cmd = ["uv", "run", "lizard", *excludes, *extra, *paths]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.stdout, proc.returncode


def parse_csv(out: str) -> list[Func]:
    funcs: list[Func] = []
    # Columns: nloc, ccn, tokens, params, length, location, file, name,
    # longname, start, end. No header row in lizard --csv output.
    for row in csv.reader(out.splitlines()):
        if len(row) < 11:
            continue
        try:
            funcs.append(
                Func(
                    nloc=int(row[0]),
                    ccn=int(row[1]),
                    tokens=int(row[2]),
                    params=int(row[3]),
                    length=int(row[4]),
                    file=row[6],
                    name=row[7],
                    start=int(row[9]),
                    end=int(row[10]),
                )
            )
        except ValueError, IndexError:
            continue
    return funcs


def parse_dup(out: str) -> tuple[list[list[str]], str]:
    blocks: list[list[str]] = []
    current: list[str] = []
    in_block = False
    dup_rate = ""
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Duplicate block:"):
            if current:
                blocks.append(current)
            current = []
            in_block = True
        elif stripped.startswith("---") or stripped.startswith("^^^"):
            continue
        elif in_block and ":" in stripped and "~" in stripped:
            current.append(stripped)
        elif "Total duplicate rate" in stripped:
            dup_rate = stripped.split(":", 1)[1].strip()
            in_block = False
    if current:
        blocks.append(current)
    return blocks, dup_rate


def parse_duplicate_location(raw: str) -> DuplicateLocation | None:
    match = DUP_LOC_RE.match(raw)
    if not match:
        return None
    return DuplicateLocation(
        file=match.group("file"),
        start=int(match.group("start")),
        end=int(match.group("end")),
        raw=raw,
    )


def discover_suppressions(
    files: set[str],
) -> tuple[dict[str, list[Suppression]], list[InvalidSuppression]]:
    suppressions: dict[str, list[Suppression]] = {}
    invalid: list[InvalidSuppression] = []
    for file in sorted(files):
        path = Path(file)
        if not path.exists() or not path.is_file():
            continue
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        for idx, line in enumerate(lines, start=1):
            if "code-health:" not in line:
                continue
            match = SUPPRESSION_RE.search(line)
            if not match:
                invalid.append(
                    InvalidSuppression(
                        file=file,
                        line=idx,
                        text=line.strip(),
                        problem="expected code-health: ignore[category] reason",
                    )
                )
                continue
            categories = [
                c.strip().lower()
                for c in re.split(r"[,\s]+", match.group("categories"))
                if c.strip()
            ]
            reason = match.group("reason").strip()
            if not categories or not reason:
                invalid.append(
                    InvalidSuppression(
                        file=file,
                        line=idx,
                        text=line.strip(),
                        problem="suppression category and reason are required",
                    )
                )
                continue
            for category in categories:
                suppressions.setdefault(file, []).append(
                    Suppression(
                        category=category,
                        file=file,
                        line=idx,
                        reason=reason,
                    )
                )
    return suppressions, invalid


def matching_suppression(
    suppressions: dict[str, list[Suppression]],
    *,
    category: str,
    file: str,
    start: int,
    end: int,
) -> Suppression | None:
    for suppression in suppressions.get(file, []):
        if suppression.category not in {category, "all"}:
            continue
        if start <= suppression.line <= end:
            return suppression
    return None


def build_metric_finding(
    func: Func,
    *,
    category: str,
    value: int,
    threshold: int,
    suppressions: dict[str, list[Suppression]],
) -> Finding:
    suppression = matching_suppression(
        suppressions,
        category=category,
        file=func.file,
        start=func.start,
        end=func.end,
    )
    return Finding(
        category=category,
        metric=category,
        value=value,
        threshold=threshold,
        target=f"{func.file}:{func.start}:{func.name}",
        file=func.file,
        line=func.start,
        symbol=func.name,
        suppressed=suppression is not None,
        suppression_reason=suppression.reason if suppression else None,
        suppression_target=suppression.target if suppression else None,
    )


def build_metric_findings(
    funcs: list[Func],
    *,
    category: str,
    key: Callable[[Func], int],
    threshold: int,
    suppressions: dict[str, list[Suppression]],
) -> list[Finding]:
    return [
        build_metric_finding(
            func,
            category=category,
            value=key(func),
            threshold=threshold,
            suppressions=suppressions,
        )
        for func in funcs
        if key(func) > threshold
    ]


def build_duplicate_finding(
    block: list[str],
    suppressions: dict[str, list[Suppression]],
) -> Finding:
    parsed = [loc for raw in block if (loc := parse_duplicate_location(raw))]
    first = parsed[0] if parsed else DuplicateLocation("", 0, 0, block[0])
    suppression = next(
        (
            found
            for loc in parsed
            if (
                found := matching_suppression(
                    suppressions,
                    category="duplicate",
                    file=loc.file,
                    start=loc.start,
                    end=loc.end,
                )
            )
        ),
        None,
    )
    return Finding(
        category="duplicate",
        metric="clone_count",
        value=len(block),
        threshold=None,
        target=f"duplicate:{first.raw}",
        file=first.file,
        line=first.start,
        symbol="duplicate block",
        suppressed=suppression is not None,
        suppression_reason=suppression.reason if suppression else None,
        suppression_target=suppression.target if suppression else None,
        locations=[
            {
                "file": loc.file,
                "start": loc.start,
                "end": loc.end,
                "raw": loc.raw,
            }
            for loc in parsed
        ]
        or [{"raw": raw} for raw in block],
    )


def count_by_suppression(findings: list[Finding]) -> dict[str, int]:
    return {
        "total": len(findings),
        "unsuppressed": sum(1 for finding in findings if not finding.suppressed),
        "suppressed": sum(1 for finding in findings if finding.suppressed),
    }


def build_report(
    *,
    paths: list[str],
    funcs: list[Func],
    metric_findings: dict[str, list[Finding]],
    duplicate_findings: list[Finding],
    thresholds: dict[str, int],
    suppressions: dict[str, list[Suppression]],
    invalid_suppressions: list[InvalidSuppression],
    duplicate_rates: list[str],
) -> dict[str, object]:
    findings = [
        *metric_findings["ccn"],
        *metric_findings["nloc"],
        *metric_findings["params"],
        *duplicate_findings,
    ]
    summary: dict[str, object] = {
        "functions_analyzed": len(funcs),
        "thresholds": thresholds,
        "ccn": count_by_suppression(metric_findings["ccn"]),
        "nloc": count_by_suppression(metric_findings["nloc"]),
        "params": count_by_suppression(metric_findings["params"]),
        "duplicate": count_by_suppression(duplicate_findings),
        "duplicate_blocks": count_by_suppression(duplicate_findings),
        "invalid_suppressions": len(invalid_suppressions),
    }
    return {
        "schema_version": 1,
        "paths": paths,
        "summary": summary,
        "findings": [asdict(finding) for finding in findings],
        "duplicate_blocks": [asdict(finding) for finding in duplicate_findings],
        "suppressions": [
            asdict(suppression)
            for by_file in suppressions.values()
            for suppression in by_file
        ],
        "invalid_suppressions": [
            asdict(suppression) for suppression in invalid_suppressions
        ],
        "duplicate_rates": duplicate_rates,
    }


def rank_section(
    title: str,
    findings: list[Finding],
    threshold: int,
    top: int,
) -> int:
    over = sorted(
        (finding for finding in findings if not finding.suppressed),
        key=lambda finding: finding.value,
        reverse=True,
    )
    suppressed = sum(1 for finding in findings if finding.suppressed)
    suffix = f", {suppressed} suppressed" if suppressed else ""
    print(f"\n=== {title} (over {threshold}, {len(over)} funcs{suffix}) ===")
    if not over:
        print("  none")
        return 0
    for finding in over[:top]:
        loc = f"{finding.file}:{finding.line}"
        print(f"  {finding.value:>4}  {loc:<55}  {finding.symbol}")
    if len(over) > top:
        print(f"  ... +{len(over) - top} more")
    return len(over)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paths", nargs="*", default=DEFAULT_PATHS)
    ap.add_argument("--top", type=int, default=DEFAULT_TOP)
    ap.add_argument("--no-dup", action="store_true", help="skip duplicate-block scan")
    ap.add_argument("--csv-out", help="dump full per-function CSV to this path")
    ap.add_argument("--json-out", help="dump full finding report JSON to this path")
    args = ap.parse_args()

    ccn_t = int(os.environ.get("CCN_THRESHOLD", "15"))
    nloc_t = int(os.environ.get("NLOC_THRESHOLD", "60"))
    param_t = int(os.environ.get("PARAM_THRESHOLD", "6"))
    thresholds = {"ccn": ccn_t, "nloc": nloc_t, "params": param_t}

    print(f"lizard scan: {' '.join(args.paths)}")
    csv_out, rc = run_lizard(args.paths, ["--csv"])
    if rc != 0 and not csv_out.strip():
        print("lizard failed — is it installed? (uv sync)", file=sys.stderr)
        return rc or 1
    funcs = parse_csv(csv_out)
    if not funcs:
        print("no functions parsed from lizard output", file=sys.stderr)
        return 1
    print(f"{len(funcs)} functions analyzed")

    if args.csv_out:
        Path(args.csv_out).write_text(csv_out, encoding="utf-8")
        print(f"wrote per-function CSV → {args.csv_out}")

    suppression_files = {func.file for func in funcs}
    all_blocks: list[list[str]] = []
    rates: list[str] = []
    if not args.no_dup:
        # Split by language: lizard 1.22.1's duplicate extension crashes
        # with IndexError on TypeScript token streams. Running per
        # language keeps the Python pass usable even if TS blows up.
        for langs, label in [(["python"], "py"), (["typescript", "tsx"], "ts")]:
            lang_args = [a for lang in langs for a in ("-l", lang)]
            dup_out, dup_rc = run_lizard(args.paths, [*lang_args, "-Eduplicate"])
            if dup_rc != 0 and not dup_out.strip():
                print(
                    f"  [{label}] duplicate scan crashed (lizard upstream bug)",
                    file=sys.stderr,
                )
                continue
            blocks, dup_rate = parse_dup(dup_out)
            all_blocks.extend(blocks)
            if dup_rate:
                rates.append(f"{label}={dup_rate}")
        for block in all_blocks:
            for raw in block:
                if loc := parse_duplicate_location(raw):
                    suppression_files.add(loc.file)

    suppressions, invalid_suppressions = discover_suppressions(suppression_files)
    if invalid_suppressions:
        print("\ninvalid code-health suppressions:", file=sys.stderr)
        for suppression in invalid_suppressions:
            print(
                f"  {suppression.target}: {suppression.problem}",
                file=sys.stderr,
            )

    metric_findings = {
        "ccn": build_metric_findings(
            funcs,
            category="ccn",
            key=lambda f: f.ccn,
            threshold=ccn_t,
            suppressions=suppressions,
        ),
        "nloc": build_metric_findings(
            funcs,
            category="nloc",
            key=lambda f: f.nloc,
            threshold=nloc_t,
            suppressions=suppressions,
        ),
        "params": build_metric_findings(
            funcs,
            category="params",
            key=lambda f: f.params,
            threshold=param_t,
            suppressions=suppressions,
        ),
    }
    duplicate_findings = [
        build_duplicate_finding(block, suppressions) for block in all_blocks
    ]

    report = build_report(
        paths=args.paths,
        funcs=funcs,
        metric_findings=metric_findings,
        duplicate_findings=duplicate_findings,
        thresholds=thresholds,
        suppressions=suppressions,
        invalid_suppressions=invalid_suppressions,
        duplicate_rates=rates,
    )
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"wrote full JSON report → {args.json_out}")

    n_ccn = rank_section(
        "cyclomatic complexity", metric_findings["ccn"], ccn_t, args.top
    )
    n_nloc = rank_section(
        "function length (nloc)", metric_findings["nloc"], nloc_t, args.top
    )
    n_param = rank_section(
        "parameter count", metric_findings["params"], param_t, args.top
    )

    n_dup = len([finding for finding in duplicate_findings if not finding.suppressed])
    if not args.no_dup:
        n_suppressed_dup = len(
            [finding for finding in duplicate_findings if finding.suppressed]
        )
        print(f"\n=== duplicate blocks ({n_dup}) ===")
        if n_suppressed_dup:
            print(f"  suppressed: {n_suppressed_dup}")
        print(f"  rates: {' '.join(rates) if rates else 'n/a'}")
        # Rank by clone count — more clones = bigger refactor leverage.
        unsuppressed_dups = sorted(
            (finding for finding in duplicate_findings if not finding.suppressed),
            key=lambda finding: -finding.value,
        )
        for finding in unsuppressed_dups[: args.top]:
            locations = finding.locations or []
            first = locations[0]["raw"] if locations else finding.target
            print(f"  clones={finding.value:>2}  first: {first}")
            for loc in locations[1:4]:
                print(f"               also: {loc['raw']}")
            if len(locations) > 4:
                print(f"               ... +{len(locations) - 4} also")
        if len(unsuppressed_dups) > args.top:
            print(f"  ... +{len(unsuppressed_dups) - args.top} more")

    print(
        f"\nsummary: ccn>{ccn_t}={n_ccn}  nloc>{nloc_t}={n_nloc}  "
        f"params>{param_t}={n_param}  duplicate-blocks={n_dup}"
    )
    return 1 if invalid_suppressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
