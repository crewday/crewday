"""Unit coverage for :func:`app.domain.llm.consent.load_consent_set`.

Spins a per-test in-memory SQLite session bound to ``Base.metadata``
and exercises the loader against the workspace-scope
``agent_preference`` row. The integration coverage in
``tests/integration/llm/test_outbound_redaction.py`` proves the
loader threads through the redaction seam end-to-end; this file pins
the loader's defensive contract (allow-list filter, missing rows,
empty body, alternate-scope rows ignored) without a live LLM client.

See ``docs/specs/11-llm-and-agents.md`` §"Redaction / PII".
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.llm.models import AgentPreference
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.llm.consent import load_consent_set
from app.util.redact import CONSENT_TOKENS, ConsentSet
from app.util.ulid import new_ulid

_NOW = datetime(2026, 5, 3, 9, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
    """Import every adapter package's ``models`` so ``Base.metadata`` is full.

    Mirrors the pattern in ``tests/unit/messaging/test_daily_digest.py``;
    ``Base.metadata.create_all`` only knows about classes whose modules
    have been imported, and the cross-package FKs land at import time.
    """
    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    # Skip every workspace FK; the loader only joins on
    # ``workspace_id`` columns and never resolves the parent row, so
    # ephemeral string ids are sufficient for the unit surface.
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


def _seed_workspace(session: Session, workspace_id: str) -> None:
    """Insert the parent ``workspace`` row so the FK constraint holds.

    ``agent_preference.workspace_id`` is a CASCADE FK against
    ``workspace.id``; seeding the parent keeps SQLite happy without
    any further service-layer setup.
    """
    session.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-6:].lower()}",
            name="ws",
            plan="free",
            quota_json={},
            verification_state="unverified",
            created_at=_NOW,
        )
    )
    session.flush()


def _seed_workspace_pref(
    session: Session,
    *,
    workspace_id: str,
    upstream_pii_consent: list[str] | None = None,
    scope_kind: str = "workspace",
    scope_id: str | None = None,
) -> None:
    _seed_workspace(session, workspace_id)
    row = AgentPreference(
        id=new_ulid(),
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_id=scope_id if scope_id is not None else workspace_id,
        body_md="",
        token_count=0,
        blocked_actions=[],
        default_approval_mode="auto",
        upstream_pii_consent=(
            upstream_pii_consent if upstream_pii_consent is not None else []
        ),
        updated_by_user_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        archived_at=None,
    )
    session.add(row)
    session.flush()


class TestLoadConsentSet:
    def test_returns_none_when_no_row_exists(self, session: Session) -> None:
        result = load_consent_set(session, workspace_id=new_ulid())
        assert result == ConsentSet.none()

    def test_returns_none_when_column_is_empty_list(self, session: Session) -> None:
        workspace_id = new_ulid()
        _seed_workspace_pref(
            session, workspace_id=workspace_id, upstream_pii_consent=[]
        )

        assert load_consent_set(session, workspace_id) == ConsentSet.none()

    def test_returns_single_token_when_one_field_opted_in(
        self, session: Session
    ) -> None:
        workspace_id = new_ulid()
        _seed_workspace_pref(
            session,
            workspace_id=workspace_id,
            upstream_pii_consent=["legal_name"],
        )

        result = load_consent_set(session, workspace_id)
        assert result.allows("legal_name")
        assert not result.allows("email")
        assert result.fields == frozenset({"legal_name"})

    def test_returns_all_tokens_when_multiple_fields_opted_in(
        self, session: Session
    ) -> None:
        workspace_id = new_ulid()
        _seed_workspace_pref(
            session,
            workspace_id=workspace_id,
            upstream_pii_consent=sorted(CONSENT_TOKENS),
        )

        result = load_consent_set(session, workspace_id)
        assert result.fields == CONSENT_TOKENS

    def test_unknown_tokens_are_silently_dropped(self, session: Session) -> None:
        """Defence-in-depth: a typo in the DB does not widen what flows upstream."""
        workspace_id = new_ulid()
        _seed_workspace_pref(
            session,
            workspace_id=workspace_id,
            upstream_pii_consent=["legal_name", "ssn", "credit_card"],
        )

        result = load_consent_set(session, workspace_id)
        assert result.fields == frozenset({"legal_name"})

    def test_returns_none_when_only_unknown_tokens(self, session: Session) -> None:
        workspace_id = new_ulid()
        _seed_workspace_pref(
            session,
            workspace_id=workspace_id,
            upstream_pii_consent=["ssn", "credit_card"],
        )

        assert load_consent_set(session, workspace_id) == ConsentSet.none()

    def test_property_scope_row_is_ignored(self, session: Session) -> None:
        """Workspace-scope only — property / user rows do not feed the loader."""
        workspace_id = new_ulid()
        property_id = new_ulid()
        _seed_workspace_pref(
            session,
            workspace_id=workspace_id,
            scope_kind="property",
            scope_id=property_id,
            upstream_pii_consent=["email"],
        )

        assert load_consent_set(session, workspace_id) == ConsentSet.none()

    def test_other_workspace_row_is_isolated(self, session: Session) -> None:
        own_id = new_ulid()
        other_id = new_ulid()
        _seed_workspace_pref(
            session,
            workspace_id=other_id,
            upstream_pii_consent=["legal_name"],
        )

        assert load_consent_set(session, own_id) == ConsentSet.none()
