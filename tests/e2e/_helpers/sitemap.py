"""360 px viewport sitemap walk for the e2e suite (cd-ndmv).

Spec: ``docs/specs/17-testing-quality.md`` §"360 px viewport sitemap"
— the full authenticated sitemap is walked at 360x780 and fails on
any horizontal scroll, any tap target < 44x44, or any unreachable nav
entry.

This is the **web-platform side** of §14's native-wrapper readiness
contract: the native shell later consumes the same routes as a black
box, so any regression at the loopback breaks both surfaces.

The pilot (cd-ndmv) hard-codes a small list of authenticated routes
because the SPA does not yet emit a runtime ``_surface.json`` for
its pages (the existing ``cli/crewday/_surface.json`` describes the
HTTP surface, not the UI sitemap). A follow-up Beads task wires the
walker to a generated ``app/web/dist/_surface.json`` once the SPA
build emits one.

Each route's check is **route-scoped** — a failure on one route
collects, but the walker keeps going so the test sees the full
worst-case picture. The aggregate failure is raised at the end with
the per-route findings.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final

from playwright.sync_api import Locator, Page

__all__ = [
    "PILOT_AUTHENTICATED_ROUTES",
    "TAP_TARGET_MIN_PX",
    "RouteFinding",
    "WalkResult",
    "assert_no_findings",
    "walk_authenticated_sitemap",
]


# WCAG / Apple HIG / Material — 44x44 dp is the floor for tappable
# elements per §14 native-wrapper readiness (and also the floor for
# the iOS App Store review checklist). The check applies to every
# element with ``role=button``, ``role=link``, or the
# ``data-tappable`` opt-in attribute.
TAP_TARGET_MIN_PX: Final[int] = 44

# Hard-coded pilot route list. Each entry is the SPA path; the walker
# prepends the test's base URL. The follow-up that wires
# ``_surface.json`` will retire this constant — keeping it visible
# (instead of inlining at the call site) means a future grep finds
# the pilot anchor.
PILOT_AUTHENTICATED_ROUTES: Final[tuple[str, ...]] = (
    "/today",
    "/schedule",
    "/dashboard",
    "/properties",
    "/employees",
)


@dataclass(frozen=True)
class RouteFinding:
    """One regression discovered at a route during the walk.

    ``kind`` is one of ``"horizontal_scroll"``, ``"tap_target"``,
    ``"nav_unreachable"``, ``"navigation_error"``. The walker emits
    one finding per (route, kind, locator-description) tuple so the
    aggregate failure message stays scannable.
    """

    route: str
    kind: str
    detail: str


@dataclass
class WalkResult:
    """Output of :func:`walk_authenticated_sitemap`.

    ``findings`` is mutable so the walker can append while iterating
    — the consumer sees a frozen view via :meth:`is_clean` and the
    aggregate-error helper :func:`assert_no_findings`.
    """

    findings: list[RouteFinding] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """Return ``True`` when no findings were recorded."""
        return not self.findings


def walk_authenticated_sitemap(
    page: Page,
    *,
    base_url: str,
    routes: Iterable[str] = PILOT_AUTHENTICATED_ROUTES,
    viewport_width: int = 360,
    viewport_height: int = 780,
    nav_selector: str | None = "nav, [role='navigation']",
) -> WalkResult:
    """Walk ``routes`` at 360x780 and collect tap-target / scroll regressions.

    For each route:

    1. Set the viewport to ``viewport_width x viewport_height``.
    2. Navigate to ``base_url + route``.
    3. Assert ``document.documentElement.scrollWidth <= viewport_width``
       — any horizontal overflow is recorded as a ``horizontal_scroll``
       finding (the spec is explicit that horizontal scroll fails the
       gate at 360 px).
    4. Walk every ``data-tappable`` / ``button`` / ``a`` /
       ``role=button`` / ``role=link`` element and assert the bounding
       box is at least ``TAP_TARGET_MIN_PX x TAP_TARGET_MIN_PX``. Hidden
       (``display:none`` / ``visibility:hidden``) and zero-area elements
       are skipped — the gate is for *visible* tappables.
    5. Assert at least one element matches ``nav_selector`` so the
       primary navigation is reachable; pass ``nav_selector=None`` for
       routes that intentionally have no nav (login, recover).

    Returns the :class:`WalkResult` for the test to consume; pair with
    :func:`assert_no_findings` for a one-line aggregate assertion.
    """
    page.set_viewport_size({"width": viewport_width, "height": viewport_height})
    result = WalkResult()
    for route in routes:
        result.visited.append(route)
        try:
            page.goto(f"{base_url.rstrip('/')}{route}", wait_until="networkidle")
        except Exception as exc:
            result.findings.append(
                RouteFinding(route=route, kind="navigation_error", detail=repr(exc))
            )
            continue

        scroll_width = int(page.evaluate("() => document.documentElement.scrollWidth"))
        if scroll_width > viewport_width:
            result.findings.append(
                RouteFinding(
                    route=route,
                    kind="horizontal_scroll",
                    detail=(
                        f"document.documentElement.scrollWidth={scroll_width}, "
                        f"viewport={viewport_width}"
                    ),
                )
            )

        if nav_selector is not None:
            nav = page.locator(nav_selector).first
            if not nav.is_visible():
                result.findings.append(
                    RouteFinding(
                        route=route,
                        kind="nav_unreachable",
                        detail=f"no visible element matched {nav_selector!r}",
                    )
                )

        for finding in _check_tap_targets(page, route=route):
            result.findings.append(finding)

    return result


def assert_no_findings(result: WalkResult) -> None:
    """Raise :class:`AssertionError` with every finding if any were recorded.

    The aggregate message lists one finding per line so a CI log grep
    matches exact ``route >> kind`` pairs.
    """
    if result.is_clean:
        return
    lines = [f"  - {f.route} >> {f.kind}: {f.detail}" for f in result.findings]
    summary = (
        f"360px sitemap walk recorded {len(result.findings)} "
        f"finding(s) across {len(result.visited)} route(s):"
    )
    raise AssertionError("\n".join([summary, *lines]))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_TAPPABLE_SELECTOR: Final[str] = (
    "[data-tappable], button, a[href], [role='button'], [role='link']"
)


def _check_tap_targets(page: Page, *, route: str) -> Iterator[RouteFinding]:
    """Yield tap-target findings for visible tappables on the current page.

    A tappable below the 44x44 floor on **either axis** is a finding.
    Hidden / zero-area elements are skipped — the gate is for
    visible-and-tappable elements; an off-screen ``display:none``
    button has no human-facing tap surface.
    """
    locators = page.locator(_TAPPABLE_SELECTOR)
    count = locators.count()
    for index in range(count):
        node = locators.nth(index)
        if not node.is_visible():
            continue
        box = node.bounding_box()
        if box is None or box["width"] == 0 or box["height"] == 0:
            continue
        width = int(box["width"])
        height = int(box["height"])
        if width >= TAP_TARGET_MIN_PX and height >= TAP_TARGET_MIN_PX:
            continue
        # Best-effort label — a CSS selector path would be ideal but
        # Playwright doesn't expose it for arbitrary nodes. The
        # element's text + tag is enough to grep audit on a regression.
        try:
            text = (node.inner_text(timeout=500) or "").strip()
        except Exception:
            text = ""
        try:
            tag = node.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            tag = "?"
        label = (text[:40] + "…") if len(text) > 40 else text
        yield RouteFinding(
            route=route,
            kind="tap_target",
            detail=(
                f"<{tag}> {width}x{height} (min {TAP_TARGET_MIN_PX}x"
                f"{TAP_TARGET_MIN_PX}); text={label!r}"
            ),
        )


@contextmanager
def temporary_viewport(page: Page, width: int, height: int) -> Iterator[None]:
    """Restore the page's viewport on exit.

    Convenience for tests that intersperse desktop + mobile checks in
    one Playwright session; the helpers above set the viewport once
    and leave it (no reason to undo it during a single walk).
    """
    original = page.viewport_size or {"width": 1280, "height": 720}
    page.set_viewport_size({"width": width, "height": height})
    try:
        yield
    finally:
        page.set_viewport_size(original)


# Exposed for tests that want to assert ``Locator`` types — kept here
# so the public surface stays one import.
__all__ = [*__all__, "Locator"]
