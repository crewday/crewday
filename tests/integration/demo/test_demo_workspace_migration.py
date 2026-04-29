"""Integration checks for the demo_workspace table."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration


def test_demo_workspace_table_shape(engine: Engine) -> None:
    inspector = inspect(engine)
    assert "demo_workspace" in inspector.get_table_names()

    cols = {c["name"]: c for c in inspector.get_columns("demo_workspace")}
    assert set(cols) == {
        "id",
        "scenario_key",
        "seed_digest",
        "created_at",
        "last_activity_at",
        "expires_at",
        "cookie_binding_digest",
    }
    for name in cols:
        assert cols[name]["nullable"] is False

    pk = inspector.get_pk_constraint("demo_workspace")
    assert pk["constrained_columns"] == ["id"]

    fks = inspector.get_foreign_keys("demo_workspace")
    assert len(fks) == 1
    assert fks[0]["constrained_columns"] == ["id"]
    assert fks[0]["referred_table"] == "workspace"
    assert fks[0]["referred_columns"] == ["id"]
