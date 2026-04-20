"""Unit tests for :mod:`app.domain.tasks.oneoff`.

Mirrors the in-memory SQLite bootstrap in
``tests/unit/tasks/test_schedules.py`` and ``test_generator.py``:
fresh engine per test, pull every sibling ``models`` module onto
the shared ``Base.metadata``, run ``Base.metadata.create_all``,
drive the domain code with :class:`FrozenClock` + a fresh
:class:`EventBus`.

Covers cd-0rf:

* Template-backed happy path — title / description / priority /
  photo_evidence / duration / linked_instructions / inventory all
  copied from the template; payload overrides win.
* Template-less happy path — explicit title, sane defaults on the
  other fields.
* Past ``scheduled_for_local`` → ``state = 'pending'``; future →
  ``state = 'scheduled'``.
* ``is_personal=True`` requires ``assigned_user_id == ctx.actor_id``
  and raises :class:`PersonalAssignmentError` on a mismatch.
* Checklist hook fires with ``is_ad_hoc=True`` when a template is
  set; not called when template-less.
* Assignment hook fires when ``assigned_user_id is None`` **and**
  ``expected_role_id`` is set; the chosen id lands on the row and
  drives the second ``task.assigned`` event.
* Unknown / soft-deleted template → :class:`TaskTemplateNotFound`.
* Audit row lands with ``entity_kind='task'``, ``action =
  'task.create_oneoff'`` and ``diff['after']`` carries the resolved
  fields.
* Events: ``task.created`` always fires; ``task.assigned`` fires
  iff ``assigned_user_id`` is non-null at the end of the flow.
* Permission: ``tasks.create`` resolver is invoked; a non-owner /
  non-worker / non-manager caller is rejected.
* Cross-workspace isolation: a template in workspace A can't be
  referenced from workspace B.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Task kinds"
(kind 1 — One-off), §"Self-created and personal tasks",
§"Natural-language intake (agent)".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence, TaskTemplate
from app.adapters.db.workspace.models import Workspace
from app.domain.tasks.oneoff import (
    PersonalAssignmentError,
    TaskCreate,
    TaskTemplateNotFound,
    TaskView,
    create_oneoff,
)
from app.events.bus import EventBus
from app.events.types import TaskAssigned, TaskCreated
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    import importlib
    import pkgutil

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
    """In-memory SQLite engine, schema created from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Fresh session per test; no tenant filter installed here."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def bus() -> EventBus:
    """Fresh in-process bus per test so subscriptions don't leak."""
    return EventBus()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------


_ACTOR_ID = "01HWA00000000000000000USR1"
_OTHER_USER = "01HWA00000000000000000USR2"


