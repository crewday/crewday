"""Regenerate ``docs/api/openapi.json`` from the live FastAPI app.

Single canonical command so every agent / contributor / CI run produces
byte-identical output. Boots the app via
:func:`app.api.factory.create_app`, dumps ``app.openapi()`` with
``json.dumps(..., indent=2, sort_keys=True, ensure_ascii=True) + "\\n"``,
and writes the canonical on-disk shape.

Why a script (vs an ad-hoc one-liner):

* **Stable formatting.** Two agents running different one-liners
  produced slightly different files (escape style, trailing newline,
  key order). Wrapping the recipe here removes that drift.
* **Dummy root key.** The app factory refuses to boot without
  ``CREWDAY_ROOT_KEY``; the script seeds a zero-filled dummy when the
  variable is unset so the regen never accidentally reads (or writes)
  a real envelope key. Operator-set values are left alone.
* **Quiet stdout.** App boot logs are suppressed so the JSON write is
  not interleaved with structured-log lines (the failure mode that
  produced the very first noisy diffs).

Usage:

    uv run python -m scripts.regen_openapi          # write the file
    uv run python -m scripts.regen_openapi --check  # exit 1 if stale
    uv run python -m scripts.regen_openapi --stdout # print, don't write

``--check`` is the CI hook: pair with ``make openapi-check`` to fail a
PR whose ``docs/api/openapi.json`` has drifted from the live schema.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = REPO_ROOT / "docs" / "api" / "openapi.json"
_DUMMY_ROOT_KEY = "0" * 32


def render() -> str:
    """Return the canonical JSON serialisation of the live schema."""
    os.environ.setdefault("CREWDAY_ROOT_KEY", _DUMMY_ROOT_KEY)
    logging.disable(logging.CRITICAL)

    from app.api.factory import create_app

    app = create_app()
    schema: dict[str, Any] = app.openapi()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate docs/api/openapi.json deterministically."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the committed file differs from a fresh regen.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the regenerated JSON to stdout instead of writing.",
    )
    args = parser.parse_args(argv)

    fresh = render()

    if args.stdout:
        sys.stdout.write(fresh)
        return 0

    if args.check:
        committed = (
            OPENAPI_PATH.read_text(encoding="utf-8") if OPENAPI_PATH.is_file() else ""
        )
        if committed == fresh:
            return 0
        sys.stderr.write("docs/api/openapi.json is stale. Run `make openapi`.\n")
        return 1

    OPENAPI_PATH.write_text(fresh, encoding="utf-8")
    sys.stderr.write(f"wrote {OPENAPI_PATH.relative_to(REPO_ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
