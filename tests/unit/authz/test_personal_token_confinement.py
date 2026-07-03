"""Unit tests for the personal-access-token ``/me`` route predicate.

§03 "Personal access tokens": a PAT is confined to the ``/me``
self-service subtree by the tenancy middleware
(:meth:`app.tenancy.middleware.WorkspaceContextMiddleware.dispatch`).
:func:`app.tenancy.middleware._is_me_scoped_path` is the structural
boundary that decision keys on; these tests pin its exact segment
match so a future router mount cannot silently widen (or narrow) the
PAT surface without turning one of them red.

The end-to-end confinement (mint a real PAT, drive the middleware,
assert 403 off the subtree / 200 on it) lives in
``tests/integration/auth/test_personal_token_confinement.py``.
"""

from __future__ import annotations

import pytest

from app.tenancy.middleware import _is_me_scoped_path


class TestIsMeScopedPath:
    """The ``/w/<slug>/api/<ver>/me`` subtree, matched per-segment."""

    @pytest.mark.parametrize(
        "path",
        [
            "/w/acme/api/v1/me",
            "/w/acme/api/v1/me/",
            "/w/acme/api/v1/me/schedule",
            "/w/acme/api/v1/me/leaves",
            "/w/acme/api/v1/me/availability_overrides",
            # Version is not pinned — a future ``/me`` subtree stays confined.
            "/w/acme/api/v2/me/schedule",
        ],
    )
    def test_me_subtree_matches(self, path: str) -> None:
        assert _is_me_scoped_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            # Sibling workspace routes — every one must be OUT of the PAT set.
            "/w/acme/api/v1/tasks",
            "/w/acme/api/v1/payroll",
            "/w/acme/api/v1/employees",
            "/w/acme/api/v1/bookings",
            "/w/acme/api/v1/auth/tokens",
            # Exact-segment compare: ``me_tokens`` / ``messaging`` do not
            # start-with their way past the boundary.
            "/w/acme/api/v1/me_tokens",
            "/w/acme/api/v1/me-tokens",
            "/w/acme/api/v1/messaging/me",
            # Case-sensitive: Starlette matches ``/me`` lowercase, so an
            # upper/mixed-case fifth segment is a different (non-existent)
            # route and must stay refused — never admit a PAT on it.
            "/w/acme/api/v1/ME",
            "/w/acme/api/v1/Me/schedule",
            # Wrong workspace anchor / non-api second segment.
            "/W/acme/api/v1/me",
            "/w/acme/events",
            # Bare-host ``/api/v1/me`` is session-cookie-only and never a
            # scoped ``/w/...`` path — it is not the workspace subtree.
            "/api/v1/me",
            "/api/v1/me/tokens",
            # Malformed / short paths.
            "/w/acme/api/v1",
            "/w/acme",
            "/",
            "",
        ],
    )
    def test_non_me_paths_do_not_match(self, path: str) -> None:
        assert _is_me_scoped_path(path) is False
