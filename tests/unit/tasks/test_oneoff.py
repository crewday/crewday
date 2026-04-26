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
from app.adapters.db.places.models import Area, Property, PropertyWorkspace, Unit
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence, TaskTemplate
from app.adapters.db.workspace.models import WorkRole, Workspace
from app.domain.tasks.oneoff import (
    InvalidLocalDatetime,
    PersonalAssignmentError,
    TaskCreate,
    TaskFieldInvalid,
    TaskNotFound,
    TaskPatch,
    TaskTemplateNotFound,
    TaskView,
    create_oneoff,
    read_task,
    update_task,
)
from app.events.bus import EventBus
from app.events.types import TaskAssigned, TaskCreated, TaskUpdated
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


def _link_property_to_workspace(
    session: Session, *, property_id: str, workspace_id: str
) -> None:
    """Bind a property to a workspace via :class:`PropertyWorkspace`.

    The PATCH-time validation ``_assert_property_in_workspace``
    queries the junction table — a property without the link is a
    cross-tenant reference and rejected as
    :class:`TaskFieldInvalid`. Tests that exercise the happy
    property-patch path bootstrap the link explicitly through this
    helper.
    """
    session.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace_id,
            label="test",
            membership_role="owner_workspace",
            created_at=_PINNED,
        )
    )
    session.flush()


def _bootstrap_area(session: Session, *, property_id: str, label: str = "Pool") -> str:
    """Insert an :class:`Area` row scoped to ``property_id``."""
    area_id = new_ulid()
    session.add(
        Area(
            id=area_id,
            property_id=property_id,
            label=label,
            icon=None,
            ordering=0,
            created_at=_PINNED,
        )
    )
    session.flush()
    return area_id


def _bootstrap_unit(
    session: Session,
    *,
    property_id: str,
    label: str = "Apt 1",
    type_: str = "apartment",
) -> str:
    """Insert a :class:`Unit` row scoped to ``property_id``."""
    unit_id = new_ulid()
    session.add(
        Unit(
            id=unit_id,
            property_id=property_id,
            label=label,
            type=type_,
            capacity=2,
            created_at=_PINNED,
        )
    )
    session.flush()
    return unit_id


def _bootstrap_work_role(
    session: Session,
    *,
    workspace_id: str,
    key: str = "maid",
    name: str = "Maid",
) -> str:
    """Insert a live :class:`WorkRole` row in the workspace."""
    role_id = new_ulid()
    session.add(
        WorkRole(
            id=role_id,
            workspace_id=workspace_id,
            key=key,
            name=name,
            description_md="",
            default_settings_json={},
            icon_name="",
            created_at=_PINNED,
        )
    )
    session.flush()
    return role_id


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


# ---------------------------------------------------------------------------
# Read + partial-update path (cd-sn26 HTTP seam)
# ---------------------------------------------------------------------------


