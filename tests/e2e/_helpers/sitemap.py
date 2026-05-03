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

**CI** runs ``npx vite build`` inside the ``web-dev`` container after
the dev stack is healthy (``.github/workflows/ci.yml`` ▸ the e2e job
▸ "Emit SPA surface manifest" step, cd-qv2l). The web-dev bind-mount
(``../app/web:/web``) projects ``/web/dist/_surface.json`` onto the
host's ``app/web/dist/_surface.json``, which is exactly where this
helper reads it. The e2e job also sets
``CREWDAY_REQUIRE_SURFACE_MANIFEST=1`` so a missing or malformed
manifest fails instead of falling back to the 5-route smoke subset.

**Locally**, run ``npm run build`` (or ``npx vite build``) inside
``app/web/`` once before invoking ``pytest tests/e2e`` to exercise
the full 45-route manifest. The fallback to
:data:`PILOT_AUTHENTICATED_ROUTES` emits a WARNING so the gap is
visible in pytest's ``-v`` log when the SPA hasn't been built locally.

**``/admin/*`` filter (cd-qv2l).** The walker test
:func:`tests.e2e.test_sitemap_mobile_walk
.test_authenticated_sitemap_at_360_walks_runtime_routes` filters out
``/admin/...`` routes because ``login_with_dev_session(role="owner")``
mints a workspace-scoped grant only — the admin shell gates on
``is_deployment_admin`` (a separate deployment-scope grant that
``scripts/dev_login.py`` does not currently expose). The 10 admin
routes still ship in the manifest for native shells and a future
admin-walk test; they just aren't reachable from the current
dev-login fast path.

Each route's check is **route-scoped** — a failure on one route
collects, but the walker keeps going so the test sees the full
worst-case picture. The aggregate failure is raised at the end with
the per-route findings.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page

__all__ = [
    "PILOT_AUTHENTICATED_ROUTES",
    "REQUIRE_SURFACE_MANIFEST_ENV",
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

REQUIRE_SURFACE_MANIFEST_ENV: Final[str] = "CREWDAY_REQUIRE_SURFACE_MANIFEST"


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
    *,
    require_manifest: bool | None = None,
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

    Set ``CREWDAY_REQUIRE_SURFACE_MANIFEST=1`` (or pass
    ``require_manifest=True``) to turn those fallback cases into
    :class:`RuntimeError`. CI uses that strict mode after emitting the
    manifest so it cannot silently fall back to the 5-route smoke
    subset.
    """
    path = manifest_path if manifest_path is not None else SURFACE_MANIFEST_PATH
    strict = (
        os.environ.get(REQUIRE_SURFACE_MANIFEST_ENV) == "1"
        if require_manifest is None
        else require_manifest
    )

    def fallback(reason: str) -> tuple[str, ...]:
        message = f"surface manifest {path} {reason}"
        if strict:
            raise RuntimeError(f"{message}; fallback disabled by strict mode")
        _log.warning("%s; falling back to pilot routes", message)
        return PILOT_AUTHENTICATED_ROUTES

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fallback("not found")
    except OSError as exc:
        return fallback(f"unreadable ({exc})")

    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        return fallback(f"is not valid JSON ({exc})")

    if not isinstance(data, dict):
        return fallback("is not a JSON object")

    routes = data.get("authenticated")
    if not isinstance(routes, list):
        return fallback("missing/invalid 'authenticated' list")

    if not all(isinstance(entry, str) for entry in routes):
        return fallback("'authenticated' contains non-string entries")

    if not routes:
        return fallback("has no authenticated routes")

    if any(not entry.startswith("/") for entry in routes):
        return fallback("'authenticated' contains non-path entries")

    unique_routes = tuple(dict.fromkeys(routes))
    if len(unique_routes) != len(routes):
        return fallback("'authenticated' contains duplicate routes")

    if strict and len(unique_routes) <= len(PILOT_AUTHENTICATED_ROUTES):
        raise RuntimeError(
            f"surface manifest {path} lists only {len(unique_routes)} authenticated "
            "route(s); expected the full SPA surface, not the pilot smoke subset"
        )

    return unique_routes


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
            _navigate_to_route(page, base_url=base_url, route=route)
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


def _navigate_to_route(page: Page, *, base_url: str, route: str) -> None:
    """Render one route without reloading the SPA for every same-origin path."""
    target = f"{base_url.rstrip('/')}{route}"
    base = urlparse(base_url)
    current = urlparse(page.url)
    if base.scheme not in {"http", "https"} or (
        current.scheme,
        current.netloc,
    ) != (base.scheme, base.netloc):
        page.goto(target, wait_until="domcontentloaded", timeout=45_000)
        _wait_for_route_to_settle(page, route=route, base_scheme=base.scheme)
        return

    page.evaluate(
        """
        (path) => {
          window.history.pushState({}, "", path);
          window.dispatchEvent(new PopStateEvent("popstate", {
            state: window.history.state,
          }));
        }
        """,
        route,
    )
    _wait_for_route_to_settle(page, route=route, base_scheme=base.scheme)


def _wait_for_route_to_settle(page: Page, *, route: str, base_scheme: str) -> None:
    if base_scheme not in {"http", "https"} or not route.startswith("/"):
        return

    page.wait_for_function(
        "(path) => window.location.pathname === path",
        arg=route,
        timeout=5_000,
    )
    page.evaluate(
        """
        () => new Promise((resolve) => {
          requestAnimationFrame(() => requestAnimationFrame(resolve));
        })
        """
    )
    final_path = urlparse(page.url).path
    if final_path != route:
        raise RuntimeError(f"route {route!r} redirected to {final_path!r}")


def _check_tap_targets(page: Page, *, route: str) -> Iterator[RouteFinding]:
    """Yield tap-target findings for visible tappables on the current page.

    A tappable below the 44x44 floor on **either axis** is a finding.
    Hidden / zero-area elements are skipped — the gate is for
    visible-and-tappable elements; an off-screen ``display:none``
    button has no human-facing tap surface.
    """
    raw_findings = page.evaluate(
        """
        ([selector, minPx]) => {
          const findings = [];
          for (const el of document.querySelectorAll(selector)) {
            const cs = getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden') continue;

            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            if (rect.width >= minPx && rect.height >= minPx) continue;

            findings.push({
              tag: el.tagName.toLowerCase(),
              width: Math.trunc(rect.width),
              height: Math.trunc(rect.height),
              text: (el.textContent || '').trim().slice(0, 41),
            });
          }
          return findings;
        }
        """,
        [_TAPPABLE_SELECTOR, TAP_TARGET_MIN_PX],
    )
    if not isinstance(raw_findings, list):
        return

    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        tag = item.get("tag")
        width = item.get("width")
        height = item.get("height")
        text = item.get("text")
        if (
            not isinstance(tag, str)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or not isinstance(text, str)
        ):
            continue
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
