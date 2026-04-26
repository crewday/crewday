"""Integration tests for the ``dispatch_due_webhooks`` worker tick (cd-q885).

End-to-end coverage for
:func:`app.worker.tasks.webhook_dispatch.dispatch_due_webhooks`:

* Skip-when-root-key-missing — the dispatcher cannot decrypt
  subscription secrets without the root key, so the tick logs a
  warning and bails. Mirrors the cd-znv4 cipher's "no root key, no
  decrypt" posture.
* ``_select_due`` filter shape — a row in ``status='succeeded'`` or
  ``status='dead_lettered'`` is **not** picked up by a sweep, even
  if its ``next_attempt_at`` lies in the past. Idempotent on
  terminal rows by construction.
* Per-row UoW isolation — one bad row (e.g. its subscription is
  gone) does not roll back the sweep's progress on its peers. The
  outer loop swallows the per-row exception, counts it as a retry,
  logs it, and continues.

Domain coverage for the in-process state machine
(:func:`deliver`'s response classification, the retry schedule,
sign / verify) lives in :mod:`tests.domain.integrations.test_webhooks`
and :mod:`tests.integration.integrations.test_webhooks_delivery`;
this module covers the worker-tick wrapper layer those suites do
not exercise.

See ``docs/specs/10-messaging-notifications.md`` §"Webhooks
(outbound)" and ``docs/specs/16-deployment-operations.md``
§"Worker process".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.integrations.models import (
    WebhookDelivery,
    WebhookSubscription,
)
from app.adapters.db.integrations.repositories import (
    SqlAlchemyWebhookRepository,
)
from app.adapters.db.secrets.models import SecretEnvelope
from app.adapters.db.secrets.repositories import (
    SqlAlchemySecretEnvelopeRepository,
)
from app.adapters.db.workspace.models import Workspace
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.config import Settings, get_settings
from app.domain.integrations.webhooks import (
    DELIVERY_DEAD_LETTERED,
    DELIVERY_PENDING,
    DELIVERY_SUCCEEDED,
    create_subscription,
)
from app.tenancy.context import WorkspaceContext
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks.webhook_dispatch import dispatch_due_webhooks

pytestmark = pytest.mark.integration

_KEY = SecretStr("x" * 32)
_PINNED = datetime(2026, 4, 26, 18, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    ``dispatch_due_webhooks`` opens its own UoW via
    :func:`app.adapters.db.session.make_uow`; the worker has no
    ambient session. This fixture pins the default sessionmaker to
    the test engine so the dispatcher reads + writes against the
    rolled-back per-test transaction. Mirrors
    ``test_approval_ttl::real_make_uow``.
    """
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


@pytest.fixture
def clean_webhook_tables(engine: Engine) -> Iterator[None]:
    """Empty webhook + secret envelope tables before/after each test.

    The harness engine is session-scoped, so cross-test bleed would
    otherwise mask regressions (a stale dead-letter row from an earlier
    test would trivially pass "no rows due" assertions even if
    ``_select_due`` mis-filtered).
    """
    with engine.begin() as conn:
        conn.execute(delete(WebhookDelivery))
        conn.execute(delete(WebhookSubscription))
        conn.execute(delete(SecretEnvelope))
    yield
    with engine.begin() as conn:
        conn.execute(delete(WebhookDelivery))
        conn.execute(delete(WebhookSubscription))
        conn.execute(delete(SecretEnvelope))


def _bootstrap_workspace(engine: Engine) -> str:
    """Insert a :class:`Workspace` row and return its id.

    The webhook tables FK ``workspace_id`` and the FK is enforced on
    flush — seed before any subscription / delivery insert.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    ws_id = new_ulid()
    with factory() as session:
        session.add(
            Workspace(
                id=ws_id,
                slug=f"webhook-tick-{ws_id[-6:].lower()}",
                name="webhook tick fixture",
                plan="free",
                quota_json={},
                settings_json={},
                created_at=_PINNED,
            )
        )
        session.commit()
    return ws_id


def _ctx(ws_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=ws_id,
        workspace_slug="webhook-tick",
        actor_id="01HWA00000000000000000USR1",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _seed_subscription(engine: Engine, ws_id: str, *, url: str) -> str:
    """Create a subscription via the service layer; return its id.

    Routes through :func:`create_subscription` (rather than a raw
    INSERT) so the secret envelope row is written too — the
    dispatcher's decrypt path needs both halves.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    with factory() as session:
        secret_repo = SqlAlchemySecretEnvelopeRepository(session)
        envelope = Aes256GcmEnvelope(
            _KEY, repository=secret_repo, clock=FrozenClock(_PINNED)
        )
        repo = SqlAlchemyWebhookRepository(session)
        view = create_subscription(
            session,
            _ctx(ws_id),
            repo=repo,
            envelope=envelope,
            name="receiver",
            url=url,
            events=["task.completed"],
            clock=FrozenClock(_PINNED),
        )
        session.commit()
        return view.id


# ---------------------------------------------------------------------------
# Skip-when-root-key-missing
# ---------------------------------------------------------------------------


