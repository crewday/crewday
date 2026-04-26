"""Pilot 360x780 mobile-walk over the authenticated SPA (cd-ndmv).

Spec: ``docs/specs/17-testing-quality.md`` §"360 px viewport sitemap"
— full authenticated sitemap walked at 360x780; fails on horizontal
scroll, sub-44x44 tap targets, or unreachable nav.

Pilot scope: hard-coded route list
(:data:`tests.e2e._helpers.sitemap.PILOT_AUTHENTICATED_ROUTES`)
because the SPA does not yet emit a runtime sitemap; the follow-up
Beads task wires the walker to a generated ``_surface.json``.

**Two tests in this file**, each pinning a different invariant:

* :func:`test_walker_detects_known_horizontal_scroll_regression` —
  loads a synthetic page that *deliberately* overflows the 360 px
  viewport, asserts the walker reports a ``horizontal_scroll`` finding.
  Pins the cd-ndmv acceptance criterion "fails on a deliberately
  introduced horizontal scroll".
* :func:`test_authenticated_sitemap_at_360_walks_pilot_routes` —
  smoke-runs the walker against the live SPA. Today the SPA does
  not yet meet the 44x44 floor on the manager nav (the nav links
  render at 32 px tall on /properties / /employees / etc.); the
  walker correctly reports those as findings, so the test
  ``xfail``\\s with ``strict=True`` until the SPA fix lands. The
  walker is still exercised on every CI run; flipping ``xfail`` →
  ``passes`` is the signal that the SPA has caught up to the spec.
  See follow-up Beads task for the SPA work.

Auth via the dev_login fast path — the walk only needs a session
cookie, not the full passkey ceremony.
"""

from __future__ import annotations

from typing import Final

import pytest
from playwright.sync_api import BrowserContext

from tests.e2e._helpers.auth import login_with_dev_session
from tests.e2e._helpers.sitemap import (
    PILOT_AUTHENTICATED_ROUTES,
    assert_no_findings,
    walk_authenticated_sitemap,
)

WALK_EMAIL: Final[str] = "e2e-pilot-mobile-walk@dev.local"
WALK_SLUG: Final[str] = "e2e-pilot-mobile-walk"

# A 600 px-wide replaced element (SVG) forces horizontal scroll
# inside a 360 px viewport. Replaced elements (SVG, IMG, IFRAME)
# carry an intrinsic width that the layout actually honours; a plain
# ``<div style="width:600px">`` is treated as a hint and quietly
# clipped on small screens. Inline data: URL keeps the test
# hermetic — no fixture file, no SPA dependency, just the contract
# proof for the walker.
_OVERFLOW_PAGE: Final[str] = (
    "data:text/html,"
    "<!doctype html><html><head><title>overflow</title></head>"
    "<body style='margin:0'>"
    "<svg width='600' height='50'></svg>"
    "<button style='width:48px;height:48px'>tap</button>"
    "</body></html>"
)


def test_walker_detects_known_horizontal_scroll_regression(
    context: BrowserContext,
) -> None:
    """The walker reports a horizontal-scroll finding on a known-bad page.

    Loads a hard-coded data URL that overflows the 360 px viewport
    by design. Pins cd-ndmv acceptance criterion: "360x780 sitemap
    walk fails on a deliberately-introduced horizontal scroll".

    No auth needed — data URLs render without the dev stack; the
    test still piggybacks on the suite's session-scoped readiness
    probe so a stack-down state surfaces uniformly.
    """
    page = context.new_page()
    result = walk_authenticated_sitemap(
        page,
        base_url=_OVERFLOW_PAGE,  # used as a literal URL
        routes=("",),  # no path appended — data URL is complete
        viewport_width=360,
        viewport_height=780,
        nav_selector=None,  # data URL has no real nav
    )
    horizontal = [f for f in result.findings if f.kind == "horizontal_scroll"]
    assert horizontal, (
        f"walker did not flag a horizontal-scroll regression on the "
        f"overflow page; got findings={result.findings!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "SPA does not yet meet the 44x44 tap-target floor at 360px on the "
        "manager nav (text-link rows render at ~32px tall). Tracked in a "
        "follow-up Beads task; remove this xfail when the SPA fix lands."
    ),
)
def test_authenticated_sitemap_at_360_walks_pilot_routes(
    context: BrowserContext, base_url: str
) -> None:
    """Walk the pilot routes at 360x780 and assert the full check passes.

    Currently expected to fail on tap-target regressions in the
    manager-shell navigation; the ``strict=True`` xfail flips to a
    real pass once the SPA closes the gaps. Findings (horizontal
    scroll, sub-floor tap targets, missing nav) surface as a single
    aggregate :class:`AssertionError` with one line per regression.

    Routes come from the pilot constant; the follow-up Beads task
    swaps in a runtime ``_surface.json`` walker.
    """
    login_with_dev_session(
        context,
        base_url=base_url,
        email=WALK_EMAIL,
        workspace_slug=WALK_SLUG,
        role="owner",
    )
    page = context.new_page()
    result = walk_authenticated_sitemap(
        page,
        base_url=base_url,
        routes=PILOT_AUTHENTICATED_ROUTES,
    )
    assert_no_findings(result)
