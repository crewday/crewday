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
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
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


def rank_section(
    title: str,
    funcs: list[Func],
    key: Callable[[Func], int],
    threshold: int,
    top: int,
) -> int:
    over = sorted((f for f in funcs if key(f) > threshold), key=key, reverse=True)
    print(f"\n=== {title} (over {threshold}, {len(over)} funcs) ===")
    if not over:
        print("  none")
        return 0
    for f in over[:top]:
        print(f"  {key(f):>4}  {f.loc:<55}  {f.name}")
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
    args = ap.parse_args()

    ccn_t = int(os.environ.get("CCN_THRESHOLD", "15"))
    nloc_t = int(os.environ.get("NLOC_THRESHOLD", "60"))
    param_t = int(os.environ.get("PARAM_THRESHOLD", "6"))

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
        Path(args.csv_out).write_text(csv_out)
        print(f"wrote per-function CSV → {args.csv_out}")

    n_ccn = rank_section(
        "cyclomatic complexity", funcs, lambda f: f.ccn, ccn_t, args.top
    )
    n_nloc = rank_section(
        "function length (nloc)", funcs, lambda f: f.nloc, nloc_t, args.top
    )
    n_param = rank_section(
        "parameter count", funcs, lambda f: f.params, param_t, args.top
    )

    n_dup = 0
    if not args.no_dup:
        # Split by language: lizard 1.22.1's duplicate extension crashes
        # with IndexError on TypeScript token streams. Running per
        # language keeps the Python pass usable even if TS blows up.
        all_blocks: list[list[str]] = []
        rates: list[str] = []
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
        n_dup = len(all_blocks)
        print(f"\n=== duplicate blocks ({n_dup}) ===")
        print(f"  rates: {' '.join(rates) if rates else 'n/a'}")
        # Rank by clone count — more clones = bigger refactor leverage.
        all_blocks.sort(key=lambda b: -len(b))
        for blk in all_blocks[: args.top]:
            print(f"  clones={len(blk):>2}  first: {blk[0]}")
            for loc in blk[1:]:
                print(f"               also: {loc}")
        if len(all_blocks) > args.top:
            print(f"  ... +{len(all_blocks) - args.top} more")

    print(
        f"\nsummary: ccn>{ccn_t}={n_ccn}  nloc>{nloc_t}={n_nloc}  "
        f"params>{param_t}={n_param}  duplicate-blocks={n_dup}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