class TestSkipWhenRootKeyMissing:
    """The dispatcher bails noisily when ``settings.root_key`` is None.

    Without the root key the cipher cannot decrypt subscription
    secrets, and signing every body with an empty secret would emit
    forgeable signatures that any third party could mint. Failing
    closed at the tick boundary (rather than at decrypt time, after
    a wasted DB read) keeps the operator-facing log stream tight
    and prevents one fan-out worker from looping on the same
    decrypt error every 30 s.
    """

    def test_no_root_key_skips_tick_with_warning(
        self,
        real_make_uow: None,
        clean_webhook_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``settings.root_key=None`` short-circuits the tick.

        Pin the settings lookup to a value with no root key, run one
        tick, and assert: zero rows processed, the warning lands at
        WARNING with ``event=webhook.dispatch.skipped_no_root_key``,
        and no row state changes.
        """
        allow_propagated_log_capture("app.worker.tasks.webhook_dispatch")

        no_key_settings = Settings(
            database_url="sqlite:///nope",
            root_key=None,
        )
        monkeypatch.setattr(
            "app.worker.tasks.webhook_dispatch.get_settings",
            lambda: no_key_settings,
        )

        with caplog.at_level(
            logging.WARNING, logger="app.worker.tasks.webhook_dispatch"
        ):
            report = dispatch_due_webhooks(clock=FrozenClock(_PINNED))

        assert report.processed_count == 0
        assert report.successes == 0
        assert report.retries == 0
        assert report.dead_lettered == 0
        assert report.processed_ids == ()
        # Warning carries the canonical event tag.
        skipped = [
            r
            for r in caplog.records
            if getattr(r, "event", "") == "webhook.dispatch.skipped_no_root_key"
        ]
        assert len(skipped) == 1


# ---------------------------------------------------------------------------
# Terminal-row idempotency in ``_select_due``
# ---------------------------------------------------------------------------


class TestDueRowFilter:
    """``_select_due`` MUST exclude rows in terminal state.

    A succeeded / dead-lettered row whose ``next_attempt_at`` was
    never cleared (a programming bug, but defensible-in-depth)
    must not be re-fired by a tick. The dispatcher is the canonical
    writer of the state machine; refiring a terminal row would
    double-deliver a webhook the receiver already accepted (or
    keep retrying one we already gave up on).
    """

    def test_succeeded_row_with_past_next_attempt_is_skipped(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_webhook_tables: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``status='succeeded'`` row is not picked up even with an
        overdue ``next_attempt_at``.

        Hand-craft the row state so the test pins the predicate
        shape itself (``status='pending' AND next_attempt_at <=
        now``) — a regression that drops the status filter would
        immediately re-fire the row.
        """
        # Pin ``settings.root_key`` so the no-root-key skip does
        # not short-circuit the tick before the predicate runs.
        monkeypatch.setenv("CREWDAY_ROOT_KEY", "x" * 32)
        get_settings.cache_clear()
        ws_id = _bootstrap_workspace(engine)
        sub_id = _seed_subscription(engine, ws_id, url="http://127.0.0.1:1/never")

        # Force the dispatcher to skip the actual HTTP path so we
        # only exercise ``_select_due``: stub ``deliver`` to fail
        # noisily if it's ever called.
        called: list[str] = []

        def _spy(_session: Session, **kwargs: object) -> object:
            called.append(str(kwargs.get("delivery_id")))
            raise AssertionError("deliver must not be called for terminal rows")

        monkeypatch.setattr(
            "app.domain.integrations.webhooks.deliver",
            _spy,
        )
        # ``_dispatch_one`` imports deliver via the fully-qualified
        # path; patch the same name on the worker module too.
        monkeypatch.setattr(
            "app.worker.tasks.webhook_dispatch.deliver",
            _spy,
        )

        # Insert a terminal row directly so we don't run a tick
        # before the assertion.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        install_tenant_filter(factory)
        succeeded_id = new_ulid()
        dead_lettered_id = new_ulid()
        pending_id = new_ulid()
        past = _PINNED - timedelta(hours=1)
        with factory() as session:
            session.add(
                WebhookDelivery(
                    id=succeeded_id,
                    workspace_id=ws_id,
                    subscription_id=sub_id,
                    event="task.completed",
                    payload_json={"event": "task.completed", "data": {}},
                    status=DELIVERY_SUCCEEDED,
                    attempt=1,
                    next_attempt_at=past,  # Past — would trip the time filter.
                    last_status_code=200,
                    last_error=None,
                    last_attempted_at=past,
                    succeeded_at=past,
                    dead_lettered_at=None,
                    replayed_from_id=None,
                    created_at=past,
                )
            )
            session.add(
                WebhookDelivery(
                    id=dead_lettered_id,
                    workspace_id=ws_id,
                    subscription_id=sub_id,
                    event="task.completed",
                    payload_json={"event": "task.completed", "data": {}},
                    status=DELIVERY_DEAD_LETTERED,
                    attempt=6,
                    next_attempt_at=past,  # Past — would trip the time filter.
                    last_status_code=500,
                    last_error="http_500",
                    last_attempted_at=past,
                    succeeded_at=None,
                    dead_lettered_at=past,
                    replayed_from_id=None,
                    created_at=past,
                )
            )
            # Plus one genuinely pending row so the tick sees something
            # to do; the spy raises if we hand it any of the three ids.
            # The spy is wired only when the deliver function is
            # called — a clean ``pending`` row would currently fire
            # the spy and break the test, so we land it with
            # ``next_attempt_at`` in the future to keep it out of
            # the sweep.
            session.add(
                WebhookDelivery(
                    id=pending_id,
                    workspace_id=ws_id,
                    subscription_id=sub_id,
                    event="task.completed",
                    payload_json={"event": "task.completed", "data": {}},
                    status=DELIVERY_PENDING,
                    attempt=0,
                    next_attempt_at=_PINNED + timedelta(hours=1),  # Future.
                    last_status_code=None,
                    last_error=None,
                    last_attempted_at=None,
                    succeeded_at=None,
                    dead_lettered_at=None,
                    replayed_from_id=None,
                    created_at=past,
                )
            )
            session.commit()

        report = dispatch_due_webhooks(clock=FrozenClock(_PINNED))
        # Neither terminal row should have been touched.
        assert called == []
        assert report.processed_count == 0


# ---------------------------------------------------------------------------
# Per-row UoW isolation
# ---------------------------------------------------------------------------


class TestPerRowUoWIsolation:
    """A failing row must not roll back the sweep.

    The dispatcher's per-row UoW pattern is a deliberate
    architectural choice: the alternative ("one big UoW for the
    whole sweep") would let one bad subscription invalidate every
    other delivery the tick was about to make. The wrapper logs
    + counts the per-row failure but the loop continues.
    """

    def test_one_failing_row_does_not_abort_sweep(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_webhook_tables: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """One row's :func:`deliver` raises; the other row still flips.

        Stub :func:`deliver` so the first id raises and the second id
        returns a successful :class:`DeliveryReport`. Run one tick;
        assert both ids are in ``processed_ids`` and the ok row is
        counted as a success.
        """
        # Pin ``settings.root_key`` so ``dispatch_due_webhooks`` does
        # not short-circuit on the no-root-key skip; the deliver
        # stub never actually decrypts anything but the gate fires
        # before the loop.
        monkeypatch.setenv("CREWDAY_ROOT_KEY", "x" * 32)
        get_settings.cache_clear()
        allow_propagated_log_capture("app.worker.tasks.webhook_dispatch")

        ws_id = _bootstrap_workspace(engine)
        sub_id = _seed_subscription(engine, ws_id, url="http://127.0.0.1:1/never")

        bad_id = new_ulid()
        good_id = new_ulid()
        # Lexicographic ULID order is monotonic — pin the order so
        # the dispatcher visits ``bad`` first by stamping its
        # ``next_attempt_at`` ealier than ``good``'s.
        bad_due = _PINNED - timedelta(seconds=10)
        good_due = _PINNED - timedelta(seconds=5)

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        install_tenant_filter(factory)
        with factory() as session:
            for delivery_id, due in ((bad_id, bad_due), (good_id, good_due)):
                session.add(
                    WebhookDelivery(
                        id=delivery_id,
                        workspace_id=ws_id,
                        subscription_id=sub_id,
                        event="task.completed",
                        payload_json={"event": "task.completed", "data": {}},
                        status=DELIVERY_PENDING,
                        attempt=0,
                        next_attempt_at=due,
                        last_status_code=None,
                        last_error=None,
                        last_attempted_at=None,
                        succeeded_at=None,
                        dead_lettered_at=None,
                        replayed_from_id=None,
                        created_at=due,
                    )
                )
            session.commit()

        from app.domain.integrations.webhooks import DeliveryReport

        def _split(_session: Session, **kwargs: object) -> DeliveryReport:
            delivery_id = kwargs.get("delivery_id")
            if delivery_id == bad_id:
                raise RuntimeError("simulated row failure")
            assert delivery_id == good_id
            return DeliveryReport(
                delivery_id=str(delivery_id),
                status=DELIVERY_SUCCEEDED,
                attempt=1,
                last_status_code=200,
                last_error=None,
                dead_lettered=False,
            )

        monkeypatch.setattr("app.worker.tasks.webhook_dispatch.deliver", _split)

        with caplog.at_level(logging.ERROR, logger="app.worker.tasks.webhook_dispatch"):
            report = dispatch_due_webhooks(clock=FrozenClock(_PINNED))

        # Both rows visited; outcomes counted independently.
        assert set(report.processed_ids) == {bad_id, good_id}
        assert report.processed_count == 2
        assert report.successes == 1
        # The bad row gets counted as a retry-side event (the
        # exception was swallowed).
        assert report.retries == 1
        # The error landed in the structured log stream.
        row_errors = [
            r
            for r in caplog.records
            if getattr(r, "event", "") == "webhook.dispatch.row_error"
        ]
        assert len(row_errors) == 1
        assert getattr(row_errors[0], "delivery_id", None) == bad_id
