"""Unit tests for :mod:`scripts.dev_login`.

Covers the dev-only force-login script end-to-end at the domain-
service level: ``mint_session`` inserts rows idempotently, the three
hard gates (``CREWDAY_DEV_AUTH`` flag, ``CREWDAY_PROFILE=dev``, sqlite
DB URL), output formats, and the ``audit.dev.force_login`` row shape.

Structured after :mod:`tests.unit.auth.test_signup` and
:mod:`tests.unit.auth.test_session`: an in-memory SQLite engine with
:class:`Base.metadata` schema, the :func:`make_uow` ``_default_*``
module globals patched to route the script's own UoW at the test
engine, and a deterministic :class:`Settings` fixture.

See ``docs/specs/03-auth-and-tokens.md`` §"Sessions" and the Beads
task ``cd-w1ia`` for the motivating context.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from click.testing import CliRunner
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import config as _config_mod
from app.adapters.db import session as _session_mod
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.base import Base
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User
from app.adapters.db.llm.models import BudgetLedger
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.bootstrap import STARTER_WORK_ROLE_KEYS
from app.adapters.db.workspace.models import UserWorkspace, WorkRole, Workspace
from app.auth.signup import FALLBACK_CAP_CENTS
from app.util.clock import SystemClock
from app.util.ulid import new_ulid
from scripts import dev_login

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_dev() -> _config_mod.Settings:
    """Dev-profile settings with a sqlite URL — every gate green."""
    return _config_mod.Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-dev-login-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
        profile="dev",
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def patched_uow(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    """Route :func:`make_uow` at ``engine``.

    :func:`scripts.dev_login.mint_session` opens its own
    :func:`make_uow`; the module-level ``_default_*`` globals in
    :mod:`app.adapters.db.session` are the seam. Swapping them in for
    the test (and restoring them on teardown) means every helper call
    routed through the script lands on the same DB the test asserts
    against.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(_session_mod, "_default_engine", engine, raising=False)
    monkeypatch.setattr(_session_mod, "_default_sessionmaker_", factory, raising=False)
    yield factory


@pytest.fixture
def patched_settings(
    monkeypatch: pytest.MonkeyPatch, settings_dev: _config_mod.Settings
) -> Iterator[_config_mod.Settings]:
    """Pin :func:`get_settings` to ``settings_dev`` for the test.

    :func:`app.config.get_settings` is ``@lru_cache``-wrapped, so a
    simple env-var swap would only take effect on the first call. We
    replace the function outright and clear the cache on teardown so
    later tests see a clean slate.
    """
    monkeypatch.setattr(_config_mod, "get_settings", lambda: settings_dev, raising=True)
    monkeypatch.setattr(dev_login, "get_settings", lambda: settings_dev, raising=True)
    yield settings_dev


