"""Pilot 360x780 mobile-walk over the authenticated SPA (cd-ndmv).

Spec: ``docs/specs/17-testing-quality.md`` §"360 px viewport sitemap"
— full authenticated sitemap walked at 360x780; fails on horizontal
scroll, sub-44x44 tap targets, or unreachable nav.

Pilot scope: hard-coded route list
(:data:`tests.e2e._helpers.sitemap.PILOT_AUTHENTICATED_ROUTES`)
because the SPA does not yet emit a runtime sitemap; the follow-up
Beads task wires the walker to a generated ``_surface.json``.

**Three tests in this file**, each pinning a different invariant:

* :func:`test_walker_detects_known_horizontal_scroll_regression` —
  loads a synthetic page that *deliberately* overflows the 360 px
  viewport, asserts the walker reports a ``horizontal_scroll`` finding.
  Pins the cd-ndmv acceptance criterion "fails on a deliberately
  introduced horizontal scroll".
* :func:`test_authenticated_sitemap_at_360_walks_pilot_routes` —
  smoke-runs the walker against the live SPA. The manager-shell
  off-canvas drawer, side-nav rows, and PreviewShell pills all hold
  the 44x44 floor at 360 px (cd-jptt). Any regression here re-surfaces
  the original cd-ndmv finding shape: one line per (route, kind,
  selector) tuple under a single aggregate :class:`AssertionError`.
* :func:`test_closed_drawer_removes_nav_from_focus_order` — pins the
  ``visibility: hidden`` + ``transition-delay`` trick that closes the
  off-canvas drawer at <=720px (cd-jptt). The closed drawer must NOT
  be reachable via Tab — a future refactor that swaps the trick for a
  pure ``transform`` would silently regress focus order; this test
  fails loudly if it does.

Auth via the dev_login fast path — the walk only needs a session
cookie, not the full passkey ceremony.
"""

from __future__ import annotations

from typing import Final

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


def test_authenticated_sitemap_at_360_walks_pilot_routes(
    context: BrowserContext, base_url: str
) -> None:
    """Walk the pilot routes at 360x780 and assert the full check passes.

    Findings (horizontal scroll, sub-floor tap targets, missing nav)
    surface as a single aggregate :class:`AssertionError` with one
    line per regression.

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
    page.goto(f"{base_url.rstrip('/')}/dashboard", wait_until="domcontentloaded")
    page.locator(".desk").wait_for(state="attached")
    result = walk_authenticated_sitemap(
        page,
        base_url=base_url,
        routes=PILOT_AUTHENTICATED_ROUTES,
    )
    assert_no_findings(result)


def test_closed_drawer_removes_nav_from_focus_order(
    context: BrowserContext, base_url: str
) -> None:
    """The off-canvas drawer must not be tabbable while closed (cd-jptt).

    The phone-mode rule
    ``.desk__nav { visibility: hidden; transition: visibility 0s
    linear 200ms; }`` removes the closed drawer's descendants from
    the focus tree (and from the §17 walker's tappable surface). A
    future refactor that drops ``visibility: hidden`` in favour of
    just ``transform: translateX(-100%)`` would silently regress —
    translated-off-screen elements stay focusable, so ``Tab`` from
    the page would reach the hidden nav links and the user would
    lose focus into invisible chrome.

    The assertion: at 360x780 with the drawer closed, every
    ``.desk__nav`` descendant matching the walker's tappable
    selector reports ``visibility: hidden`` via getComputedStyle —
    which is exactly what removes them from the focus order per
    https://www.w3.org/TR/css-display-3/#visibility (and what
    Chromium / WebKit / Firefox all enforce).

    Sanity-checked with a real ``Tab`` press: focus must NOT land on
    a ``.desk__nav`` descendant.
    """
    login_with_dev_session(
        context,
        base_url=base_url,
        email=WALK_EMAIL,
        workspace_slug=WALK_SLUG,
        role="owner",
    )
    page = context.new_page()
    page.set_viewport_size({"width": 360, "height": 780})
    page.goto(f"{base_url.rstrip('/')}/dashboard", wait_until="domcontentloaded")
    page.locator(".desk__nav").wait_for(state="attached")

    # Confirm the drawer is in its closed state — a stray
    # data-nav-open="true" elsewhere would invalidate the test.
    nav_open = page.evaluate(
        "() => document.querySelector('.desk')?.getAttribute('data-nav-open')"
    )
    assert nav_open in (None, "false"), (
        f"closed-drawer focus test requires data-nav-open!='true'; got {nav_open!r}"
    )

    # 1. Computed visibility check — every nav tappable must inherit
    #    visibility:hidden from the .desk__nav root, which removes them
    #    from the tabbable surface per CSS Display 3.
    visible_descendants = page.evaluate(
        """
        () => {
          const nav = document.querySelector('.desk__nav');
          if (!nav) return { error: 'no .desk__nav found' };
          const tappables = nav.querySelectorAll(
            '[data-tappable], button, a[href], [role="button"], [role="link"]'
          );
          const visible = [];
          for (const el of tappables) {
            const cs = getComputedStyle(el);
            if (cs.visibility !== 'hidden') {
              visible.push({
                tag: el.tagName,
                cls: (el.className?.toString() || '').slice(0, 50),
                visibility: cs.visibility,
              });
            }
          }
          return { total: tappables.length, visible };
        }
        """
    )
    assert "error" not in visible_descendants, visible_descendants
    assert visible_descendants["total"] > 0, (
        "expected the drawer to render at least one tappable in the DOM "
        "(it's hidden, not unmounted); got 0 — selector or layout drift"
    )
    assert visible_descendants["visible"] == [], (
        "closed drawer leaks focusable descendants — "
        "visibility:hidden must propagate to every tappable: "
        f"{visible_descendants['visible']!r}"
    )

    # 2. Sanity tab-press — pressing Tab from a fresh page focus must
    #    NOT land on a .desk__nav descendant. Catches the case where
    #    a nav link explicitly opted out of the inherited visibility
    #    (visibility: visible, tabindex=0, etc.).
    page.evaluate("() => document.body.focus()")
    page.keyboard.press("Tab")
    in_nav = page.evaluate("() => !!document.activeElement?.closest('.desk__nav')")
    assert not in_nav, (
        "Tab from page body landed inside .desk__nav while drawer "
        "was closed — focus order regressed; the visibility:hidden "
        "trick may have been replaced with a translate-only animation"
    )