def _ctx(
    workspace_id: str,
    *,
    slug: str = "ws",
    actor_id: str = _ACTOR_ID,
    was_owner: bool = True,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=was_owner,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _bootstrap_workspace(session: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _bootstrap_property(session: Session, *, timezone: str = "Europe/Paris") -> str:
    prop_id = new_ulid()
    session.add(
        Property(
            id=prop_id,
            address="1 Villa Sud Way",
            timezone=timezone,
            tags_json=[],
            created_at=_PINNED,
        )
    )
    session.flush()
    return prop_id


def _bootstrap_user(session: Session, *, email: str, user_id: str | None = None) -> str:
    from app.adapters.db.identity.models import User

    uid = user_id if user_id is not None else new_ulid()
    session.add(
        User(
            id=uid,
            email=email,
            email_lower=email.lower(),
            display_name=email.split("@")[0],
            locale=None,
            timezone=None,
            avatar_blob_hash=None,
            created_at=_PINNED,
            last_login_at=None,
        )
    )
    session.flush()
    return uid


def _bootstrap_template(
    session: Session,
    *,
    workspace_id: str,
    name: str = "Villa Sud pool",
    description_md: str = "Clean the pool edges and skim surface",
    priority: str = "high",
    photo_evidence: str = "required",
    duration_minutes: int = 45,
    linked_instruction_ids: list[str] | None = None,
    inventory_consumption_json: dict[str, int] | None = None,
) -> TaskTemplate:
    tpl = TaskTemplate(
        id=new_ulid(),
        workspace_id=workspace_id,
        title=name,
        name=name,
        description_md=description_md,
        default_duration_min=duration_minutes,
        duration_minutes=duration_minutes,
        required_evidence="photo" if photo_evidence == "required" else "none",
        photo_required=(photo_evidence == "required"),
        default_assignee_role=None,
        role_id="role-housekeeper",
        property_scope="any",
        listed_property_ids=[],
        area_scope="any",
        listed_area_ids=[],
        checklist_template_json=[],
        photo_evidence=photo_evidence,
        linked_instruction_ids=linked_instruction_ids or ["ins-1"],
        priority=priority,
        inventory_consumption_json=inventory_consumption_json or {"sku-towel": 2},
        llm_hints_md=None,
        created_at=_PINNED,
    )
    session.add(tpl)
    session.flush()
    return tpl


def _bootstrap_actor(
    session: Session,
    *,
    workspace_id: str,
    grant_role: str = "manager",
    user_id: str = _ACTOR_ID,
) -> None:
    """Insert the canonical test-actor user + a matching role grant.

    Every ``create_oneoff`` call writes ``created_by_user_id =
    ctx.actor_id`` and that FK is ``ON DELETE SET NULL`` — the unit-
    test engine has FKs on, so we need the row to exist before we
    flush.

    The service also re-asserts the ``tasks.create`` action via
    :mod:`app.authz.enforce`. That check walks the system-groups
    list (``owners`` / ``managers`` / ``all_workers``) and needs
    either an owners-group membership row or a matching
    :class:`RoleGrant`. We seed a ``grant_role='manager'`` grant by
    default so the default ctx flows through the check; tests that
    want to exercise the denial path pass a different ``grant_role``
    (or skip the bootstrap).
    """
    from app.adapters.db.authz.models import RoleGrant

    _bootstrap_user(session, email=f"{user_id}@example.com", user_id=user_id)
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    session.flush()


def _future_local(hours: int = 4) -> str:
    """ISO-8601 property-local timestamp somewhere in the future.

    The property timezone is Europe/Paris (UTC+2 in April 2026), so
    the pinned UTC ``12:00Z`` on 2026-04-19 is local 14:00 Paris.
    A +4h delta lands at ``2026-04-19T18:00`` local — comfortably
    after ``now`` in every frame.
    """
    return f"2026-04-19T{14 + hours:02d}:00"


def _past_local(hours: int = 4) -> str:
    """ISO-8601 property-local timestamp in the past relative to ``now``."""
    h = 14 - hours
    return f"2026-04-19T{h:02d}:00"


# ---------------------------------------------------------------------------
# DTO validation
# ---------------------------------------------------------------------------


class TestDTO:
    """``TaskCreate`` enforces shape rules before the service runs."""

    def test_template_less_requires_title(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskCreate.model_validate({"scheduled_for_local": _future_local()})
        assert "title" in str(exc.value)

    def test_template_less_with_title_accepted(self) -> None:
        body = TaskCreate.model_validate(
            {
                "title": "Call Maria back",
                "scheduled_for_local": _future_local(),
            }
        )
        assert body.title == "Call Maria back"
        assert body.template_id is None

    def test_template_backed_omits_title(self) -> None:
        body = TaskCreate.model_validate(
            {
                "template_id": "tmpl-1",
                "scheduled_for_local": _future_local(),
            }
        )
        assert body.title is None
        assert body.template_id == "tmpl-1"

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TaskCreate.model_validate(
                {
                    "title": "x",
                    "scheduled_for_local": _future_local(),
                    "extra": "nope",
                }
            )

    def test_duration_range(self) -> None:
        with pytest.raises(ValidationError):
            TaskCreate.model_validate(
                {
                    "title": "x",
                    "scheduled_for_local": _future_local(),
                    "duration_minutes": 0,
                }
            )
        with pytest.raises(ValidationError):
            TaskCreate.model_validate(
                {
                    "title": "x",
                    "scheduled_for_local": _future_local(),
                    "duration_minutes": 24 * 60 + 1,
                }
            )

    def test_empty_scheduled_for_local_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TaskCreate.model_validate({"title": "x", "scheduled_for_local": ""})

    def test_title_only_whitespace_rejected(self) -> None:
        """Template-less requires a non-blank title."""
        with pytest.raises(ValidationError):
            TaskCreate.model_validate(
                {"title": "   ", "scheduled_for_local": _future_local()}
            )

    def test_template_backed_whitespace_title_rejected(self) -> None:
        """Explicit whitespace title on the template-backed path is rejected.

        Regression guard (cd-057z): a whitespace-only override would
        otherwise silently replace the template's name with an empty
        string in the row.
        """
        with pytest.raises(ValidationError):
            TaskCreate.model_validate(
                {
                    "template_id": "tmpl-1",
                    "title": "   ",
                    "scheduled_for_local": _future_local(),
                }
            )

    def test_invalid_priority_rejected(self) -> None:
        """The ``priority`` ``Literal`` narrows unknown values on ingress."""
        with pytest.raises(ValidationError):
            TaskCreate.model_validate(
                {
                    "title": "x",
                    "scheduled_for_local": _future_local(),
                    "priority": "bogus",
                }
            )

    def test_invalid_photo_evidence_rejected(self) -> None:
        """The ``photo_evidence`` ``Literal`` narrows unknown values."""
        with pytest.raises(ValidationError):
            TaskCreate.model_validate(
                {
                    "title": "x",
                    "scheduled_for_local": _future_local(),
                    "photo_evidence": "nope",
                }
            )

    def test_inventory_requires_positive(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskCreate.model_validate(
                {
                    "title": "x",
                    "scheduled_for_local": _future_local(),
                    "inventory_consumption_json": {"sku-a": 0},
                }
            )
        assert "positive integer" in str(exc.value)


# ---------------------------------------------------------------------------
# Template-backed happy path
# ---------------------------------------------------------------------------


class TestTemplateBacked:
    """``template_id`` set → copy template fields, override with payload."""

    def test_copies_template_fields(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="tb-copy")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        ctx = _ctx(ws, slug="tb-copy")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "template_id": tpl.id,
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        assert isinstance(view, TaskView)
        assert view.title == "Villa Sud pool"
        assert view.description_md == "Clean the pool edges and skim surface"
        assert view.priority == "high"
        assert view.photo_evidence == "required"
        assert view.duration_minutes == 45
        assert view.linked_instruction_ids == ("ins-1",)
        assert view.inventory_consumption_json == {"sku-towel": 2}
        assert view.template_id == tpl.id
        assert view.created_by == _ACTOR_ID
        assert view.is_personal is False

    def test_payload_overrides_template(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="tb-over")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        ctx = _ctx(ws, slug="tb-over")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "template_id": tpl.id,
                    "title": "Override title",
                    "priority": "urgent",
                    "duration_minutes": 15,
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        assert view.title == "Override title"
        assert view.priority == "urgent"
        assert view.duration_minutes == 15
        # Non-overridden fields still come from the template.
        assert view.photo_evidence == "required"

    def test_explicit_empty_lists_win_over_template(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        """An explicit ``[]`` / ``{}`` override disables the template defaults."""
        ws = _bootstrap_workspace(session, slug="tb-empty")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        ctx = _ctx(ws, slug="tb-empty")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "template_id": tpl.id,
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                    "linked_instruction_ids": [],
                    "inventory_consumption_json": {},
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        assert view.linked_instruction_ids == ()
        assert view.inventory_consumption_json == {}

    def test_unknown_template_rejected(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="tb-404")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="tb-404")

        with pytest.raises(TaskTemplateNotFound):
            create_oneoff(
                session,
                ctx,
                payload=TaskCreate.model_validate(
                    {
                        "template_id": "tmpl-nonexistent",
                        "property_id": prop_id,
                        "scheduled_for_local": _future_local(),
                    }
                ),
                clock=clock,
                event_bus=bus,
            )

    def test_soft_deleted_template_rejected(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="tb-del")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        tpl.deleted_at = _PINNED
        session.flush()
        ctx = _ctx(ws, slug="tb-del")

        with pytest.raises(TaskTemplateNotFound):
            create_oneoff(
                session,
                ctx,
                payload=TaskCreate.model_validate(
                    {
                        "template_id": tpl.id,
                        "property_id": prop_id,
                        "scheduled_for_local": _future_local(),
                    }
                ),
                clock=clock,
                event_bus=bus,
            )

    def test_cross_workspace_template_hidden(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="tb-iso-a")
        ws_b = _bootstrap_workspace(session, slug="tb-iso-b")
        _bootstrap_actor(session, workspace_id=ws_b)
        prop_id = _bootstrap_property(session)
        tpl_a = _bootstrap_template(session, workspace_id=ws_a)
        ctx_b = _ctx(ws_b, slug="tb-iso-b")

        with pytest.raises(TaskTemplateNotFound):
            create_oneoff(
                session,
                ctx_b,
                payload=TaskCreate.model_validate(
                    {
                        "template_id": tpl_a.id,
                        "property_id": prop_id,
                        "scheduled_for_local": _future_local(),
                    }
                ),
                clock=clock,
                event_bus=bus,
            )


# ---------------------------------------------------------------------------
# Template-less happy path
# ---------------------------------------------------------------------------


class TestTemplateLess:
    """``template_id`` None → explicit payload drives every field."""

    def test_happy_path_with_defaults(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="tl-defaults")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="tl-defaults")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Call Maria back",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        assert view.title == "Call Maria back"
        assert view.description_md is None
        assert view.priority == "normal"
        assert view.photo_evidence == "disabled"
        assert view.duration_minutes is None
        assert view.linked_instruction_ids == ()
        assert view.inventory_consumption_json == {}
        assert view.template_id is None

    def test_explicit_fields_land(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="tl-explicit")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="tl-explicit")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Sweep porch",
                    "description_md": "Front + side.",
                    "priority": "low",
                    "property_id": prop_id,
                    "area_id": "area-porch",
                    "unit_id": "unit-main",
                    "expected_role_id": "role-worker",
                    "scheduled_for_local": _future_local(),
                    "duration_minutes": 20,
                    "photo_evidence": "optional",
                    "linked_instruction_ids": ["ins-1", "ins-2"],
                    "inventory_consumption_json": {"sku-broom": 1},
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        assert view.description_md == "Front + side."
        assert view.priority == "low"
        assert view.duration_minutes == 20
        assert view.photo_evidence == "optional"
        assert view.linked_instruction_ids == ("ins-1", "ins-2")
        assert view.inventory_consumption_json == {"sku-broom": 1}
        assert view.area_id == "area-porch"
        assert view.unit_id == "unit-main"
        assert view.expected_role_id == "role-worker"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class TestState:
    """§06 state machine: past ``scheduled_for_local`` → ``pending``."""

    def test_future_lands_in_scheduled(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="st-future")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="st-future")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Later today",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(hours=4),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        assert view.state == "scheduled"

    def test_past_lands_in_pending(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        """A one-off with a past ``scheduled_for_local`` is immediately actionable."""
        ws = _bootstrap_workspace(session, slug="st-past")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="st-past")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Should have done earlier",
                    "property_id": prop_id,
                    "scheduled_for_local": _past_local(hours=6),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        assert view.state == "pending"

    def test_exactly_now_is_pending(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        """Boundary: ``scheduled_for_local == now`` → pending (``<=`` in the spec)."""
        ws = _bootstrap_workspace(session, slug="st-now")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="st-now")

        # Pinned UTC now is 2026-04-19T12:00 UTC = 14:00 Europe/Paris.
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Right now",
                    "property_id": prop_id,
                    "scheduled_for_local": "2026-04-19T14:00",
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        assert view.state == "pending"


# ---------------------------------------------------------------------------
# Personal tasks
# ---------------------------------------------------------------------------


class TestPersonal:
    """``is_personal=True`` requires self-assignment per §06."""

    def test_self_assigned_personal_accepted(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="per-ok")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="per-ok")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Personal errand",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                    "is_personal": True,
                    "assigned_user_id": _ACTOR_ID,
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        assert view.is_personal is True
        assert view.assigned_user_id == _ACTOR_ID

    def test_personal_requires_assignee(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="per-noassign")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="per-noassign")

        with pytest.raises(PersonalAssignmentError):
            create_oneoff(
                session,
                ctx,
                payload=TaskCreate.model_validate(
                    {
                        "title": "Personal",
                        "property_id": prop_id,
                        "scheduled_for_local": _future_local(),
                        "is_personal": True,
                    }
                ),
                clock=clock,
                event_bus=bus,
            )

    def test_personal_rejects_other_assignee(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="per-mismatch")
        _bootstrap_actor(session, workspace_id=ws)
        _bootstrap_user(session, email="other@example.com", user_id=_OTHER_USER)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="per-mismatch")

        with pytest.raises(PersonalAssignmentError):
            create_oneoff(
                session,
                ctx,
                payload=TaskCreate.model_validate(
                    {
                        "title": "Personal",
                        "property_id": prop_id,
                        "scheduled_for_local": _future_local(),
                        "is_personal": True,
                        "assigned_user_id": _OTHER_USER,
                    }
                ),
                clock=clock,
                event_bus=bus,
            )


