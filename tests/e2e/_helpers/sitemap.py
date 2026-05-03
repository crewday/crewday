"""360 px viewport sitemap walk for the e2e suite (cd-ndmv, cd-7zfr).

Spec: ``docs/specs/17-testing-quality.md`` §"360 px viewport sitemap"
— the full authenticated sitemap is walked at 360x780 and fails on
any horizontal scroll, any tap target < 44x44, or any unreachable nav
entry.

This is the **web-platform side** of §14's native-wrapper readiness
contract: the native shell later consumes the same routes as a black
box, so any regression at the loopback breaks both surfaces.

The route list is read at test time from ``app/web/dist/_surface.json``
(emitted by the ``crewday:emit-surface-manifest`` Vite plugin from
``app/web/src/routes/_surface.ts``). When the SPA hasn't been built —
or the manifest is malformed — :func:`load_authenticated_routes`
falls back to :data:`PILOT_AUTHENTICATED_ROUTES`, the original cd-ndmv
smoke list, so the e2e suite still has *some* coverage even with a
stale ``dist/``.

**CI gap (cd-7zfr follow-up).** ``mocks/docker-compose.yml`` runs the
SPA via ``vite dev`` (``web-dev`` container, ``npm run dev``); no
production build runs in CI, so ``dist/_surface.json`` is absent and
:func:`load_authenticated_routes` falls back to
:data:`PILOT_AUTHENTICATED_ROUTES` on every CI run. Locally, run
``pnpm -C app/web build`` once before invoking ``pytest tests/e2e``
to exercise the full 45-route manifest. The fallback emits a WARNING
so the gap is visible in pytest's ``-v`` log; wiring the build into
CI (e2e job adds a pnpm install + build step before
``docker compose up``) is the canonical fix and is left as a
follow-up Beads task.

Each route's check is **route-scoped** — a failure on one route
collects, but the walker keeps going so the test sees the full
worst-case picture. The aggregate failure is raised at the end with
the per-route findings.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from playwright.sync_api import Locator, Page

__all__ = [
    "PILOT_AUTHENTICATED_ROUTES",
    "SURFACE_MANIFEST_PATH",
    "TAP_TARGET_MIN_PX",
    "RouteFinding",
    "WalkResult",
    "assert_no_findings",
    "load_authenticated_routes",
    "walk_authenticated_sitemap",
]


_log = logging.getLogger(__name__)


# WCAG / Apple HIG / Material — 44x44 dp is the floor for tappable
# elements per §14 native-wrapper readiness (and also the floor for
# the iOS App Store review checklist). The check applies to every
# element with ``role=button``, ``role=link``, or the
# ``data-tappable`` opt-in attribute.
TAP_TARGET_MIN_PX: Final[int] = 44

# Smoke fallback for :func:`load_authenticated_routes`. Used when
# ``app/web/dist/_surface.json`` is missing (e.g. the SPA hasn't been
# built) or unreadable. The live walker reads the manifest at runtime
# via :func:`load_authenticated_routes`; this constant is intentionally
# tiny (5 routes) so a stale ``dist/`` still gets some coverage instead
# of silently falling through to zero.
PILOT_AUTHENTICATED_ROUTES: Final[tuple[str, ...]] = (
    "/today",
    "/schedule",
    "/dashboard",
    "/properties",
    "/employees",
)


# Default location of the SPA build manifest. Computed by walking up
# from this file (``tests/e2e/_helpers/sitemap.py``) to the repo root
# and joining ``app/web/dist/_surface.json``. Exposed as a module
# attribute so the helper unit tests can monkeypatch it without
# reaching into private state.
SURFACE_MANIFEST_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "app" / "web" / "dist" / "_surface.json"
)


def load_authenticated_routes(
    manifest_path: Path | None = None,
) -> tuple[str, ...]:
    """Return the authenticated SPA routes from the build manifest.

    The manifest is emitted by the ``crewday:emit-surface-manifest``
    Vite plugin (see ``app/web/vite.config.ts``) and lives at
    ``app/web/dist/_surface.json`` after a production build. Schema:
    ``{"version": 1, "authenticated": ["/today", ...]}``.

    Falls back to :data:`PILOT_AUTHENTICATED_ROUTES` (logging a
    WARNING) when:

    * the manifest file does not exist (SPA not built);
    * the file is unreadable or malformed JSON;
    * the JSON is valid but missing the ``authenticated`` key, or that
      key is not a list of strings.

    Never raises — the e2e suite must still be able to run with the
    smaller pilot list when the SPA isn't built. Pass ``manifest_path``
    to override the default location (used by the helper unit tests).
    """
    path = manifest_path if manifest_path is not None else SURFACE_MANIFEST_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _log.warning(
            "surface manifest %s not found; falling back to pilot routes", path
        )
        return PILOT_AUTHENTICATED_ROUTES
    except OSError as exc:
        _log.warning(
            "surface manifest %s unreadable (%s); falling back to pilot routes",
            path,
            exc,
        )
        return PILOT_AUTHENTICATED_ROUTES

    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning(
            "surface manifest %s is not valid JSON (%s); falling back to pilot routes",
            path,
            exc,
        )
        return PILOT_AUTHENTICATED_ROUTES

    if not isinstance(data, dict):
        _log.warning(
            "surface manifest %s is not a JSON object; falling back to pilot routes",
            path,
        )
        return PILOT_AUTHENTICATED_ROUTES

    routes = data.get("authenticated")
    if not isinstance(routes, list):
        _log.warning(
            "surface manifest %s missing/invalid 'authenticated' list; "
            "falling back to pilot routes",
            path,
        )
        return PILOT_AUTHENTICATED_ROUTES

    if not all(isinstance(entry, str) for entry in routes):
        _log.warning(
            "surface manifest %s 'authenticated' contains non-string entries; "
            "falling back to pilot routes",
            path,
        )
        return PILOT_AUTHENTICATED_ROUTES

    return tuple(routes)


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
            page.goto(f"{base_url.rstrip('/')}{route}", wait_until="domcontentloaded")
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
            navs = page.locator(nav_selector)
            visible_nav = any(
                navs.nth(index).is_visible() for index in range(navs.count())
            )
            if not visible_nav:
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