class TestReadTask:
    """``read_task`` returns a :class:`TaskView` for every §06 state."""

    @pytest.mark.parametrize(
        "state",
        # ``overdue`` is accepted by the view's Literal, but the DB
        # CHECK constraint on ``occurrence.state`` does not yet carry
        # it (the widening migration is tracked alongside cd-7am7).
        # Excluded here so the FK / CHECK layer doesn't reject the
        # force-set in the test fixture.
        ["scheduled", "pending", "in_progress", "done", "skipped", "cancelled"],
    )
    def test_every_state_round_trips(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        state: str,
    ) -> None:
        """Regression for cd-me3q: the narrowed Literal covers every
        state-machine name, not just the two that :func:`create_oneoff`
        stamps at insert time."""
        ws = _bootstrap_workspace(session, slug=f"read-{state}")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug=f"read-{state}")

        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": f"{state} task",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        # Force-set the row into the state under test.
        row = session.scalars(select(Occurrence).where(Occurrence.id == view.id)).one()
        row.state = state
        session.flush()

        refreshed = read_task(session, ctx, task_id=view.id)
        assert refreshed.state == state

    def test_unknown_id_raises(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session, slug="read-missing")
        _bootstrap_actor(session, workspace_id=ws)
        ctx = _ctx(ws, slug="read-missing")
        with pytest.raises(TaskNotFound):
            read_task(session, ctx, task_id="01UNKNOWN000000000000000000")

    def test_cross_tenant_raises_not_found(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """A task in workspace A is 404 for the workspace B caller —
        the service never leaks the row."""
        ws_a = _bootstrap_workspace(session, slug="read-a")
        ws_b = _bootstrap_workspace(session, slug="read-b")
        actor_a = "01HWA00000000000000000USRA"
        actor_b = "01HWA00000000000000000USRB"
        _bootstrap_actor(session, workspace_id=ws_a, user_id=actor_a)
        _bootstrap_actor(session, workspace_id=ws_b, user_id=actor_b)
        prop_id = _bootstrap_property(session)

        ctx_a = _ctx(ws_a, slug="read-a", actor_id=actor_a)
        view = create_oneoff(
            session,
            ctx_a,
            payload=TaskCreate.model_validate(
                {
                    "title": "A only",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        ctx_b = _ctx(ws_b, slug="read-b", actor_id=actor_b)
        with pytest.raises(TaskNotFound):
            read_task(session, ctx_b, task_id=view.id)

    def test_personal_gate_hides_task_from_non_creator(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """A personal task is not visible to a non-owner non-creator."""
        ws = _bootstrap_workspace(session, slug="read-pers")
        _bootstrap_actor(session, workspace_id=ws)
        # The stranger exists in the workspace but isn't the creator.
        stranger_id = "01HWA00000000000000000USR3"
        _bootstrap_user(session, email="stranger@example.com", user_id=stranger_id)

        prop_id = _bootstrap_property(session)
        creator_ctx = _ctx(ws, slug="read-pers")
        view = create_oneoff(
            session,
            creator_ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "secret",
                    "property_id": prop_id,
                    "is_personal": True,
                    "assigned_user_id": _ACTOR_ID,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )

        stranger_ctx = _ctx(ws, slug="read-pers", actor_id=stranger_id, was_owner=False)
        with pytest.raises(TaskNotFound):
            read_task(session, stranger_ctx, task_id=view.id)

        # The creator still sees the task.
        assert read_task(session, creator_ctx, task_id=view.id).id == view.id


class TestUpdateTask:
    """``update_task`` rewrites title / description_md and audits the delta."""

    def test_updates_title_and_writes_audit(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-title")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-title")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Original",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(title="New title"),
            clock=clock,
        )
        assert updated.title == "New title"

        audit_actions = session.scalars(
            select(AuditLog.action).where(AuditLog.entity_id == view.id)
        ).all()
        assert "task.update" in audit_actions

    def test_description_null_clears_field(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-desc")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-desc")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Has body",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                    "description_md": "original body",
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch.model_validate({"description_md": None}),
            clock=clock,
        )
        assert updated.description_md is None

    def test_empty_body_is_noop_no_audit(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-noop")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-noop")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Unchanged",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        before_audit = session.scalars(
            select(AuditLog.id).where(AuditLog.entity_id == view.id)
        ).all()

        update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(),
            clock=clock,
        )
        after_audit = session.scalars(
            select(AuditLog.id).where(AuditLog.entity_id == view.id)
        ).all()
        assert list(before_audit) == list(after_audit), (
            "no-op PATCH must not write an audit row"
        )

    def test_zero_delta_patch_skips_audit(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """Regression for cd-me3q: sending an explicit null on an
        already-null field must not trip a no-delta audit row."""
        ws = _bootstrap_workspace(session, slug="upd-zero")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-zero")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Intact",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                    # description_md intentionally omitted → defaults
                    # to None on the row.
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        before_audit = session.scalars(
            select(AuditLog.id).where(AuditLog.entity_id == view.id)
        ).all()

        # Explicit null on a field that is already null — the row
        # value doesn't change, so no audit row should land.
        update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch.model_validate({"description_md": None}),
            clock=clock,
        )
        after_audit = session.scalars(
            select(AuditLog.id).where(AuditLog.entity_id == view.id)
        ).all()
        assert list(before_audit) == list(after_audit)

    def test_cross_tenant_update_raises_not_found(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="upd-a")
        ws_b = _bootstrap_workspace(session, slug="upd-b")
        actor_a = "01HWA00000000000000000USRA"
        actor_b = "01HWA00000000000000000USRB"
        _bootstrap_actor(session, workspace_id=ws_a, user_id=actor_a)
        _bootstrap_actor(session, workspace_id=ws_b, user_id=actor_b)
        prop_id = _bootstrap_property(session)
        ctx_a = _ctx(ws_a, slug="upd-a", actor_id=actor_a)
        view = create_oneoff(
            session,
            ctx_a,
            payload=TaskCreate.model_validate(
                {
                    "title": "A only",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        ctx_b = _ctx(ws_b, slug="upd-b", actor_id=actor_b)
        with pytest.raises(TaskNotFound):
            update_task(
                session,
                ctx_b,
                task_id=view.id,
                body=TaskPatch(title="hijack"),
                clock=clock,
            )

    def test_unknown_field_rejected(self) -> None:
        """``TaskPatch`` uses ``extra='forbid'`` — a stray key is 422."""
        with pytest.raises(ValidationError):
            TaskPatch.model_validate({"title": "ok", "extra": "nope"})


class TestUpdateTaskWiderFields:
    """cd-43wv — PATCH widens to the §06 mutable set.

    Each new field flows through individually plus a few cross-field
    combinations. The existing :class:`TestUpdateTask` keeps the
    cd-sn26 narrow-slice happy path; this class layers the new
    coverage so a regression on either set is local to its block.
    """

    # --- priority / duration_minutes / photo_evidence — single-shot
    # column writes; no cross-resource validation needed. -----------

    def test_priority_update(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-pri")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-pri")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Pri",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(priority="urgent"),
            clock=clock,
            event_bus=bus,
        )
        assert updated.priority == "urgent"

    def test_duration_minutes_update(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-dur")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-dur")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Dur",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                    "duration_minutes": 30,
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(duration_minutes=90),
            clock=clock,
            event_bus=bus,
        )
        assert updated.duration_minutes == 90

    def test_photo_evidence_update(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-phev")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-phev")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Photo",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(photo_evidence="required"),
            clock=clock,
            event_bus=bus,
        )
        assert updated.photo_evidence == "required"

    def test_duration_clear_via_explicit_null(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """``duration_minutes`` is nullable — explicit null clears the column."""
        ws = _bootstrap_workspace(session, slug="upd-dur-null")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-dur-null")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "DurNull",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                    "duration_minutes": 45,
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch.model_validate({"duration_minutes": None}),
            clock=clock,
            event_bus=bus,
        )
        assert updated.duration_minutes is None

    # --- expected_role_id — workspace scoping required. ------------

    def test_expected_role_id_update(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-role")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        role_id = _bootstrap_work_role(session, workspace_id=ws)
        ctx = _ctx(ws, slug="upd-role")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Role",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(expected_role_id=role_id),
            clock=clock,
            event_bus=bus,
        )
        assert updated.expected_role_id == role_id

    def test_expected_role_id_cross_workspace_rejected(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="upd-role-a")
        ws_b = _bootstrap_workspace(session, slug="upd-role-b")
        _bootstrap_actor(session, workspace_id=ws_a)
        prop_id = _bootstrap_property(session)
        # Role lives in workspace B; the patch fires under workspace A.
        foreign_role_id = _bootstrap_work_role(session, workspace_id=ws_b)
        ctx = _ctx(ws_a, slug="upd-role-a")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "RoleX",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        with pytest.raises(TaskFieldInvalid) as exc:
            update_task(
                session,
                ctx,
                task_id=view.id,
                body=TaskPatch(expected_role_id=foreign_role_id),
                clock=clock,
                event_bus=bus,
            )
        assert exc.value.field == "expected_role_id"

    def test_expected_role_id_soft_deleted_rejected(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """A retired role can't be patched onto a fresh task — §05 archive."""
        ws = _bootstrap_workspace(session, slug="upd-role-del")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        role_id = _bootstrap_work_role(session, workspace_id=ws)
        ctx = _ctx(ws, slug="upd-role-del")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "RoleDel",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        # Soft-delete the role.
        role_row = session.scalar(select(WorkRole).where(WorkRole.id == role_id))
        assert role_row is not None
        role_row.deleted_at = _PINNED
        session.flush()

        with pytest.raises(TaskFieldInvalid) as exc:
            update_task(
                session,
                ctx,
                task_id=view.id,
                body=TaskPatch(expected_role_id=role_id),
                clock=clock,
                event_bus=bus,
            )
        assert exc.value.field == "expected_role_id"

    # --- property_id / area_id / unit_id — chain of validation. ----

    def test_property_id_update_with_workspace_link(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-prop")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id_a = _bootstrap_property(session)
        prop_id_b = _bootstrap_property(session, timezone="America/New_York")
        _link_property_to_workspace(session, property_id=prop_id_b, workspace_id=ws)
        ctx = _ctx(ws, slug="upd-prop")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Prop",
                    "property_id": prop_id_a,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(property_id=prop_id_b),
            clock=clock,
            event_bus=bus,
        )
        assert updated.property_id == prop_id_b

    def test_property_id_cross_workspace_rejected(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-prop-x")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id_a = _bootstrap_property(session)
        # Foreign property — never linked to ``ws``.
        prop_id_foreign = _bootstrap_property(session, timezone="UTC")
        ctx = _ctx(ws, slug="upd-prop-x")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "PropX",
                    "property_id": prop_id_a,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        with pytest.raises(TaskFieldInvalid) as exc:
            update_task(
                session,
                ctx,
                task_id=view.id,
                body=TaskPatch(property_id=prop_id_foreign),
                clock=clock,
                event_bus=bus,
            )
        assert exc.value.field == "property_id"

    def test_area_id_belongs_to_property(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-area")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        area_id = _bootstrap_area(session, property_id=prop_id, label="Kitchen")
        ctx = _ctx(ws, slug="upd-area")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Area",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(area_id=area_id),
            clock=clock,
            event_bus=bus,
        )
        assert updated.area_id == area_id

    def test_area_id_for_other_property_rejected(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-area-x")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id_a = _bootstrap_property(session)
        prop_id_b = _bootstrap_property(session, timezone="Europe/London")
        # Area is on property B; task is on property A.
        foreign_area = _bootstrap_area(session, property_id=prop_id_b)
        ctx = _ctx(ws, slug="upd-area-x")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "AreaX",
                    "property_id": prop_id_a,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        with pytest.raises(TaskFieldInvalid) as exc:
            update_task(
                session,
                ctx,
                task_id=view.id,
                body=TaskPatch(area_id=foreign_area),
                clock=clock,
                event_bus=bus,
            )
        assert exc.value.field == "area_id"

    def test_unit_id_for_other_property_rejected(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-unit-x")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id_a = _bootstrap_property(session)
        prop_id_b = _bootstrap_property(session, timezone="Europe/London")
        foreign_unit = _bootstrap_unit(session, property_id=prop_id_b)
        ctx = _ctx(ws, slug="upd-unit-x")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "UnitX",
                    "property_id": prop_id_a,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        with pytest.raises(TaskFieldInvalid) as exc:
            update_task(
                session,
                ctx,
                task_id=view.id,
                body=TaskPatch(unit_id=foreign_unit),
                clock=clock,
                event_bus=bus,
            )
        assert exc.value.field == "unit_id"

    def test_property_area_unit_combo_validates_against_new_property(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """Cross-field check: area / unit are validated against the
        post-patch property, not the original. A combo PATCH that
        moves all three at once must succeed when they are mutually
        consistent under the NEW property."""
        ws = _bootstrap_workspace(session, slug="upd-combo")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id_a = _bootstrap_property(session)
        prop_id_b = _bootstrap_property(session, timezone="Europe/London")
        _link_property_to_workspace(session, property_id=prop_id_b, workspace_id=ws)
        new_area = _bootstrap_area(session, property_id=prop_id_b, label="Garden")
        new_unit = _bootstrap_unit(session, property_id=prop_id_b)
        ctx = _ctx(ws, slug="upd-combo")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Combo",
                    "property_id": prop_id_a,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(
                property_id=prop_id_b,
                area_id=new_area,
                unit_id=new_unit,
            ),
            clock=clock,
            event_bus=bus,
        )
        assert updated.property_id == prop_id_b
        assert updated.area_id == new_area
        assert updated.unit_id == new_unit

    # --- scheduled_for_local — recompute + state flip. -------------

    def test_scheduled_for_local_recomputes_utc(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """Patching the local timestamp slides the UTC mirror via the
        property timezone."""
        ws = _bootstrap_workspace(session, slug="upd-sched-utc")
        _bootstrap_actor(session, workspace_id=ws)
        # Europe/Paris in late April is UTC+2 — local 18:00 → 16:00Z.
        prop_id = _bootstrap_property(session, timezone="Europe/Paris")
        ctx = _ctx(ws, slug="upd-sched-utc")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "SchedUtc",
                    "property_id": prop_id,
                    "scheduled_for_local": "2026-04-19T18:00",
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        # Move the local stamp forward by one hour; UTC must follow.
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(scheduled_for_local="2026-04-19T19:00"),
            clock=clock,
            event_bus=bus,
        )
        assert updated.scheduled_for_local == "2026-04-19T19:00:00"
        # 19:00 Europe/Paris on 2026-04-19 is 17:00 UTC.
        assert updated.scheduled_for_utc == datetime(2026, 4, 19, 17, 0, tzinfo=UTC)

    def test_scheduled_for_local_pushed_past_flips_to_pending(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """A scheduled task moved to the past becomes ``pending`` immediately."""
        ws = _bootstrap_workspace(session, slug="upd-flip-pending")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session, timezone="Europe/Paris")
        ctx = _ctx(ws, slug="upd-flip-pending")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Flip",
                    "property_id": prop_id,
                    # +4h in the future → state == "scheduled".
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        assert view.state == "scheduled"
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            # Past local timestamp → starts_at <= now → "pending".
            body=TaskPatch(scheduled_for_local=_past_local()),
            clock=clock,
            event_bus=bus,
        )
        assert updated.state == "pending"

    def test_scheduled_for_local_pushed_future_flips_back_to_scheduled(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """A pending task moved to the future flips back to ``scheduled``."""
        ws = _bootstrap_workspace(session, slug="upd-flip-sched")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session, timezone="Europe/Paris")
        ctx = _ctx(ws, slug="upd-flip-sched")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "FlipBack",
                    "property_id": prop_id,
                    # Past — lands directly in "pending".
                    "scheduled_for_local": _past_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        assert view.state == "pending"
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(scheduled_for_local=_future_local()),
            clock=clock,
            event_bus=bus,
        )
        assert updated.state == "scheduled"

    def test_in_progress_state_preserved_across_reschedule(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """An ``in_progress`` task keeps its state across a reschedule
        — the worker has already started the task; the auto-flip
        gate must not undo their move."""
        ws = _bootstrap_workspace(session, slug="upd-flip-ip")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session, timezone="Europe/Paris")
        ctx = _ctx(ws, slug="upd-flip-ip")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "IP",
                    "property_id": prop_id,
                    "scheduled_for_local": _past_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        # Move the row into ``in_progress`` directly on the ORM —
        # the create path stamped it ``pending``; the assignment /
        # completion services would move it from there. Skipping
        # those layers keeps this test focused on the PATCH gate.
        row = session.scalar(select(Occurrence).where(Occurrence.id == view.id))
        assert row is not None
        row.state = "in_progress"
        session.flush()

        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(scheduled_for_local=_future_local()),
            clock=clock,
            event_bus=bus,
        )
        assert updated.state == "in_progress"

    def test_property_change_reprojects_local_to_new_zone(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """Patching ``property_id`` without a ``scheduled_for_local``
        update re-projects the existing local clock through the new
        property's timezone."""
        ws = _bootstrap_workspace(session, slug="upd-tz")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id_paris = _bootstrap_property(session, timezone="Europe/Paris")
        prop_id_london = _bootstrap_property(session, timezone="Europe/London")
        _link_property_to_workspace(
            session, property_id=prop_id_london, workspace_id=ws
        )
        ctx = _ctx(ws, slug="upd-tz")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Tz",
                    "property_id": prop_id_paris,
                    # Paris UTC+2 → 18:00 local = 16:00 UTC.
                    "scheduled_for_local": "2026-04-19T18:00",
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        assert view.scheduled_for_utc == datetime(2026, 4, 19, 16, 0, tzinfo=UTC)
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(property_id=prop_id_london),
            clock=clock,
            event_bus=bus,
        )
        # London UTC+1 in April → 18:00 local = 17:00 UTC.
        assert updated.scheduled_for_utc == datetime(2026, 4, 19, 17, 0, tzinfo=UTC)
        assert updated.scheduled_for_local == "2026-04-19T18:00:00"

    def test_invalid_scheduled_for_local_raises_value_error(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-bad-iso")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-bad-iso")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "BadIso",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        with pytest.raises(ValueError):
            update_task(
                session,
                ctx,
                task_id=view.id,
                body=TaskPatch(scheduled_for_local="not-an-iso"),
                clock=clock,
                event_bus=bus,
            )

    # --- Event emission. -------------------------------------------

    def test_task_updated_event_published_with_changed_fields(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """A successful PATCH publishes :class:`TaskUpdated` carrying the
        sorted list of fields that genuinely moved."""
        captured: list[TaskUpdated] = []

        @bus.subscribe(TaskUpdated)
        def _on(event: TaskUpdated) -> None:
            captured.append(event)

        ws = _bootstrap_workspace(session, slug="upd-evt")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-evt")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Evt",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(title="Renamed", priority="high"),
            clock=clock,
            event_bus=bus,
        )
        assert len(captured) == 1
        evt = captured[0]
        assert evt.task_id == view.id
        assert "title" in evt.changed_fields
        assert "priority" in evt.changed_fields

    def test_no_event_on_zero_delta_patch(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """A PATCH that doesn't move any value must not publish
        :class:`TaskUpdated` — the audit row is also skipped."""
        captured: list[TaskUpdated] = []

        @bus.subscribe(TaskUpdated)
        def _on(event: TaskUpdated) -> None:
            captured.append(event)

        ws = _bootstrap_workspace(session, slug="upd-noevt")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-noevt")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "NoEvt",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(),
            clock=clock,
            event_bus=bus,
        )
        assert captured == []

    def test_audit_row_carries_before_and_after_for_widened_fields(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session, slug="upd-audit")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        role_id = _bootstrap_work_role(session, workspace_id=ws)
        ctx = _ctx(ws, slug="upd-audit")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Audit",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch(expected_role_id=role_id, priority="urgent"),
            clock=clock,
            event_bus=bus,
        )
        diffs = session.scalars(
            select(AuditLog.diff).where(
                AuditLog.entity_id == view.id,
                AuditLog.action == "task.update",
            )
        ).all()
        assert len(diffs) == 1
        diff = diffs[0]
        assert diff["before"]["expected_role_id"] is None
        assert diff["after"]["expected_role_id"] == role_id
        assert diff["before"]["priority"] == "normal"
        assert diff["after"]["priority"] == "urgent"

    # --- Edge cases the cd-43wv selfreview surfaced. ---------------

    def test_whitespace_only_title_rejected(self) -> None:
        """A whitespace-only ``title`` would survive ``min_length=1``
        but ``.strip()`` empty in the service — reject at DTO so the
        row never lands with an empty title."""
        with pytest.raises(ValidationError):
            TaskPatch.model_validate({"title": "   "})

    def test_property_clear_rejects_dangling_area(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """Patching ``property_id=null`` while the row still has an
        ``area_id`` (pointing at the old property) must be rejected —
        the area would otherwise be a cross-property orphan. The
        caller has to clear ``area_id`` in the same patch."""
        ws = _bootstrap_workspace(session, slug="upd-prop-orphan-area")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        area_id = _bootstrap_area(session, property_id=prop_id, label="Pool")
        ctx = _ctx(ws, slug="upd-prop-orphan-area")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Orphan",
                    "property_id": prop_id,
                    "area_id": area_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        with pytest.raises(TaskFieldInvalid) as exc:
            update_task(
                session,
                ctx,
                task_id=view.id,
                body=TaskPatch.model_validate({"property_id": None}),
                clock=clock,
                event_bus=bus,
            )
        assert exc.value.field == "area_id"

    def test_property_clear_with_simultaneous_area_clear_succeeds(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """Clearing property and area together is the right way to
        scrub the location tuple — the patch stands."""
        ws = _bootstrap_workspace(session, slug="upd-loc-clear")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        area_id = _bootstrap_area(session, property_id=prop_id, label="Pool")
        ctx = _ctx(ws, slug="upd-loc-clear")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "Clear",
                    "property_id": prop_id,
                    "area_id": area_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        updated = update_task(
            session,
            ctx,
            task_id=view.id,
            body=TaskPatch.model_validate({"property_id": None, "area_id": None}),
            clock=clock,
            event_bus=bus,
        )
        assert updated.property_id is None
        assert updated.area_id is None

    def test_property_change_rejects_dangling_unit(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """Moving the task to a new property without repointing the
        existing ``unit_id`` (still on the old property) must be
        rejected — silent inconsistency would leak to readers."""
        ws = _bootstrap_workspace(session, slug="upd-prop-orphan-unit")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id_a = _bootstrap_property(session)
        prop_id_b = _bootstrap_property(session, timezone="Europe/London")
        _link_property_to_workspace(session, property_id=prop_id_b, workspace_id=ws)
        unit_id_a = _bootstrap_unit(session, property_id=prop_id_a)
        ctx = _ctx(ws, slug="upd-prop-orphan-unit")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "OrphanUnit",
                    "property_id": prop_id_a,
                    "unit_id": unit_id_a,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        with pytest.raises(TaskFieldInvalid) as exc:
            update_task(
                session,
                ctx,
                task_id=view.id,
                body=TaskPatch(property_id=prop_id_b),
                clock=clock,
                event_bus=bus,
            )
        assert exc.value.field == "unit_id"

    def test_invalid_scheduled_for_local_raises_typed_subclass(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
    ) -> None:
        """``_parse_local_datetime`` raises :class:`InvalidLocalDatetime`,
        a ``ValueError`` subclass — keeping the type distinct from
        clock-contract / generic ``ValueError`` paths the router
        catches separately."""
        ws = _bootstrap_workspace(session, slug="upd-bad-iso-typed")
        _bootstrap_actor(session, workspace_id=ws)
        prop_id = _bootstrap_property(session)
        ctx = _ctx(ws, slug="upd-bad-iso-typed")
        view = create_oneoff(
            session,
            ctx,
            payload=TaskCreate.model_validate(
                {
                    "title": "BadIsoTyped",
                    "property_id": prop_id,
                    "scheduled_for_local": _future_local(),
                }
            ),
            clock=clock,
            event_bus=bus,
        )
        with pytest.raises(InvalidLocalDatetime):
            update_task(
                session,
                ctx,
                task_id=view.id,
                body=TaskPatch(scheduled_for_local="2026-04-19T18:00+02:00"),
                clock=clock,
                event_bus=bus,
            )