# ---------------------------------------------------------------------------
# Checklist hook
# ---------------------------------------------------------------------------


class TestChecklistHook:
    """The hook fires with ``is_ad_hoc=True`` when a template is set."""

    def test_hook_called_on_template_backed(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="cl-fire")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        ctx = _ctx(ws, slug="cl-fire")

        calls: list[tuple[str, str, bool]] = []

        def hook(
            sess: Session,
            context: WorkspaceContext,
            occurrence_id: str,
            template: TaskTemplate,
            is_ad_hoc: bool,
        ) -> None:
            _ = sess, context
            calls.append((occurrence_id, template.id, is_ad_hoc))

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "template_id": tpl.id,
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
            expand_checklist=hook,
        )

        assert calls == [(view.id, tpl.id, True)]

    def test_hook_not_called_template_less(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="cl-skip")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="cl-skip")

        calls: list[object] = []

        def hook(*args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))

        create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "No template",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
            expand_checklist=hook,
        )

        assert calls == []


# ---------------------------------------------------------------------------
# Assignment hook
# ---------------------------------------------------------------------------


class TestAssignmentHook:
    """Hook fires only when ``assigned_user_id is None`` + role is set."""

    def test_hook_fires_and_assigns(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="as-fire")
        _bootstrap_actor(session, workspace_id=ws)
        picked_user = _bootstrap_user(session, email="picked@example.com")
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="as-fire")

        captured_calls: list[str] = []

        def hook(
            sess: Session,
            context: WorkspaceContext,
            occurrence_id: str,
        ) -> str | None:
            _ = sess, context
            captured_calls.append(occurrence_id)
            return picked_user

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Needs someone",
                    "property_id": prop_id,
                    "expected_role_id": "role-worker",
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
            assign=hook,
        )

        assert captured_calls == [view.id]
        assert view.assigned_user_id == picked_user

    def test_hook_skipped_when_already_assigned(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="as-skip-assigned")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="as-skip-assigned")

        calls: list[object] = []

        def hook(
            sess: Session,
            context: WorkspaceContext,
            occurrence_id: str,
        ) -> str | None:
            _ = sess, context
            calls.append(occurrence_id)
            return None

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Already set",
                    "property_id": prop_id,
                    "assigned_user_id": _ACTOR_ID,
                    "expected_role_id": "role-worker",
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
            assign=hook,
        )

        assert calls == []
        assert view.assigned_user_id == _ACTOR_ID

    def test_hook_skipped_when_no_role(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="as-skip-no-role")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="as-skip-no-role")

        calls: list[object] = []

        def hook(
            sess: Session,
            context: WorkspaceContext,
            occurrence_id: str,
        ) -> str | None:
            _ = sess, context
            calls.append(occurrence_id)
            return _ACTOR_ID

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "No role",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
            assign=hook,
        )

        assert calls == []
        assert view.assigned_user_id is None

    def test_hook_returning_none_leaves_unassigned(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="as-none")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="as-none")

        def hook(
            sess: Session,
            context: WorkspaceContext,
            occurrence_id: str,
        ) -> str | None:
            _ = sess, context, occurrence_id
            return None

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Unassignable",
                    "property_id": prop_id,
                    "expected_role_id": "role-worker",
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
            assign=hook,
        )

        assert view.assigned_user_id is None


