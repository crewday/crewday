"""Demo-mode fixture loading and cookie binding helpers."""

from app.demo.cookies import (
    DEMO_COOKIE_MAX_AGE_SECONDS,
    DemoCookieBinding,
    build_demo_cookie_header,
    demo_cookie_name,
    load_demo_cookie,
    mint_demo_cookie,
)
from app.demo.seeder import (
    DEFAULT_SCENARIO_KEY,
    SCENARIO_KEYS,
    SeededDemoWorkspace,
    demo_workspace_slug,
    load_bound_demo_workspace,
    load_bound_demo_workspace_for_slug,
    load_scenario_fixture,
    normalise_start_path,
    resolve_relative_timestamp,
    seed_workspace,
)

__all__ = [
    "DEFAULT_SCENARIO_KEY",
    "DEMO_COOKIE_MAX_AGE_SECONDS",
    "SCENARIO_KEYS",
    "DemoCookieBinding",
    "SeededDemoWorkspace",
    "build_demo_cookie_header",
    "demo_cookie_name",
    "demo_workspace_slug",
    "load_bound_demo_workspace",
    "load_bound_demo_workspace_for_slug",
    "load_demo_cookie",
    "load_scenario_fixture",
    "mint_demo_cookie",
    "normalise_start_path",
    "resolve_relative_timestamp",
    "seed_workspace",
]