@pytest.fixture
def dev_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip ``CREWDAY_DEV_AUTH=1`` on for the test."""
    monkeypatch.setenv("CREWDAY_DEV_AUTH", "1")


# ---------------------------------------------------------------------------
# mint_session — happy path + idempotency
# ---------------------------------------------------------------------------


class TestMintSessionGreenfield:
    def test_mint_session_fresh_creates_user_workspace_grant_session_audit(
        self,
        patched_uow: sessionmaker[Session],
        patched_settings: _config_mod.Settings,
    ) -> None:
        """Greenfield call seeds every identity + tenancy row + audit."""
        result = dev_login.mint_session(
            email="SMOKE@dev.local",
            workspace_slug="smoke",
            display_name="Smoke Tester",
            timezone="UTC",
            role="owner",
        )
        assert result.user_created is True
        assert result.workspace_created is True

        with patched_uow() as s:
            user = s.scalars(
                select(User).where(User.email_lower == "smoke@dev.local")
            ).one()
            assert user.display_name == "Smoke Tester"
            assert user.timezone == "UTC"

            workspace = s.scalars(
                select(Workspace).where(Workspace.slug == "smoke")
            ).one()
            membership = s.scalars(
                select(UserWorkspace).where(UserWorkspace.user_id == user.id)
            ).one()
            assert membership.workspace_id == workspace.id
            assert membership.source == "workspace_grant"

            groups = s.scalars(
                select(PermissionGroup).where(
                    PermissionGroup.workspace_id == workspace.id
                )
            ).all()
            assert {g.slug for g in groups} == {
                "owners",
                "managers",
                "all_workers",
                "all_clients",
            }

            owners_group = next(g for g in groups if g.slug == "owners")
            owners_members = s.scalars(
                select(PermissionGroupMember).where(
                    PermissionGroupMember.group_id == owners_group.id
                )
            ).all()
            assert [m.user_id for m in owners_members] == [user.id]

            grants = s.scalars(
                select(RoleGrant).where(RoleGrant.user_id == user.id)
            ).all()
            assert [g.grant_role for g in grants] == ["manager"]

            session_rows = s.scalars(
                select(SessionRow).where(SessionRow.user_id == user.id)
            ).all()
            assert len(session_rows) == 1
            assert session_rows[0].id == result.session_issue.session_id
            assert session_rows[0].workspace_id == workspace.id

            force_login = s.scalars(
                select(AuditLog).where(AuditLog.action == "dev.force_login")
            ).one()
            assert force_login.entity_kind == "session"
            assert force_login.entity_id == result.session_issue.session_id
            assert force_login.workspace_id == workspace.id
            diff = force_login.diff
            # ``email`` lands scrubbed: the audit writer funnels every
            # diff through :func:`app.util.redact.redact`, so raw PII
            # (including the dev-login email) never touches the log.
            # See ``docs/specs/15-security-privacy.md`` §"Audit log".
            assert diff["email"] == "<redacted:email>"
            assert diff["workspace_slug"] == "smoke"
            assert diff["role"] == "owner"
            assert diff["user_created"] is True
            assert diff["workspace_created"] is True
            assert diff["user_id"] == user.id
            assert diff["workspace_id"] == workspace.id


class TestMintSessionIdempotency:
    def test_mint_session_existing_user_workspace_is_idempotent(
        self,
        patched_uow: sessionmaker[Session],
        patched_settings: _config_mod.Settings,
    ) -> None:
        """Second call reuses user + workspace rows; mints a fresh session."""
        first = dev_login.mint_session(
            email="repeat@dev.local", workspace_slug="repeat"
        )
        second = dev_login.mint_session(
            email="repeat@dev.local", workspace_slug="repeat"
        )

        assert first.user_created is True
        assert first.workspace_created is True
        assert second.user_created is False
        assert second.workspace_created is False
        # Distinct session rows.
        assert first.session_issue.session_id != second.session_issue.session_id
        assert first.session_issue.cookie_value != second.session_issue.cookie_value

        with patched_uow() as s:
            users = s.scalars(
                select(User).where(User.email_lower == "repeat@dev.local")
            ).all()
            assert len(users) == 1
            workspaces = s.scalars(
                select(Workspace).where(Workspace.slug == "repeat")
            ).all()
            assert len(workspaces) == 1
            session_rows = s.scalars(
                select(SessionRow).where(SessionRow.user_id == users[0].id)
            ).all()
            assert len(session_rows) == 2

            force_logins = s.scalars(
                select(AuditLog).where(AuditLog.action == "dev.force_login")
            ).all()
            assert len(force_logins) == 2
            # Second audit row carries user_created=False + workspace_created=False.
            second_diff = next(
                a.diff
                for a in force_logins
                if a.entity_id == second.session_issue.session_id
            )
            assert second_diff["user_created"] is False
            assert second_diff["workspace_created"] is False

    def test_existing_workspace_without_membership_repairs_live_grant_state(
        self,
        patched_uow: sessionmaker[Session],
        patched_settings: _config_mod.Settings,
    ) -> None:
        """Existing slug is reused and incomplete dev-login state is repaired."""
        now = SystemClock().now()
        user_id = new_ulid()
        workspace_id = new_ulid()
        owners_group_id = new_ulid()
        revoked_grant_id = new_ulid()
        property_grant_id = new_ulid()
        deployment_grant_id = new_ulid()

        with patched_uow() as s:
            s.add(
                Workspace(
                    id=workspace_id,
                    slug="preexisting",
                    name="Preexisting",
                    plan="free",
                    quota_json={},
                    created_at=now,
                )
            )
            s.add(
                User(
                    id=user_id,
                    email="repair@dev.local",
                    email_lower="repair@dev.local",
                    display_name="Repair",
                    timezone="UTC",
                    created_at=now,
                )
            )
            s.flush()
            s.add(
                PermissionGroup(
                    id=owners_group_id,
                    workspace_id=workspace_id,
                    slug="owners",
                    name="Owners",
                    system=True,
                    capabilities_json={"all": True},
                    created_at=now,
                )
            )
            s.add(
                RoleGrant(
                    id=revoked_grant_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    grant_role="manager",
                    scope_kind="workspace",
                    scope_property_id=None,
                    created_at=now,
                    created_by_user_id=None,
                    revoked_at=now,
                )
            )
            s.add(
                RoleGrant(
                    id=property_grant_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    grant_role="manager",
                    scope_kind="workspace",
                    scope_property_id=new_ulid(),
                    created_at=now,
                    created_by_user_id=None,
                    revoked_at=None,
                )
            )
            s.add(
                RoleGrant(
                    id=deployment_grant_id,
                    workspace_id=None,
                    user_id=user_id,
                    grant_role="manager",
                    scope_kind="deployment",
                    scope_property_id=None,
                    created_at=now,
                    created_by_user_id=None,
                    revoked_at=None,
                )
            )
            s.commit()

        result = dev_login.mint_session(
            email="repair@dev.local",
            workspace_slug="preexisting",
            role="owner",
        )
        repeated = dev_login.mint_session(
            email="repair@dev.local",
            workspace_slug="preexisting",
            role="owner",
        )

        assert result.user_created is False
        assert result.workspace_created is False
        assert repeated.user_created is False
        assert repeated.workspace_created is False

        with patched_uow() as s:
            workspaces = s.scalars(
                select(Workspace).where(Workspace.slug == "preexisting")
            ).all()
            assert [workspace.id for workspace in workspaces] == [workspace_id]

            membership = s.scalars(
                select(UserWorkspace)
                .where(UserWorkspace.user_id == user_id)
                .where(UserWorkspace.workspace_id == workspace_id)
            ).one()
            assert membership.source == "workspace_grant"

            live_grants = s.scalars(
                select(RoleGrant)
                .where(RoleGrant.user_id == user_id)
                .where(RoleGrant.workspace_id == workspace_id)
                .where(RoleGrant.grant_role == "manager")
                .where(RoleGrant.scope_kind == "workspace")
                .where(RoleGrant.scope_property_id.is_(None))
                .where(RoleGrant.revoked_at.is_(None))
            ).all()
            assert len(live_grants) == 1
            assert live_grants[0].id not in {
                revoked_grant_id,
                property_grant_id,
                deployment_grant_id,
            }

            owners_member = s.scalars(
                select(PermissionGroupMember)
                .where(PermissionGroupMember.group_id == owners_group_id)
                .where(PermissionGroupMember.user_id == user_id)
            ).one()
            assert owners_member.workspace_id == workspace_id


class TestMintSessionFingerprintWiped:
    def test_fingerprint_hash_wiped_so_any_caller_validates(
        self,
        patched_uow: sessionmaker[Session],
        patched_settings: _config_mod.Settings,
    ) -> None:
        """The minted session row has ``fingerprint_hash=NULL``.

        Dev-login can't predict the caller's UA / Accept-Language (curl,
        Playwright, and httpx all differ), so the script wipes the
        fingerprint that :func:`app.auth.session.issue` stamps. The
        :func:`app.auth.session.validate` path then downgrades to the
        idle + absolute caps, which is the right guarantee level for
        a dev-only cookie. Without this invariant the cookie is
        useless from any real client.
        """
        from app.auth.session import validate as validate_session

        result = dev_login.mint_session(email="fp@dev.local", workspace_slug="fp-ws")
        with patched_uow() as s:
            row = s.scalars(
                select(SessionRow).where(
                    SessionRow.id == result.session_issue.session_id
                )
            ).one()
            assert row.fingerprint_hash is None

            # Sanity: the cookie validates even with a non-empty UA /
            # Accept-Language pair the script never saw.
            user_id = validate_session(
                s,
                cookie_value=result.session_issue.cookie_value,
                ua="curl/8.14.1",
                accept_language="en-GB,en;q=0.9",
            )
            assert user_id


class TestMintSessionRoleSurface:
    def test_owner_role_has_has_owner_grant_true_on_session(
        self,
        patched_uow: sessionmaker[Session],
        patched_settings: _config_mod.Settings,
    ) -> None:
        """The ``owner`` script-role mints an owner-shaped session.

        Exercised via two calls: first an owner seeds the workspace
        and gets a 7-day session; then a separate worker dev-login
        joins the existing workspace with ``role=worker`` and gets
        the longer 30-day non-owner session plus a worker grant (and
        crucially, no owners-group membership on their own account).
        Putting both calls against the same slug lets the worker
        exercise the "workspace already exists" branch — the one
        dev-login's idempotency contract actually has to support.
        """
        owner = dev_login.mint_session(
            email="owner@dev.local", workspace_slug="shared-ws", role="owner"
        )
        worker = dev_login.mint_session(
            email="worker@dev.local", workspace_slug="shared-ws", role="worker"
        )

        with patched_uow() as s:
            owner_session = s.scalars(
                select(SessionRow).where(
                    SessionRow.id == owner.session_issue.session_id
                )
            ).one()
            worker_session = s.scalars(
                select(SessionRow).where(
                    SessionRow.id == worker.session_issue.session_id
                )
            ).one()

            # TTL delta on the row — SQLite drops tzinfo, so we compare
            # the raw ``(expires_at - created_at)`` delta which is
            # tzinfo-agnostic.
            owner_ttl = owner_session.expires_at - owner_session.created_at
            worker_ttl = worker_session.expires_at - worker_session.created_at
            assert owner_ttl.days == 7
            assert worker_ttl.days == 30

            # The session.created audit row carries the boolean directly.
            owner_created_audit = s.scalars(
                select(AuditLog)
                .where(AuditLog.action == "session.created")
                .where(AuditLog.entity_id == owner.session_issue.session_id)
            ).one()
            assert owner_created_audit.diff["has_owner_grant"] is True
            worker_created_audit = s.scalars(
                select(AuditLog)
                .where(AuditLog.action == "session.created")
                .where(AuditLog.entity_id == worker.session_issue.session_id)
            ).one()
            assert worker_created_audit.diff["has_owner_grant"] is False

            # Worker role has a worker role-grant, no owners membership.
            worker_user = s.scalars(
                select(User).where(User.email_lower == "worker@dev.local")
            ).one()
            worker_grants = s.scalars(
                select(RoleGrant).where(RoleGrant.user_id == worker_user.id)
            ).all()
            assert [g.grant_role for g in worker_grants] == ["worker"]
            worker_memberships = s.scalars(
                select(PermissionGroupMember).where(
                    PermissionGroupMember.user_id == worker_user.id
                )
            ).all()
            # A worker is NOT a member of the owners group.
            for member in worker_memberships:
                group = s.get(PermissionGroup, member.group_id)
                assert group is not None
                assert group.slug != "owners"


# ---------------------------------------------------------------------------
# Budget ledger seed — cd-tubi
# ---------------------------------------------------------------------------


class TestMintSessionBudgetLedger:
    """cd-tubi: dev-login greenfield workspaces seed a budget ledger row.

    The dev-login path calls :func:`provision_workspace_and_owner_seat`
    without a :class:`Capabilities` envelope, so the fallback cap fires
    (``FALLBACK_CAP_CENTS`` = 500, scaled to 50 by §03 "Tight initial
    caps"). These tests pin the ledger-seed invariant on **every**
    dev-minted workspace path — greenfield (both user + workspace
    fresh) and the existing-user / fresh-workspace fallback — so a
    future refactor of the provisioning seam never silently drops the
    seed from either path; otherwise dev-minted workspaces would
    refuse LLM calls until someone hand-seeded the row.
    """

    def test_mint_session_fresh_seeds_budget_ledger_with_tight_cap(
        self,
        patched_uow: sessionmaker[Session],
        patched_settings: _config_mod.Settings,
    ) -> None:
        """Greenfield dev-login seeds the ledger at the tight 10 % cap."""
        from app.domain.plans import tight_cap_cents

        result = dev_login.mint_session(
            email="budget@dev.local", workspace_slug="budget-ws"
        )
        assert result.workspace_created is True

        with patched_uow() as s:
            workspace = s.scalars(
                select(Workspace).where(Workspace.slug == "budget-ws")
            ).one()
            ledger = s.scalars(
                select(BudgetLedger).where(BudgetLedger.workspace_id == workspace.id)
            ).one()
            # §03 "Tight initial caps": 10 % of the 500-cent fallback.
            assert ledger.cap_cents == tight_cap_cents(FALLBACK_CAP_CENTS)
            assert ledger.cap_cents == 50
            assert ledger.spent_cents == 0
            # quota_json agrees with the ledger cap — the two views of
            # the budget ceiling must never drift.
            assert workspace.quota_json["llm_budget_cents_30d"] == ledger.cap_cents

    def test_mint_session_existing_user_fresh_workspace_also_seeds_ledger(
        self,
        patched_uow: sessionmaker[Session],
        patched_settings: _config_mod.Settings,
    ) -> None:
        """The existing-user / fresh-workspace fallback path also seeds the ledger.

        :func:`_resolve_or_create_workspace` replays the workspace-only
        half of :func:`provision_workspace_and_owner_seat`; before
        cd-wpyt it silently skipped the ledger-seed step, so dev-login
        on an existing user joining a new slug left the workspace
        with no :class:`BudgetLedger` row — every
        :func:`~app.domain.llm.budget.check_budget` call on that
        workspace would then fail closed on ``llm.budget.ledger_missing``
        until an operator manually seeded the row. Pin the invariant.
        """
        from app.domain.plans import tight_cap_cents

        # First call: greenfield user + workspace A.
        first = dev_login.mint_session(
            email="shared@dev.local", workspace_slug="dev-existing-a"
        )
        assert first.user_created is True
        assert first.workspace_created is True

        # Second call: SAME user, DIFFERENT workspace slug. Exercises
        # the ``existing_user is not None and existing_workspace is None``
        # branch in ``mint_session`` — the one that calls into
        # :func:`_resolve_or_create_workspace` on the fresh-slug side.
        second = dev_login.mint_session(
            email="shared@dev.local", workspace_slug="dev-existing-b"
        )
        assert second.user_created is False
        assert second.workspace_created is True

        with patched_uow() as s:
            ws_b = s.scalars(
                select(Workspace).where(Workspace.slug == "dev-existing-b")
            ).one()
            ledger_b = s.scalars(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws_b.id)
            ).one()
            assert ledger_b.cap_cents == tight_cap_cents(FALLBACK_CAP_CENTS)
            assert ledger_b.cap_cents == 50
            assert ledger_b.spent_cents == 0
            assert ws_b.quota_json["llm_budget_cents_30d"] == ledger_b.cap_cents

    def test_mint_session_seeds_starter_work_roles_idempotently(
        self,
        patched_uow: sessionmaker[Session],
        patched_settings: _config_mod.Settings,
    ) -> None:
        first = dev_login.mint_session(
            email="roles@dev.local", workspace_slug="dev-roles"
        )
        second = dev_login.mint_session(
            email="roles@dev.local", workspace_slug="dev-roles"
        )
        assert first.workspace_created is True
        assert second.workspace_created is False

        with patched_uow() as s:
            workspace = s.scalars(
                select(Workspace).where(Workspace.slug == "dev-roles")
            ).one()
            roles = s.scalars(
                select(WorkRole)
                .where(WorkRole.workspace_id == workspace.id)
                .order_by(WorkRole.key)
            ).all()
            assert [role.key for role in roles] == sorted(STARTER_WORK_ROLE_KEYS)
            assert len({role.key for role in roles}) == len(STARTER_WORK_ROLE_KEYS)


# ---------------------------------------------------------------------------
# Hard gates
# ---------------------------------------------------------------------------


class TestGates:
    def test_gate_refuses_when_flag_off(
        self,
        patched_uow: sessionmaker[Session],
        patched_settings: _config_mod.Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``CREWDAY_DEV_AUTH`` unset / 0 → exit 1, stderr names the flag."""
        monkeypatch.delenv("CREWDAY_DEV_AUTH", raising=False)
        runner = CliRunner()
        result = runner.invoke(
            dev_login.main,
            ["--email", "gate@dev.local", "--workspace", "gate"],
        )
        assert result.exit_code == 1
        assert "CREWDAY_DEV_AUTH" in result.stderr

    def test_gate_refuses_on_profile_prod(
        self,
        patched_uow: sessionmaker[Session],
        dev_auth_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``CREWDAY_PROFILE=prod`` + flag on → exit 1 with profile message."""
        prod_settings = _config_mod.Settings.model_construct(
            database_url="sqlite:///:memory:",
            root_key=SecretStr("unit-test-dev-login-root-key"),
            profile="prod",
        )
        monkeypatch.setattr(
            _config_mod, "get_settings", lambda: prod_settings, raising=True
        )
        monkeypatch.setattr(
            dev_login, "get_settings", lambda: prod_settings, raising=True
        )
        runner = CliRunner()
        result = runner.invoke(
            dev_login.main,
            ["--email", "gate@dev.local", "--workspace", "gate"],
        )
        assert result.exit_code == 1
        assert "profile" in result.stderr.lower()
        assert "'prod'" in result.stderr

    def test_gate_refuses_on_postgres_url(
        self,
        patched_uow: sessionmaker[Session],
        dev_auth_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Postgres-shaped DB URL → exit 1 with DB message."""
        pg_settings = _config_mod.Settings.model_construct(
            database_url="postgresql://user:pw@localhost/crewday",
            root_key=SecretStr("unit-test-dev-login-root-key"),
            profile="dev",
        )
        monkeypatch.setattr(
            _config_mod, "get_settings", lambda: pg_settings, raising=True
        )
        monkeypatch.setattr(
            dev_login, "get_settings", lambda: pg_settings, raising=True
        )
        runner = CliRunner()
        result = runner.invoke(
            dev_login.main,
            ["--email", "gate@dev.local", "--workspace", "gate"],
        )
        assert result.exit_code == 1
        assert "SQLite" in result.stderr or "sqlite" in result.stderr.lower()
        assert "postgresql" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


class TestOutputFormats:
    """Parametrise over every supported ``--output`` shape."""

    @pytest.mark.parametrize(
        "fmt,check",
        [
            ("cookie", lambda out, cv: out == f"__Host-crewday_session={cv}"),
            (
                "json",
                lambda out, cv: (
                    json.loads(out) == {"name": "__Host-crewday_session", "value": cv}
                ),
            ),
            (
                "curl",
                lambda out, cv: out == f"-b '__Host-crewday_session={cv}'",
            ),
            (
                "header",
                lambda out, cv: out == f"Cookie: __Host-crewday_session={cv}",
            ),
            (
                "playwright",
                lambda out, cv: (
                    json.loads(out)
                    == {
                        "name": "crewday_session",
                        "value": cv,
                        "url": "http://127.0.0.1:8100",
                        "httpOnly": True,
                        "secure": False,
                        "sameSite": "Lax",
                    }
                ),
            ),
        ],
    )
    def test_output_formats(
        self,
        patched_uow: sessionmaker[Session],
        patched_settings: _config_mod.Settings,
        dev_auth_env: None,
        fmt: str,
        check: object,
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(
            dev_login.main,
            [
                "--email",
                f"fmt-{fmt}@dev.local",
                "--workspace",
                f"fmt-{fmt}",
                "--output",
                fmt,
            ],
        )
        assert result.exit_code == 0, result.stderr or result.output
        # click's echo appends a trailing newline; strip it before
        # comparing the payload shape.
        out = result.output.rstrip("\n")
        # The cookie value appears in every shape; pull it out of the
        # row the script just wrote.
        with patched_uow() as s:
            session_rows = s.scalars(select(SessionRow)).all()
            assert len(session_rows) == 1
            # The cookie value is opaque + not stored — we can only
            # cross-check the row's PK is the sha256-hex of ``out``'s
            # embedded cookie payload. Extract the cookie from each
            # shape.
        if fmt == "cookie":
            cookie_value = out.split("=", 1)[1]
        elif fmt == "json":
            cookie_value = json.loads(out)["value"]
        elif fmt == "curl":
            cookie_value = out.split("=", 1)[1].rstrip("'")
        elif fmt == "header":
            cookie_value = out.split("=", 1)[1]
        elif fmt == "playwright":
            cookie_value = json.loads(out)["value"]
        else:  # pragma: no cover - parametrize guard
            pytest.fail(f"unknown fmt {fmt!r}")

        assert callable(check)
        assert check(out, cookie_value)  # type: ignore[operator]

        # Sanity: the row's PK is sha256-hex of the emitted cookie.
        import hashlib

        expected_pk = hashlib.sha256(cookie_value.encode("utf-8")).hexdigest()
        with patched_uow() as s:
            row = s.scalars(select(SessionRow)).one()
            assert row.id == expected_pk