# ---------------------------------------------------------------------------
# Audit + events
# ---------------------------------------------------------------------------


class TestAudit:
    """One ``task.create_oneoff`` audit row per successful create."""

    def test_audit_row_written(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="au-happy")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="au-happy")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "For audit",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        audits = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).all()
        assert len(audits) == 1
        audit = audits[0]
        assert audit.entity_kind == "task"
        assert audit.action == "task.create_oneoff"
        assert audit.workspace_id == ws
        assert audit.diff["after"]["id"] == view.id
        assert audit.diff["after"]["title"] == "For audit"


class TestEvents:
    """``task.created`` always fires; ``task.assigned`` fires on assignment."""

    def test_task_created_fires(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="ev-created")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="ev-created")

        captured: list[TaskCreated] = []

        @bus.subscribe(TaskCreated)
        def _on(event: TaskCreated) -> None:
            captured.append(event)

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Has event",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        assert len(captured) == 1
        assert captured[0].task_id == view.id
        assert captured[0].workspace_id == ws
        assert captured[0].actor_id == ctx.actor_id

    def test_task_assigned_not_fired_when_unassigned(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="ev-noassign")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="ev-noassign")

        captured: list[TaskAssigned] = []

        @bus.subscribe(TaskAssigned)
        def _on(event: TaskAssigned) -> None:
            captured.append(event)

        create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "No assignee",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        assert captured == []

    def test_task_assigned_fires_on_explicit_assignee(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="ev-assign-explicit")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="ev-assign-explicit")

        captured: list[TaskAssigned] = []

        @bus.subscribe(TaskAssigned)
        def _on(event: TaskAssigned) -> None:
            captured.append(event)

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Explicit assignee",
                    "property_id": prop_id,
                    "assigned_user_id": _ACTOR_ID,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        assert len(captured) == 1
        assert captured[0].task_id == view.id
        assert captured[0].assigned_to == _ACTOR_ID

    def test_task_assigned_fires_after_hook_picks(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="ev-assign-hook")
        _bootstrap_actor(session, workspace_id=ws)
        picked = _bootstrap_user(session, email="picked@example.com")
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="ev-assign-hook")

        captured: list[TaskAssigned] = []

        @bus.subscribe(TaskAssigned)
        def _on(event: TaskAssigned) -> None:
            captured.append(event)

        def hook(
            sess: Session,
            context: WorkspaceContext,
            occurrence_id: str,
        ) -> str | None:
            _ = sess, context, occurrence_id
            return picked

        create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Hook assigns",
                    "property_id": prop_id,
                    "expected_role_id": "role-worker",
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
            assign=hook,
        )

        assert len(captured) == 1
        assert captured[0].assigned_to == picked


