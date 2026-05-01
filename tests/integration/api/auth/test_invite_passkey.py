"""Integration tests for ``POST /api/v1/invite/passkey/{start,finish}`` (cd-9q6bb).

Bridges :func:`app.api.v1.auth.invite.build_invite_router`'s new
invite-driven passkey enrolment surface against a real engine
(SQLite by default; Postgres when ``CREWDAY_TEST_DB=postgres``).
The two new endpoints close the §03 GA gap that journey 2 of the
e2e suite (cd-db0g) depends on: an invitee whose ``user`` row
already exists at invite time but who holds no passkey can now
drive the WebAuthn ceremony from the bare host.

Coverage:

* **Happy path.** Start mints a challenge bound to the
  ``invite_id``; finish lands a :class:`PasskeyCredential` for the
  pre-existing user row; the subsequent ``/invite/complete`` call
  activates grants exactly as the integration-level
  ``test_invite_accept`` suite covers via the direct domain seed.
* **Authorisation gates.** Unknown invite → 404; finish replay
  after success → 409 (``passkey_already_registered``); missing
  invite_id → 422 (FastAPI body validation).
* **Challenge hygiene.** A failed-attestation finish burns the
  challenge row so the next finish replays as 409
  ``challenge_consumed_or_unknown``, matching
  :func:`app.api.v1.auth.passkey.post_register_finish`.

The tests stub :func:`app.auth.passkey._verify_or_raise` (same
seam as the signup full-flow test) — the real WebAuthn
verification is exercised in
``tests/integration/identity/test_passkey_register_pg.py``;
this file's job is the HTTP envelope + state-machine coverage.

See ``docs/specs/03-auth-and-tokens.md`` §"Additional users
(invite → click-to-accept)" and ``docs/specs/12-rest-api.md``
§"Auth".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from webauthn.helpers.structs import (
    AttestationFormat,
    CredentialDeviceType,
    PublicKeyCredentialType,
)

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import (
    Invite,
    MagicLinkNonce,
    PasskeyCredential,
    User,
    canonicalise_email,
)
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth import invite as invite_module
from app.auth import magic_link, passkey
from app.auth._throttle import Throttle
from app.auth.webauthn import VerifiedRegistration
from app.config import Settings
from app.tenancy import registry, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


# Anchored slightly behind real wall-clock so the magic-link token still has
# a valid ``exp`` claim when the handler resolves it via :class:`SystemClock`.
_PINNED = datetime.now(tz=UTC) - timedelta(hours=1)
_BASE_URL = "https://crew.day"
_ROOT_KEY = "integration-invite-passkey-root-key-0123456789ab"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        root_key=SecretStr(_ROOT_KEY),
        public_url=_BASE_URL,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    registry.register("invite")
    registry.register("audit_log")
    registry.register("user_workspace")
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("role_grant")
    registry.register("work_engagement")
    registry.register("user_work_role")
    registry.register("work_role")


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def client(
    engine: Engine,
    settings: Settings,
    session_factory: sessionmaker[Session],
    throttle: Throttle,
) -> Iterator[TestClient]:
    """FastAPI :class:`TestClient` mounting the singular invite router.

    The new passkey endpoints live on the singular router (the plural
    surface stays read-only / accept-only). Mount only what the test
    needs so a missing wire-up on the plural surface fails loudly.
    """
    import app.adapters.db.session as _session_mod

    app = FastAPI()
    app.include_router(
        invite_module.build_invite_router(throttle=throttle, settings=settings),
        prefix="/api/v1",
    )

    def _session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[db_session_dep] = _session

    # ``burn_challenge_on_failure`` opens its own UoW via
    # :func:`make_uow`, which honours the module-level default
    # sessionmaker — not the test's overridden ``db_session`` dep.
    # Without this redirect the fresh-UoW burn would target the
    # wrong (or absent) engine and silently log-and-swallow, leaving
    # the challenge replayable. Mirrors the signup full-flow test.
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = session_factory
    try:
        with TestClient(app, base_url="https://testserver") as c:
            yield c
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory

    # Sweep committed rows so sibling tests see a clean slate.
    with session_factory() as s:
        for model in (
            AuditLog,
            MagicLinkNonce,
            Invite,
            PasskeyCredential,
            User,
        ):
            with tenant_agnostic():
                for row in s.scalars(select(model)).all():
                    s.delete(row)
        s.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_passkey_verifier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    credential_id: bytes = b"invite-cred-" + b"x" * 20,
) -> bytes:
    """Patch :func:`app.auth.passkey._verify_or_raise` to always succeed.

    Mirrors the pattern used by ``test_signup_full_flow``: the real
    WebAuthn verifier needs a live authenticator; the integration test
    only cares about the HTTP envelope + state machine, so we stub
    the verifier and assert on the row layout. Real verification is
    covered by :mod:`tests.integration.identity.test_passkey_register_pg`.
    """
    verified = VerifiedRegistration(
        credential_id=credential_id,
        credential_public_key=b"pub-" + b"\x00" * 60,
        sign_count=0,
        aaguid="00000000-0000-0000-0000-000000000000",
        fmt=AttestationFormat.NONE,
        credential_type=PublicKeyCredentialType.PUBLIC_KEY,
        user_verified=True,
        attestation_object=b"",
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
    )

    def _fake_verify(**_: Any) -> VerifiedRegistration:
        return verified

    monkeypatch.setattr(passkey, "_verify_or_raise", _fake_verify)
    return credential_id


def _seed_workspace_with_owner(
    session_factory: sessionmaker[Session],
    *,
    slug: str,
    owner_email: str,
) -> tuple[str, str]:
    """Seed an owner + workspace; return ``(workspace_id, owner_id)``."""
    with session_factory() as s:
        owner = bootstrap_user(
            s,
            email=owner_email,
            display_name=f"Owner {slug}",
            clock=FrozenClock(_PINNED),
        )
        ws = bootstrap_workspace(
            s,
            slug=slug,
            name=f"WS {slug}",
            owner_user_id=owner.id,
            clock=FrozenClock(_PINNED),
        )
        s.commit()
        return ws.id, owner.id


def _seed_invite(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    inviter_id: str,
    invitee_email: str,
    invitee_display_name: str = "Invitee Pat",
    seed_passkey: bool = False,
) -> tuple[str, str, str]:
    """Seed an :class:`Invite` row + magic-link nonce.

    Returns ``(invite_id, token, invitee_user_id)``. When
    ``seed_passkey`` is ``True``, plants a fake passkey row so the
    "already enrolled" branch can be exercised.
    """
    with session_factory() as s:
        invitee_id = new_ulid()
        with tenant_agnostic():
            s.add(
                User(
                    id=invitee_id,
                    email=invitee_email,
                    email_lower=canonicalise_email(invitee_email),
                    display_name=invitee_display_name,
                    created_at=_PINNED,
                )
            )
            s.flush()

        if seed_passkey:
            with tenant_agnostic():
                s.add(
                    PasskeyCredential(
                        id=f"pk-{invitee_id}".encode(),
                        user_id=invitee_id,
                        public_key=b"seeded-public-key",
                        sign_count=0,
                        transports=None,
                        backup_eligible=False,
                        label="seeded passkey",
                        created_at=_PINNED,
                        last_used_at=None,
                    )
                )
                s.flush()

        invite_id = new_ulid()
        invite_row = Invite(
            id=invite_id,
            workspace_id=workspace_id,
            user_id=invitee_id,
            pending_email=canonicalise_email(invitee_email),
            pending_email_lower=canonicalise_email(invitee_email),
            email_hash="test-email-hash",
            display_name=invitee_display_name,
            state="pending",
            grants_json=[
                {
                    "scope_kind": "workspace",
                    "scope_id": workspace_id,
                    "grant_role": "worker",
                }
            ],
            group_memberships_json=[],
            invited_by_user_id=inviter_id,
            created_at=_PINNED,
            expires_at=_PINNED + timedelta(hours=24),
            accepted_at=None,
            revoked_at=None,
        )
        with tenant_agnostic():
            s.add(invite_row)
            s.flush()

        pending = magic_link.request_link(
            s,
            email=invitee_email,
            purpose="grant_invite",
            ip="127.0.0.1",
            mailer=None,
            base_url=_BASE_URL,
            now=_PINNED,
            ttl=timedelta(hours=24),
            throttle=Throttle(),
            settings=Settings(
                root_key=SecretStr(_ROOT_KEY),
                public_url=_BASE_URL,
            ),
            clock=FrozenClock(_PINNED),
            subject_id=invite_id,
            send_email=False,
        )
        s.commit()
        assert pending is not None
        token = pending.url.rsplit("/", 1)[-1]
        return invite_id, token, invitee_id


def _make_credential_payload() -> dict[str, Any]:
    """Return the credential JSON the SPA would send.

    The actual bytes are ignored because :func:`_stub_passkey_verifier`
    short-circuits the WebAuthn verifier; FastAPI's body validation
    only cares that the field is a dict.
    """
    return {
        "id": "stub-rawid",
        "rawId": "stub-rawid",
        "type": "public-key",
        "response": {
            "clientDataJSON": "stub",
            "attestationObject": "stub",
        },
    }


def _accept_invite(client: TestClient, *, token: str) -> dict[str, Any]:
    """Drive ``/invite/accept`` to burn the magic link and resolve user_id."""
    r = client.post("/api/v1/invite/accept", json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict)
    return body


def _force_burn_magic_link(
    session_factory: sessionmaker[Session], *, invite_id: str
) -> None:
    """Manually flip ``consumed_at`` on the matching ``grant_invite`` nonce.

    Used by the "already enrolled" guard test: that scenario needs the
    magic-link gate to PASS (so the user-already-has-passkey gate fires
    next), but routing through ``/invite/accept`` would raise
    ``passkey_session_required`` for an existing-user invitee — and
    the FastAPI session rollback would revert the ``consumed_at``
    UPDATE, leaving us back in the "magic link not consumed" branch.
    Forcing the flip out-of-band keeps the test focused on the
    second gate without re-implementing the existing-user accept
    ceremony.
    """
    with session_factory() as s:
        with tenant_agnostic():
            nonce = s.scalar(
                select(MagicLinkNonce).where(
                    MagicLinkNonce.subject_id == invite_id,
                    MagicLinkNonce.purpose == "grant_invite",
                )
            )
        assert nonce is not None
        nonce.consumed_at = _PINNED
        s.commit()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestInvitePasskeyHappyPath:
    """start → finish → /complete drives every downstream row."""

    def test_full_enrolment_lands_passkey_then_completes(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        credential_id = _stub_passkey_verifier(monkeypatch)
        ws_id, owner_id = _seed_workspace_with_owner(
            session_factory,
            slug="cd-9q6bb-happy",
            owner_email="owner-happy@acme.test",
        )
        invite_id, token, invitee_id = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="invitee-happy@acme.test",
        )

        # 1. Accept burns the magic link, returns the new_user envelope.
        accept_body = _accept_invite(client, token=token)
        assert accept_body["kind"] == "new_user"
        assert accept_body["invite_id"] == invite_id
        assert accept_body["user_id"] == invitee_id

        # 2. Passkey start mints a fresh challenge bound to invite_id.
        r = client.post(
            "/api/v1/invite/passkey/start",
            json={"invite_id": invite_id},
        )
        assert r.status_code == 200, r.text
        start_body = r.json()
        challenge_id = start_body["challenge_id"]
        assert isinstance(challenge_id, str) and challenge_id
        assert start_body["options"], "missing CreationOptions payload"

        # 3. Passkey finish verifies + persists the credential.
        r = client.post(
            "/api/v1/invite/passkey/finish",
            json={
                "invite_id": invite_id,
                "challenge_id": challenge_id,
                "credential": _make_credential_payload(),
            },
        )
        assert r.status_code == 200, r.text
        finish_body = r.json()
        assert finish_body["user_id"] == invitee_id

        # 4. The PasskeyCredential row landed against the invitee's
        #    pre-existing user_id (no new user created).
        with session_factory() as s:
            cred = s.scalars(
                select(PasskeyCredential).where(
                    PasskeyCredential.user_id == invitee_id
                )
            ).one()
            assert cred.id == credential_id

        # 5. /invite/complete activates the grants.
        r = client.post(
            "/api/v1/invite/complete",
            json={"invite_id": invite_id},
        )
        assert r.status_code == 200, r.text
        complete_body = r.json()
        assert complete_body["workspace_id"] == ws_id

        # 6. The invite row flipped to accepted.
        with session_factory() as s:
            with tenant_agnostic():
                row = s.get(Invite, invite_id)
            assert row is not None
            assert row.state == "accepted"
            assert row.accepted_at is not None


# ---------------------------------------------------------------------------
# State-machine guards
# ---------------------------------------------------------------------------


class TestInvitePasskeyStateGuards:
    """Reject calls that violate the invite state contract."""

    def test_unknown_invite_id_returns_404(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_passkey_verifier(monkeypatch)
        r = client.post(
            "/api/v1/invite/passkey/start",
            json={"invite_id": new_ulid()},
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "invite_not_found"

    def test_skipping_accept_returns_401(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Passkey enrolment without a prior ``/invite/accept`` is rejected.

        cd-9q6bb authorisation gate #1: ``invite_id`` is "not a secret"
        per spec §03; the email-control proof comes from the magic link.
        Calling ``/invite/passkey/start`` without first burning the
        magic-link nonce means the caller never proved they received
        the email — collapse onto the same ``passkey_session_required``
        symbol the existing-user accept branch raises.
        """
        _stub_passkey_verifier(monkeypatch)
        ws_id, owner_id = _seed_workspace_with_owner(
            session_factory,
            slug="cd-9q6bb-no-accept",
            owner_email="owner-no-accept@acme.test",
        )
        invite_id, _token, _invitee_id = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="invitee-no-accept@acme.test",
        )

        # No /accept call — go straight to /passkey/start.
        r = client.post(
            "/api/v1/invite/passkey/start",
            json={"invite_id": invite_id},
        )
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "passkey_session_required"

    def test_passkey_already_registered_returns_409(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_passkey_verifier(monkeypatch)
        ws_id, owner_id = _seed_workspace_with_owner(
            session_factory,
            slug="cd-9q6bb-already",
            owner_email="owner-already@acme.test",
        )
        invite_id, _token, _invitee_id = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="invitee-already@acme.test",
            seed_passkey=True,
        )

        # Force-flip the magic-link nonce's ``consumed_at`` so the gate-#1
        # check passes; we want gate-#3 (passkey already registered) to
        # be the one that fires, not gate-#1.
        _force_burn_magic_link(session_factory, invite_id=invite_id)

        r = client.post(
            "/api/v1/invite/passkey/start",
            json={"invite_id": invite_id},
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "passkey_already_registered"

    def test_finish_replay_after_success_rejects_with_409(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_passkey_verifier(monkeypatch)
        ws_id, owner_id = _seed_workspace_with_owner(
            session_factory,
            slug="cd-9q6bb-replay",
            owner_email="owner-replay@acme.test",
        )
        invite_id, token, _invitee_id = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="invitee-replay@acme.test",
        )
        _accept_invite(client, token=token)

        start = client.post(
            "/api/v1/invite/passkey/start",
            json={"invite_id": invite_id},
        )
        challenge_id = start.json()["challenge_id"]
        finish = client.post(
            "/api/v1/invite/passkey/finish",
            json={
                "invite_id": invite_id,
                "challenge_id": challenge_id,
                "credential": _make_credential_payload(),
            },
        )
        assert finish.status_code == 200

        # Replay against the same invite — the user now has a passkey,
        # so the state guard fires before any challenge work.
        replay = client.post(
            "/api/v1/invite/passkey/finish",
            json={
                "invite_id": invite_id,
                "challenge_id": challenge_id,
                "credential": _make_credential_payload(),
            },
        )
        assert replay.status_code == 409, replay.text
        assert replay.json()["detail"]["error"] == "passkey_already_registered"


# ---------------------------------------------------------------------------
# Challenge hygiene
# ---------------------------------------------------------------------------


class TestInvitePasskeyChallengeHygiene:
    """Failed attestation burns the challenge so retry must restart."""

    def test_invalid_attestation_burns_challenge(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_id, owner_id = _seed_workspace_with_owner(
            session_factory,
            slug="cd-9q6bb-burn",
            owner_email="owner-burn@acme.test",
        )
        invite_id, token, _invitee_id = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="invitee-burn@acme.test",
        )
        _accept_invite(client, token=token)

        # Stub the verifier to raise — the route must burn the
        # challenge row before it propagates the 400 to the SPA.
        def _fail(**_: Any) -> VerifiedRegistration:
            raise passkey.InvalidRegistration("synthetic verification failure")

        monkeypatch.setattr(passkey, "_verify_or_raise", _fail)

        start = client.post(
            "/api/v1/invite/passkey/start",
            json={"invite_id": invite_id},
        )
        assert start.status_code == 200
        challenge_id = start.json()["challenge_id"]

        first = client.post(
            "/api/v1/invite/passkey/finish",
            json={
                "invite_id": invite_id,
                "challenge_id": challenge_id,
                "credential": _make_credential_payload(),
            },
        )
        assert first.status_code == 400, first.text
        assert first.json()["detail"]["error"] == "invalid_registration"

        # Replay with the same challenge — the row is gone, so the
        # retry collapses onto challenge_consumed_or_unknown rather
        # than handing the attacker a second verification attempt.
        retry = client.post(
            "/api/v1/invite/passkey/finish",
            json={
                "invite_id": invite_id,
                "challenge_id": challenge_id,
                "credential": _make_credential_payload(),
            },
        )
        assert retry.status_code == 409, retry.text
        assert retry.json()["detail"]["error"] == "challenge_consumed_or_unknown"
