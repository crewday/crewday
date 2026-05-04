"""Pilot visual-regression test against ``/styleguide`` (cd-ndmv).

Spec: ``docs/specs/17-testing-quality.md`` §"Visual regression" —
``/styleguide`` is the public dev-surface baseline; a > 0.1 % diff
fails the check.

The pilot lands the harness end-to-end:

* Navigate to ``/styleguide`` (public — no auth needed).
* Capture a full-page PNG.
* Diff against ``tests/e2e/_baselines/styleguide.png`` via
  pixelmatch.

First run writes the baseline + skips with a focused message; second
run is the real diff. The diff threshold is the spec's 0.1 %.

Cross-browser: pinned to **Chromium** for the baseline. WebKit
renders the same CSS slightly differently (font hinting, sub-pixel
positioning), so a single baseline can't satisfy both engines.
The follow-up Beads task widens the harness to per-engine baselines
once the GA suite needs Safari coverage.

**Two tests in this file**, mirroring the sitemap walker's contract-
proof / smoke-run split:

* :func:`test_styleguide_baseline_holds_at_0_1pct` — the smoke-run.
* :func:`test_visual_harness_fires_on_deliberate_pixel_change` —
  pins the cd-ndmv acceptance criterion "screenshot diff fires on a
  deliberate pixel change". Uses a synthetic baseline + a
  deliberately-altered capture so the assertion is hermetic (no SPA
  dependency) — the moral equivalent of
  :func:`tests.e2e.test_sitemap_mobile_walk.test_walker_detects_known_horizontal_scroll_regression`
  for the visual harness.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from playwright.sync_api import Page

from tests.e2e._helpers.visual import (
    STYLEGUIDE_DIFF_FRACTION,
    compare_screenshot,
)


def test_styleguide_baseline_holds_at_0_1pct(page: Page, base_url: str) -> None:
    """The ``/styleguide`` snapshot stays within the 0.1 % budget.

    Behaviour matrix lives in :func:`compare_screenshot` — baseline
    missing skips, mismatch above threshold fails with a diff PNG,
    everything else passes. Threshold pinned at the spec's 0.1 %.
    """
    page.goto(f"{base_url.rstrip('/')}/styleguide")
    # A short stabilisation wait: web fonts swap in after first paint
    # and the design tokens load via CSS @font-face. Without this the
    # first capture often diffs against the second by a few subpixels
    # of glyph hinting.
    page.wait_for_load_state("networkidle")
    compare_screenshot(
        page,
        name="styleguide",
        threshold=STYLEGUIDE_DIFF_FRACTION,
    )


def test_visual_harness_fires_on_deliberate_pixel_change(
    page: Page, tmp_path: Path
) -> None:
    """:func:`compare_screenshot` fails when capture > threshold from baseline.

    Pins cd-ndmv acceptance criterion: "/styleguide screenshot diff
    fires on a deliberate pixel change". Hermetic — uses a synthetic
    data-URL page + a tmp-path baseline directory so neither the SPA
    nor the committed baseline is involved. The moral equivalent of
    :func:`tests.e2e.test_sitemap_mobile_walk.test_walker_detects_known_horizontal_scroll_regression`.

    Walks the harness through both terminal states:

    1. First call writes the baseline + skips (the helper's
       baseline-missing path); we catch ``pytest.skip.Exception`` to
       continue the test.
    2. Repaint the page so the next capture differs by ~25 % of pixels
       (well above the 0.1 % styleguide budget); :func:`compare_screenshot`
       must raise ``Failed`` with a diff PNG on disk.
    """
    # A 200x200 solid-colour page; trivially deterministic across
    # Chromium / WebKit and totally hermetic (data: URL — no SPA, no
    # network).
    page.set_viewport_size({"width": 200, "height": 200})
    page.goto(
        "data:text/html,<!doctype html><html><body style='margin:0;background:#abcdef'>"
        "<div id='swatch' style='width:200px;height:200px;background:#abcdef'></div>"
        "</body></html>"
    )
    page.wait_for_load_state("load")

    baselines_dir = tmp_path / "_baselines"
    diff_dir = tmp_path / "_diff"

    # Phase 1 — baseline missing → helper writes + skips.
    with pytest.raises(pytest.skip.Exception, match="baseline missing"):
        compare_screenshot(
            page,
            name="harness_smoke",
            threshold=STYLEGUIDE_DIFF_FRACTION,
            baselines_dir=baselines_dir,
            diff_dir=diff_dir,
        )
    baseline_path = baselines_dir / "harness_smoke.png"
    assert baseline_path.exists(), "helper failed to write the baseline on phase 1"

    # Mutate the on-disk baseline so the next capture diverges by far
    # more than the threshold (we paint a 50% red rectangle into the
    # baseline — 50% mismatch ≫ 0.1% threshold). Mutating the baseline
    # rather than the page lets the test stay synchronous and avoids
    # any browser-side render races.
    baseline_img = Image.open(baseline_path).convert("RGBA")
    draw = ImageDraw.Draw(baseline_img)
    draw.rectangle((0, 0, 100, 200), fill=(255, 0, 0, 255))
    baseline_img.save(baseline_path)

    # Phase 2 — capture diverges from the mutated baseline; helper
    # must fail with a diff PNG written to disk.
    with pytest.raises(pytest.fail.Exception, match="visual regression"):
        compare_screenshot(
            page,
            name="harness_smoke",
            threshold=STYLEGUIDE_DIFF_FRACTION,
            baselines_dir=baselines_dir,
            diff_dir=diff_dir,
        )
    diff_path = diff_dir / "harness_smoke.png"
    assert diff_path.exists(), "helper failed to write the diff PNG on phase 2"
    # Sanity: the diff PNG is a valid image, not empty bytes.
    diff_img = Image.open(BytesIO(diff_path.read_bytes()))
    assert diff_img.size == baseline_img.size


def test_visual_harness_tolerates_trailing_blank_height_drift(
    page: Page, tmp_path: Path
) -> None:
    """Full-page browser captures may grow by blank background rows only."""
    page.set_viewport_size({"width": 200, "height": 200})
    html = (
        "<!doctype html><html><body style='margin:0;background:rgb(171,205,239)'>"
        "<div style='width:200px;height:200px;background:rgb(171,205,239)'>"
        "</div></body></html>"
    )
    page.goto(f"data:text/html,{html}")
    page.wait_for_load_state("load")

    baselines_dir = tmp_path / "_baselines"
    diff_dir = tmp_path / "_diff"
    baselines_dir.mkdir()
    Image.new("RGBA", (200, 190), (171, 205, 239, 255)).save(
        baselines_dir / "blank_tail.png"
    )

    compare_screenshot(
        page,
        name="blank_tail",
        threshold=STYLEGUIDE_DIFF_FRACTION,
        baselines_dir=baselines_dir,
        diff_dir=diff_dir,
    )

    assert not (diff_dir / "blank_tail.png").exists()


def test_visual_harness_rejects_nonblank_trailing_height_drift(
    page: Page, tmp_path: Path
) -> None:
    page.set_viewport_size({"width": 200, "height": 200})
    html = (
        "<!doctype html><html><body style='margin:0;background:rgb(171,205,239)'>"
        "<div style='width:200px;height:190px;background:rgb(171,205,239)'></div>"
        "<div style='width:200px;height:10px;background:rgb(255,0,0)'></div>"
        "</body></html>"
    )
    page.goto(f"data:text/html,{html}")
    page.wait_for_load_state("load")

    baselines_dir = tmp_path / "_baselines"
    diff_dir = tmp_path / "_diff"
    baselines_dir.mkdir()
    Image.new("RGBA", (200, 190), (171, 205, 239, 255)).save(
        baselines_dir / "nonblank_tail.png"
    )

    with pytest.raises(pytest.fail.Exception, match="size mismatch"):
        compare_screenshot(
            page,
            name="nonblank_tail",
            threshold=STYLEGUIDE_DIFF_FRACTION,
            baselines_dir=baselines_dir,
            diff_dir=diff_dir,
        )

    assert (diff_dir / "nonblank_tail.png").exists()


def test_visual_harness_rejects_blank_height_drift_for_viewport_capture(
    page: Page, tmp_path: Path
) -> None:
    page.set_viewport_size({"width": 200, "height": 200})
    html = (
        "<!doctype html><html><body style='margin:0;background:rgb(171,205,239)'>"
        "<div style='width:200px;height:200px;background:rgb(171,205,239)'>"
        "</div></body></html>"
    )
    page.goto(f"data:text/html,{html}")
    page.wait_for_load_state("load")

    baselines_dir = tmp_path / "_baselines"
    diff_dir = tmp_path / "_diff"
    baselines_dir.mkdir()
    Image.new("RGBA", (200, 190), (171, 205, 239, 255)).save(
        baselines_dir / "viewport_tail.png"
    )

    with pytest.raises(pytest.fail.Exception, match="size mismatch"):
        compare_screenshot(
            page,
            name="viewport_tail",
            threshold=STYLEGUIDE_DIFF_FRACTION,
            full_page=False,
            baselines_dir=baselines_dir,
            diff_dir=diff_dir,
        )

    assert (diff_dir / "viewport_tail.png").exists()