# ---------------------------------------------------------------------------
# Row shape on insert
# ---------------------------------------------------------------------------


class TestPermission:
    """``tasks.create`` resolver is invoked; denial bubbles up as an error."""

    def test_guest_denied(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        from app.authz.enforce import PermissionDenied

        ws = _bootstrap_workspace(session, slug="perm-guest")
        # Seed the actor as a *guest* — the action catalog lists
        # only ``owners``, ``managers``, and ``all_workers`` in
        # ``tasks.create``'s ``default_allow``, so a guest grant
        # falls through to a denial.
        _bootstrap_actor(session, workspace_id=ws, grant_role="guest")
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="perm-guest", was_owner=False)

        with pytest.raises(PermissionDenied):
            create_oneoff(
                session,
                ctx,
                payload=TaskCreate.model_validate(
                    {
                        "title": "Denied",
                        "property_id": prop_id,
                        "scheduled_for_local": _future_local(),
                    }
                ),
                clock=clock,
                event_bus=bus,
            )

    def test_worker_allowed(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        """Workers hold the ``tasks.create`` capability per §05."""
        ws = _bootstrap_workspace(session, slug="perm-worker")
        _bootstrap_actor(session, workspace_id=ws, grant_role="worker")
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="perm-worker", was_owner=False)

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Worker creates",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        assert view.id


