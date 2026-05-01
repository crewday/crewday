"""Shared autouse fixtures for places-context integration tests.

A sibling unit test (``tests/unit/test_tenancy_orm_filter.py``) resets
the process-wide tenancy registry in its autouse fixture. Each module
in this package already re-registers the plain workspace-scoped tables
it depends on, but the **scope-through-join** registrations for
``unit`` / ``area`` / ``property_closure`` (cd-014h) are owned by
:mod:`app.adapters.db.places` at import time and are not re-applied by
those module-local fixtures. Without re-registering them here, after
the unit-test reset the ORM tenant filter silently treats those tables
as un-scoped — a soft failure where queries either hit the wrong code
path (``target.c.workspace_id`` AttributeError) or, worse, return
unfiltered rows.

Scoping the fixture to ``conftest.py`` keeps the registration in one
place rather than threading it through every module's existing
``_ensure_tables_registered`` fixture.
"""

from __future__ import annotations

import pytest

from app.tenancy import registry


@pytest.fixture(autouse=True)
def _ensure_places_scope_through_join_registered() -> None:
    for table in ("unit", "area", "property_closure"):
        registry.register_scope_through_join(
            table,
            via_table="property_workspace",
            via_local_column="property_id",
            via_remote_column="property_id",
        )
