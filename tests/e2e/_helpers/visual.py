"""Pixel-level screenshot diff harness for the e2e suite (cd-ndmv).

Spec: ``docs/specs/17-testing-quality.md`` §"Visual regression" —
``/styleguide`` fails on > 0.1 % diff, every other route on > 0.5 %.

The helper takes a Playwright ``page``, captures a full-page PNG,
loads the committed baseline at ``tests/e2e/_baselines/<name>.png``,
and runs ``pixelmatch`` (the Python port of Mapbox's pixelmatch).
Three terminal states:

* **Baseline missing.** Write the new capture as the baseline and
  ``pytest.skip`` with a focused message — the next run will start
  diffing. Bake-in is intentional: a missing baseline must NOT fail
  the suite (Playwright tests are slow; a green-after-skip on the
  next CI run is the right signal).

* **Diff above threshold.** Write the diff PNG to
  ``tests/e2e/_diff/<name>.png`` and fail with the pixel count + the
  threshold. The diff PNG is a Playwright artefact; CI uploads it
  alongside trace.zip.

* **Diff within threshold.** Pass silently.

Threshold is **fraction of mismatched pixels** (so the spec's
``0.1 %`` is ``threshold=0.001``). The default 0.5 % matches the
spec's "all other routes" budget; callers override per-route.

The pixelmatch ``threshold`` argument (named the same but operating
at the per-pixel YIQ-distance level) defaults to its package default
(``0.1``) — finer per-pixel sensitivity, since the *fraction*
gate is the policy knob the spec speaks in.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Final

import pytest
from PIL import Image
from pixelmatch.contrib.PIL import pixelmatch
from playwright.sync_api import Page

__all__ = [
    "DEFAULT_DIFF_FRACTION",
    "STYLEGUIDE_DIFF_FRACTION",
    "compare_screenshot",
]


# Spec defaults from §17 "Visual regression". The non-styleguide
# default lives here so callers can drop the kwarg at the call site.
DEFAULT_DIFF_FRACTION: Final[float] = 0.005
STYLEGUIDE_DIFF_FRACTION: Final[float] = 0.001

_BASELINES_DIR: Final[Path] = Path(__file__).parent.parent / "_baselines"
_DIFF_DIR: Final[Path] = Path(__file__).parent.parent / "_diff"


def compare_screenshot(
    page: Page,
    name: str,
    *,
    threshold: float = DEFAULT_DIFF_FRACTION,
    full_page: bool = True,
    baselines_dir: Path = _BASELINES_DIR,
    diff_dir: Path = _DIFF_DIR,
) -> None:
    """Capture ``page`` and diff against ``<baselines_dir>/<name>.png``.

    Behaviour matrix:

    * Baseline missing → write capture as baseline, ``pytest.skip``.
    * ``mismatch / total`` > ``threshold`` → write diff PNG, fail.
    * Otherwise → pass silently.

    ``threshold`` is a fraction (0.0-1.0) of mismatched pixels; see
    the module docstring for the spec's per-route values.

    Diff artefacts land at ``<diff_dir>/<name>.png`` so a CI
    ``upload-artifact`` step pinned at ``tests/e2e/_diff/`` carries
    every regression's evidence without the developer hand-curating
    paths. The directory is created on demand.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        # Defensive: ``name`` becomes a filesystem path component, so
        # path traversal / hidden-file shapes are rejected upfront.
        raise ValueError(f"invalid baseline name {name!r}")

    capture_bytes = page.screenshot(full_page=full_page)
    capture_img = Image.open(BytesIO(capture_bytes)).convert("RGBA")

    baseline_path = baselines_dir / f"{name}.png"
    if not baseline_path.exists():
        baselines_dir.mkdir(parents=True, exist_ok=True)
        capture_img.save(baseline_path)
        pytest.skip(
            f"baseline missing for {name!r}; wrote capture to {baseline_path} — "
            "review the image, commit it, and rerun"
        )

    baseline_img = Image.open(baseline_path).convert("RGBA")
    if baseline_img.size != capture_img.size:
        diff_dir.mkdir(parents=True, exist_ok=True)
        oversized_path = diff_dir / f"{name}.png"
        capture_img.save(oversized_path)
        pytest.fail(
            f"baseline {name!r} size mismatch: baseline={baseline_img.size}, "
            f"capture={capture_img.size}; saved capture at {oversized_path} — "
            "viewport drifted or theme changed?"
        )

    width, height = baseline_img.size
    diff_img = Image.new("RGBA", (width, height))
    mismatch_count = pixelmatch(
        baseline_img,
        capture_img,
        diff_img,
        threshold=0.1,
        includeAA=True,
    )
    total_pixels = width * height
    mismatch_fraction = mismatch_count / total_pixels if total_pixels else 0.0

    if mismatch_fraction > threshold:
        diff_dir.mkdir(parents=True, exist_ok=True)
        diff_path = diff_dir / f"{name}.png"
        diff_img.save(diff_path)
        pct = mismatch_fraction * 100
        budget_pct = threshold * 100
        pytest.fail(
            f"visual regression {name!r}: {mismatch_count} / {total_pixels} "
            f"pixels differ ({pct:.3f}%, budget {budget_pct:.3f}%). "
            f"Diff at {diff_path}."
        )