class TestDuration:
    """``ends_at`` derivation for the three duration branches."""

    def test_duration_from_payload_lands_on_ends_at(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="dur-payload")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="dur-payload")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Explicit 45 min",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                    "duration_minutes": 45,
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        assert view.duration_minutes == 45
        row = session.scalars(select(Occurrence).where(Occurrence.id == view.id)).one()
        delta_min = int((row.ends_at - row.starts_at).total_seconds() // 60)
        assert delta_min == 45

    def test_missing_duration_defaults_to_thirty_minutes(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        """Template-less task without ``duration_minutes`` falls back to 30 min.

        Regression guard (cd-057z): the earlier 1-minute placeholder
        left ``ends_at = starts_at + 1min`` on the row, which the
        §06 ``ends_at - starts_at`` reader fallback would surface as
        a ludicrously short task.
        """
        ws = _bootstrap_workspace(session, slug="dur-default")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="dur-default")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "No duration",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        # Row ``duration_minutes`` stays NULL (caller never provided
        # one and there's no template) — the §06 fallback is
        # ``ends_at - starts_at``.
        assert view.duration_minutes is None
        row = session.scalars(select(Occurrence).where(Occurrence.id == view.id)).one()
        delta_min = int((row.ends_at - row.starts_at).total_seconds() // 60)
        assert delta_min == 30


class TestRowShape:
    """Sanity-check the Occurrence row the service writes."""

    def test_row_carries_created_by_and_is_personal(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="row-shape")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="row-shape")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Shape check",
                    "property_id": prop_id,
                    "is_personal": True,
                    "assigned_user_id": _ACTOR_ID,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        row = session.scalars(select(Occurrence).where(Occurrence.id == view.id)).one()
        assert row.created_by_user_id == _ACTOR_ID
        assert row.is_personal is True
        assert row.schedule_id is None
        assert row.scheduled_for_local == view.scheduled_for_local
        assert row.originally_scheduled_for == view.scheduled_for_local

    def test_view_is_frozen_and_slotted(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="view-frozen")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="view-frozen")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "x",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            view.title = "changed"  # type: ignore[misc]
        with pytest.raises((AttributeError, TypeError)):
            view.extra = "nope"  # type: ignore[attr-defined]
