"""cd-zzplt — ``chat_link_challenge`` is workspace-scoped under a ctx.

Before cd-zzplt the table was registered as a *plain* workspace-scoped
table (``register("chat_link_challenge")``) yet the model carried no
``workspace_id`` column, so the ORM tenant filter hit
``target.c.workspace_id`` and raised ``AttributeError`` the moment the
§23 chat-channel binding-verify flow ran under a real
:class:`~app.tenancy.WorkspaceContext`. cd-zzplt added the denormalised
``workspace_id`` column (backfilled from the parent binding) so the
plain registration works as intended.

These tests drive
:meth:`app.domain.messaging.channel_bindings.ChatChannelBindingService.verify`
through the **filtered** session seam (the production shape:
:func:`app.tenancy.orm_filter.install_tenant_filter` is installed on
``tenant_session_factory`` and the active ctx lives in the
``ContextVar``). They prove:

* the challenge create + ``increment_challenge_attempts`` +
  ``verify_binding`` write paths all run under ctx A without an
  ``AttributeError`` or :class:`~app.tenancy.orm_filter.TenantFilterMissing`
  and without a ``tenant_agnostic`` escape; and
* a challenge (and its binding) created under workspace B is neither
  readable nor verifiable under ctx A — cross-tenant isolation holds.

See ``docs/specs/17-testing-quality.md`` §"Cross-tenant regression
test" and ``docs/specs/23-chat-gateway.md`` §"off-app channels".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.messaging.models import ChatChannelBinding, ChatLinkChallenge
from app.adapters.db.messaging.repositories import (
    SqlAlchemyChatChannelBindingRepository,
)
from app.domain.messaging.channel_bindings import (
    MOCK_LINK_CODE,
    ChatChannelBindingInvalid,
    ChatChannelBindingNotFound,
    ChatChannelBindingService,
)
from app.events.bus import EventBus
from app.tenancy import tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.tenant.conftest import TenantSeed

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 5, 1, 6, 0, 0, tzinfo=UTC)


def _service(seed: TenantSeed) -> ChatChannelBindingService:
    """Build the binding service on an isolated no-op event bus + clock."""
    return ChatChannelBindingService(
        seed.ctx,
        clock=FrozenClock(_PINNED),
        # Fresh bus so no production subscriber fires a side-effect
        # write during the test.
        event_bus=EventBus(),
    )


def test_verify_under_ctx_a_scopes_challenge_writes(
    tenant_session_factory: sessionmaker[Session],
    tenant_a: TenantSeed,
) -> None:
    """start + increment + verify all run scoped under ctx A, no crash.

    The wrong-code branch exercises ``increment_challenge_attempts``
    (a scoped ORM UPDATE by PK); the correct-code branch exercises
    ``verify_binding`` (a scoped ORM UPDATE that consumes the
    challenge). Both would raise ``AttributeError`` /
    ``TenantFilterMissing`` under the pre-cd-zzplt registration.
    """
    with tenant_session_factory() as session:
        token = set_current(tenant_a.ctx)
        try:
            repo = SqlAlchemyChatChannelBindingRepository(session)
            service = _service(tenant_a)

            start = service.start(
                repo,
                user_id=tenant_a.owner_user_id,
                channel_kind="offapp_whatsapp",
                address="+15550002222",
            )
            binding_id = start.binding.id

            # The challenge row carries the ctx workspace, and the
            # ctx-scoped read finds it.
            challenge = repo.latest_open_challenge(binding_id=binding_id)
            assert challenge is not None

            # Wrong code -> ChatChannelBindingInvalid, and the service
            # bumped the attempts counter via a scoped UPDATE.
            with pytest.raises(ChatChannelBindingInvalid):
                service.verify(repo, binding_id=binding_id, code="000000")
            bumped = repo.latest_open_challenge(binding_id=binding_id)
            assert bumped is not None
            assert bumped.attempts == 1

            # Correct code -> verify_binding activates the binding and
            # consumes the challenge, both under the ctx-scoped seam.
            verified = service.verify(repo, binding_id=binding_id, code=MOCK_LINK_CODE)
            assert verified.state == "active"
            # Consumed -> no longer an *open* challenge for the binding.
            assert repo.latest_open_challenge(binding_id=binding_id) is None
        finally:
            reset_current(token)
        # Discard the uncommitted rows so the shared seed stays clean.
        session.rollback()


def test_challenge_in_workspace_b_is_not_readable_or_verifiable_under_ctx_a(
    tenant_session_factory: sessionmaker[Session],
    tenant_a: TenantSeed,
    tenant_b: TenantSeed,
) -> None:
    """A pending binding + open challenge owned by B is hidden from ctx A.

    Seeds the B-owned rows directly (INSERT is not tenant-filtered),
    then probes under ctx A: ``latest_open_challenge`` returns ``None``
    and ``verify`` raises :class:`ChatChannelBindingNotFound` because
    the workspace-scoped binding lookup cannot see B's row.
    """
    b_binding_id = new_ulid()
    b_challenge_id = new_ulid()
    with tenant_session_factory() as session:
        # justification: seeding a peer-workspace (B) binding + challenge
        # for a cross-tenant isolation probe; INSERTs are not
        # tenant-filtered, and no ctx is set for the seed.
        with tenant_agnostic():
            session.add(
                ChatChannelBinding(
                    id=b_binding_id,
                    workspace_id=tenant_b.workspace_id,
                    user_id=tenant_b.owner_user_id,
                    channel_kind="offapp_whatsapp",
                    address="+15550003333",
                    address_hash="peer-hash",
                    display_label="WhatsApp",
                    state="pending",
                    created_at=_PINNED,
                    verified_at=None,
                    revoked_at=None,
                    revoke_reason=None,
                    last_message_at=None,
                    provider_metadata_json={},
                )
            )
            session.flush()
            session.add(
                ChatLinkChallenge(
                    id=b_challenge_id,
                    workspace_id=tenant_b.workspace_id,
                    binding_id=b_binding_id,
                    code_hash="peer-code-hash",
                    code_hash_params="sha256:mock",
                    sent_via="channel",
                    attempts=0,
                    expires_at=_PINNED + timedelta(minutes=15),
                    consumed_at=None,
                    created_at=_PINNED,
                )
            )
            session.flush()

        token = set_current(tenant_a.ctx)
        try:
            repo_a = SqlAlchemyChatChannelBindingRepository(session)
            # The challenge belongs to B; the ctx-A scoped read hides it.
            assert repo_a.latest_open_challenge(binding_id=b_binding_id) is None
            # The binding is invisible too, so verify fails not-found
            # before it ever compares a code.
            service_a = _service(tenant_a)
            with pytest.raises(ChatChannelBindingNotFound):
                service_a.verify(repo_a, binding_id=b_binding_id, code=MOCK_LINK_CODE)
        finally:
            reset_current(token)
        session.rollback()
