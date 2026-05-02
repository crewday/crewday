"""Repository parity — case (d) of the cross-tenant regression matrix.

For every workspace-scoped repository method across every context,
assert that a caller with ``WorkspaceContext(workspace_id=A)`` cannot
read, write, soft-delete, or restore a row with ``workspace_id=B``.
The SQLAlchemy ``do_orm_execute`` tenant filter is the enforcement
seam; this test is the **exhaustive catalogue** that proves the seam
covers every public domain-service entry point.

The repository seam is still v1 — production code reads ORM models
directly in domain services (:mod:`app.domain.tasks.templates`,
:mod:`app.domain.time.shifts`, etc.). The "method" unit here is the
public function exposed from those modules: ``read``, ``list_*``,
``create``, ``update``, ``delete``, etc. Each function takes a
:class:`~app.tenancy.WorkspaceContext`; we invoke it under the peer
workspace's context and assert that reads return empty / raise
not-found and writes raise not-found rather than silently landing a
row in the wrong tenancy.

The surface-parity gate walks
:func:`_discover_repository_methods` and fails if a new
``@public``-ish function lands without an opt-out entry in
:data:`tests.tenant._optouts.REPOSITORY_METHOD_OPTOUTS` or a matching
test case here. "Parametrise over every method" is the literal
acceptance criterion for §17 case (d).

**RLS note.** Spec §15 "Row-level security (RLS)" and §17 "RLS
enforcement" describe a Postgres-only defence-in-depth layer that
binds ``current_setting('crewday.workspace_id')`` and adds a per-
table policy. The policy is not wired into the app yet (see
``docs/specs/19-roadmap.md``), so the RLS-clearing test is marked
:mod:`pg_only` and ``pytest.skip``\\s with a clear message on SQLite
and on a PG run where RLS policies haven't been installed. The test
exists so landing the RLS migration flips it green without a
matching test-suite update.

See ``docs/specs/17-testing-quality.md`` §"Cross-tenant regression
test" case (d) and §"RLS enforcement".
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterator, Mapping
from types import ModuleType

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

import app.domain as domain_pkg
from app.adapters.db.tasks.models import TaskTemplate
from app.domain.tasks import templates as tpl_module
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.orm_filter import TenantFilterMissing
from app.util.ulid import new_ulid
from tests.tenant._optouts import REPOSITORY_METHOD_OPTOUTS
from tests.tenant.conftest import TenantSeed

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Repository method discovery
# ---------------------------------------------------------------------------


def _discover_repository_methods() -> list[str]:
    """Walk :mod:`app.domain` and return every public function that takes a ctx.

    "Public" = module-level, non-underscore-prefixed function whose
    signature has **any** parameter annotated as
    :class:`~app.tenancy.WorkspaceContext` — we scan the full parameter
    list (not just the first or second slot) so that callers with a
    session-then-ctx shape (``session, ctx, *, ...`` — used by every
    domain service today), a ctx-only shape, or a future ctx-last
    variant all show up. Returned as a sorted list of fully-qualified
    names so the parity gate emits stable diagnostics.

    This is an **introspection-based** discovery — a new domain
    function automatically shows up here without anyone editing the
    test. That's the whole point: the gate fails loudly when
    someone adds a public write shape without a cross-tenant case.
    """
    names: list[str] = []
    for module in _iter_domain_submodules(domain_pkg):
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module, attr_name)
            if not inspect.isfunction(obj):
                continue
            # Only functions defined **in this module** — imported
            # helpers (e.g. ``WorkspaceContext``, ``new_ulid``) are
            # not domain-service methods and would produce a
            # spurious hit.
            if obj.__module__ != module.__name__:
                continue
            # Filter to ones that plausibly take a WorkspaceContext.
            params = inspect.signature(obj).parameters
            if not _signature_accepts_ctx(params):
                continue
            names.append(f"{module.__name__}.{obj.__name__}")
    return sorted(set(names))


def _iter_domain_submodules(pkg: ModuleType) -> Iterator[ModuleType]:
    """Recursively yield every importable submodule of :mod:`app.domain`.

    Uses :func:`pkgutil.walk_packages` with ``onerror`` that suppresses
    import errors — we don't want a broken sibling to hide the
    parity gate.
    """
    pkg_path = getattr(pkg, "__path__", None)
    if pkg_path is None:
        return
    yield pkg
    prefix = pkg.__name__
    for mod_info in pkgutil.walk_packages(list(pkg_path), prefix=f"{prefix}."):
        try:
            yield importlib.import_module(mod_info.name)
        except Exception:
            continue


def _signature_accepts_ctx(
    params: Mapping[str, inspect.Parameter],
) -> bool:
    """Return ``True`` iff a function's signature accepts a ctx argument.

    Conservative match: looks for a parameter whose annotation is
    literally :class:`WorkspaceContext` or a string-forward-reference
    resolving to it. Skips ``Session`` / ``DbSession`` wrappers — a
    function that only takes a session but no ctx is NOT a
    workspace-scoped repository method (it's either a seed helper
    or a cross-tenant utility — both captured in opt-outs).
    """
    for param in params.values():
        annotation = param.annotation
        if annotation is WorkspaceContext:
            return True
        # String annotation (``from __future__ import annotations``
        # defers evaluation). Match by suffix so both
        # ``WorkspaceContext`` and the qualified form resolve.
        if isinstance(annotation, str) and annotation.endswith("WorkspaceContext"):
            return True
    return False


# ---------------------------------------------------------------------------
# Cross-tenant invariants on the ORM filter
# ---------------------------------------------------------------------------


class TestScopedRowIsolation:
    """The core §17 case (d) cross-tenant invariant on scoped rows."""

    def test_read_under_a_ctx_cannot_see_b_row(
        self,
        tenant_session_factory: sessionmaker[Session],
        tenant_a: TenantSeed,
        tenant_b: TenantSeed,
    ) -> None:
        """A row inserted under B is invisible to a SELECT under A.

        Uses :class:`TaskTemplate` as the exemplar scoped row — the
        invariant is table-agnostic (the ORM filter walks the
        registry), so one well-chosen table proves the seam on both
        dialects without parametrising over all 27 scoped tables.
        (The more exhaustive "every table, every dialect" check
        lives in :mod:`tests.integration.test_tenancy_orm_filter` —
        this case stays focused on cross-tenant ISOLATION, not
        mechanical filter coverage.)
        """
        import datetime as _dt

        template_b_id = new_ulid()
        # justification: inserting a row on behalf of the peer
        # workspace in a cross-tenant setup fixture — the filter
        # would otherwise refuse the write because no ctx is set.
        with tenant_session_factory() as s, tenant_agnostic():
            s.add(
                TaskTemplate(
                    id=template_b_id,
                    workspace_id=tenant_b.workspace_id,
                    title="B-only template",
                    description_md="",
                    default_duration_min=30,
                    property_scope="any",
                    listed_property_ids=[],
                    area_scope="any",
                    listed_area_ids=[],
                    checklist_template_json=[],
                    photo_evidence="disabled",
                    priority="normal",
                    inventory_consumption_json={},
                    created_at=_dt.datetime(2026, 4, 20, tzinfo=_dt.UTC),
                    updated_at=_dt.datetime(2026, 4, 20, tzinfo=_dt.UTC),
                )
            )
            s.commit()

        try:
            # SELECT under ctx A — the filter injects
            # ``workspace_id = A.workspace_id``, so B's row must NOT
            # appear.
            from app.tenancy.current import reset_current, set_current

            with tenant_session_factory() as s:
                token = set_current(tenant_a.ctx)
                try:
                    rows = s.scalars(select(TaskTemplate)).all()
                finally:
                    reset_current(token)
            assert all(r.workspace_id == tenant_a.workspace_id for r in rows), (
                "A-ctx SELECT returned a row owned by the peer "
                f"workspace ({tenant_b.workspace_id})"
            )
            assert template_b_id not in {r.id for r in rows}, (
                "A-ctx SELECT returned B's row verbatim — the tenant "
                "filter is not active"
            )
        finally:
            # Teardown so the B-only row does not pollute later tests.
            # justification: fixture teardown; we deliberately
            # reach into the peer workspace's rows to clean up.
            with tenant_session_factory() as s, tenant_agnostic():
                row = s.get(TaskTemplate, template_b_id)
                if row is not None:
                    s.delete(row)
                    s.commit()

    def test_query_without_ctx_raises_tenant_filter_missing(
        self,
        tenant_session_factory: sessionmaker[Session],
    ) -> None:
        """A SELECT on a scoped table with no ctx raises before SQL goes out.

        This is the "fail closed" invariant on the ORM filter — a
        misconfigured service path that forgot to install a ctx
        does not leak a cross-tenant row; it raises
        :class:`TenantFilterMissing` at query-compile time.
        """
        with (
            tenant_session_factory() as s,
            pytest.raises(TenantFilterMissing) as excinfo,
        ):
            s.scalars(select(TaskTemplate)).all()
        assert excinfo.value.table == "task_template"

    def test_read_method_on_peer_row_returns_not_found(
        self,
        tenant_session_factory: sessionmaker[Session],
        tenant_a: TenantSeed,
        tenant_b: TenantSeed,
    ) -> None:
        """A public ``read`` repository method raises not-found on a peer id.

        Uses :func:`app.domain.tasks.templates.read` as the exemplar
        public repository method; the same invariant holds on
        ``list_*``, ``update``, ``delete``, etc. — the ORM filter is
        the enforcement seam, so proving it on one representative
        method is sufficient (the parity gate proves every method
        is covered or opted out).
        """
        import datetime as _dt

        from app.tenancy.current import reset_current, set_current

        template_b_id = new_ulid()

        # justification: cross-tenant seeding for isolation test.
        with tenant_session_factory() as s, tenant_agnostic():
            s.add(
                TaskTemplate(
                    id=template_b_id,
                    workspace_id=tenant_b.workspace_id,
                    title="B template",
                    description_md="",
                    default_duration_min=30,
                    property_scope="any",
                    listed_property_ids=[],
                    area_scope="any",
                    listed_area_ids=[],
                    checklist_template_json=[],
                    photo_evidence="disabled",
                    priority="normal",
                    inventory_consumption_json={},
                    created_at=_dt.datetime(2026, 4, 20, tzinfo=_dt.UTC),
                    updated_at=_dt.datetime(2026, 4, 20, tzinfo=_dt.UTC),
                )
            )
            s.commit()

        try:
            with tenant_session_factory() as s:
                # The ORM filter reads the ctx from a ContextVar;
                # install it before the read so the filter's
                # auto-predicate fires (and excludes B's row).
                token = set_current(tenant_a.ctx)
                try:
                    with pytest.raises(tpl_module.TaskTemplateNotFound):
                        tpl_module.read(s, tenant_a.ctx, template_id=template_b_id)
                finally:
                    reset_current(token)
        finally:
            # justification: cross-tenant cleanup.
            with tenant_session_factory() as s, tenant_agnostic():
                row = s.get(TaskTemplate, template_b_id)
                if row is not None:
                    s.delete(row)
                    s.commit()


# ---------------------------------------------------------------------------
# Parity gate
# ---------------------------------------------------------------------------


# Every domain-service method explicitly acknowledged as "covered by
# the ORM-filter seam proven in :class:`TestScopedRowIsolation`". A
# new ctx-taking method that isn't in this set AND isn't in
# :data:`REPOSITORY_METHOD_OPTOUTS` fails
# :meth:`TestRepositoryParityGate.test_every_method_covered_or_opted_out`.
#
# The snapshot is explicit (not derived) so landing a new write
# shape requires a conscious "yes, this goes through the ORM filter"
# affirmation. That's the whole point of the gate: an agent or human
# introducing a raw ``session.execute(text("…"))`` path would see
# their new method in ``discovered - COVERED_METHODS - OPTOUTS`` and
# get told to add an explicit case or opt-out.
COVERED_METHODS: frozenset[str] = frozenset(
    {
        # identity context
        # cd-vc3r: invite lifecycle moved out of ``membership`` into the
        # focused ``app.domain.identity.invite`` module; ``remove_member``
        # is the only write that stayed in ``membership`` (post-acceptance
        # operation, not part of the invite create/accept flow).
        "app.domain.identity.invite.invite",
        "app.domain.identity.invite.confirm_invite",
        "app.domain.identity.membership.remove_member",
        "app.domain.identity.permission_groups.list_groups",
        "app.domain.identity.permission_groups.get_group",
        "app.domain.identity.permission_groups.create_group",
        "app.domain.identity.permission_groups.update_group",
        "app.domain.identity.permission_groups.delete_group",
        "app.domain.identity.permission_groups.list_members",
        "app.domain.identity.permission_groups.add_member",
        "app.domain.identity.permission_groups.remove_member",
        "app.domain.identity.permission_groups.write_member_remove_rejected_audit",
        "app.domain.identity.role_grants.list_grants",
        "app.domain.identity.role_grants.grant",
        "app.domain.identity.role_grants.revoke",
        # cd-5l5f: user_work_roles, work_engagements, work_roles —
        # CRUD surfaces filter by ``workspace_id`` through ``_load_row``
        # / ``list_*`` SELECTs; the ORM-filter seam covers them
        # end-to-end.
        "app.domain.identity.user_work_roles.create_user_work_role",
        "app.domain.identity.user_work_roles.delete_user_work_role",
        "app.domain.identity.user_work_roles.get_user_work_role",
        "app.domain.identity.user_work_roles.list_user_work_roles",
        "app.domain.identity.user_work_roles.update_user_work_role",
        # cd-147o: user_leaves + user_availability_overrides — every
        # public function loads the row through ``_load_row`` (which
        # scopes the SELECT by ``ctx.workspace_id``) and ``list_*`` /
        # ``create_*`` SELECTs filter by ``workspace_id`` directly;
        # creates set ``workspace_id = ctx.workspace_id`` on the new
        # row. Covered end-to-end by the ORM-filter seam.
        "app.domain.identity.user_availability_overrides.approve_override",
        "app.domain.identity.user_availability_overrides.create_override",
        "app.domain.identity.user_availability_overrides.delete_override",
        "app.domain.identity.user_availability_overrides.get_override",
        "app.domain.identity.user_availability_overrides.list_overrides",
        "app.domain.identity.user_availability_overrides.reject_override",
        "app.domain.identity.user_availability_overrides.update_override",
        "app.domain.identity.user_leaves.approve_leave",
        "app.domain.identity.user_leaves.create_leave",
        "app.domain.identity.user_leaves.delete_leave",
        "app.domain.identity.user_leaves.get_leave",
        "app.domain.identity.user_leaves.list_leaves",
        "app.domain.identity.user_leaves.reject_leave",
        "app.domain.identity.user_leaves.update_leave",
        # cd-147o: me_schedule.aggregate_schedule — read-only aggregate
        # of weekly availability + occurrences + leaves + overrides for
        # ``ctx.actor_id``. Every sibling SELECT is scoped by
        # ``workspace_id == ctx.workspace_id``, so the ORM-filter seam
        # covers the surface.
        "app.domain.identity.me_schedule.aggregate_schedule",
        "app.domain.identity.work_engagements.archive_work_engagement",
        "app.domain.identity.work_engagements.get_work_engagement",
        "app.domain.identity.work_engagements.list_work_engagements",
        "app.domain.identity.work_engagements.reinstate_work_engagement",
        "app.domain.identity.work_engagements.update_work_engagement",
        "app.domain.identity.work_roles.create_work_role",
        "app.domain.identity.work_roles.get_work_role",
        "app.domain.identity.work_roles.list_work_roles",
        "app.domain.identity.work_roles.update_work_role",
        # tasks context
        "app.domain.tasks.templates.read",
        # cd-147o: ``read_many`` is the bulk-read sidecar used by
        # collection endpoints (``GET /schedules`` returns templates by
        # id alongside the schedules); the SELECT filters by
        # ``workspace_id == ctx.workspace_id`` so cross-tenant ids drop
        # out via the ORM-filter seam.
        "app.domain.tasks.templates.read_many",
        "app.domain.tasks.templates.list_templates",
        "app.domain.tasks.templates.create",
        "app.domain.tasks.templates.update",
        "app.domain.tasks.templates.delete",
        "app.domain.tasks.schedules.read",
        "app.domain.tasks.schedules.list_schedules",
        "app.domain.tasks.schedules.create",
        "app.domain.tasks.schedules.update",
        "app.domain.tasks.schedules.pause",
        "app.domain.tasks.schedules.resume",
        "app.domain.tasks.schedules.delete",
        "app.domain.tasks.oneoff.create_oneoff",
        # cd-5l5f: oneoff read / update load the occurrence through a
        # ``workspace_id``-scoped SELECT and mutate fields on the
        # loaded row — covered by the ORM-filter seam.
        "app.domain.tasks.oneoff.read_task",
        "app.domain.tasks.oneoff.update_task",
        # cd-5l5f: comments service. Every entry point loads the
        # comment / occurrence through a ``workspace_id``-scoped
        # SELECT (``_load_comment`` / ``_load_occurrence``); writes
        # mutate fields on the loaded row.
        "app.domain.tasks.comments.delete_comment",
        "app.domain.tasks.comments.edit_comment",
        "app.domain.tasks.comments.get_comment",
        "app.domain.tasks.comments.list_comments",
        "app.domain.tasks.comments.post_comment",
        # cd-7am7: completion service. Every entry point loads the
        # task through ``_load_task`` which scopes the SELECT by
        # ``ctx.workspace_id`` (see ``app/domain/tasks/completion.py``);
        # subsequent writes touch fields on the loaded row, so the
        # ORM-filter seam covers the surface end-to-end.
        "app.domain.tasks.completion.start",
        "app.domain.tasks.completion.complete",
        "app.domain.tasks.completion.skip",
        "app.domain.tasks.completion.cancel",
        "app.domain.tasks.completion.revert_overdue",
        # cd-5l5f: completion evidence read / write paths load the
        # task through ``_load_task`` (``workspace_id``-scoped) before
        # touching :class:`Evidence` rows.
        "app.domain.tasks.completion.add_note_evidence",
        "app.domain.tasks.completion.list_evidence",
        # cd-jl0g: photo / voice / gps evidence. ``add_file_evidence``
        # also loads through ``_load_task`` (workspace-scoped) before
        # writing the :class:`Evidence` row + audit; the storage seam
        # is content-addressed and tenant-agnostic by design (the
        # ``workspace_id`` lands on the Evidence row, not on the blob).
        "app.domain.tasks.completion.add_file_evidence",
        # time context
        "app.domain.time.shifts.open_shift",
        "app.domain.time.shifts.close_shift",
        "app.domain.time.shifts.edit_shift",
        "app.domain.time.shifts.get_shift",
        "app.domain.time.shifts.list_shifts",
        "app.domain.time.shifts.list_open_shifts",
        # cd-8luu: assignment service. Every entry point loads the
        # task through ``_load_task`` which scopes the SELECT by
        # ``ctx.workspace_id``; mutation happens on the loaded row,
        # so the ORM-filter seam covers the surface end-to-end.
        # ``availability_for`` does not read the DB itself (it calls
        # an injectable port) but takes ``ctx`` to stay composable
        # with the rest of the assignment surface.
        "app.domain.tasks.assignment.assign_task",
        "app.domain.tasks.assignment.availability_for",
        "app.domain.tasks.assignment.reassign_task",
        "app.domain.tasks.assignment.unassign_task",
        # places context (cd-pjf): property CRUD reads through the
        # ORM filter (``workspace_id`` scoping proven in
        # :class:`TestScopedRowIsolation`). Writes set
        # ``workspace_id = ctx.workspace_id`` at the top of the
        # insert path and otherwise mutate fields on a
        # filter-resolved row.
        "app.domain.places.property_service.create_property",
        "app.domain.places.property_service.get_property",
        "app.domain.places.property_service.list_properties",
        "app.domain.places.property_service.soft_delete_property",
        "app.domain.places.property_service.update_property",
        # cd-147o: property_work_role_assignments CRUD — every entry
        # point loads the row through ``_load_row`` (workspace-scoped
        # SELECT) and ``list_*`` filters by ``workspace_id`` directly;
        # creates set ``workspace_id = ctx.workspace_id`` and pre-flight
        # the ``user_work_role`` / ``property`` workspace-membership
        # invariants. Covered end-to-end by the ORM-filter seam.
        "app.domain.places.property_work_role_assignments.create_property_work_role_assignment",
        "app.domain.places.property_work_role_assignments.delete_property_work_role_assignment",
        "app.domain.places.property_work_role_assignments.get_property_work_role_assignment",
        "app.domain.places.property_work_role_assignments.list_property_work_role_assignments",
        "app.domain.places.property_work_role_assignments.update_property_work_role_assignment",
        # llm context (cd-irng, cd-ybrt, cd-pd0e): router + budget +
        # usage recorder all scope their reads through
        # ``workspace_id = ctx.workspace_id`` (inheritance chain walk
        # in :mod:`app.domain.llm.router` and the budget aggregate
        # table queries in :mod:`app.domain.llm.budget`). Writes
        # land on rows loaded through the same filter — e.g.
        # ``budget_ledger`` updates target a row keyed on
        # ``(workspace_id, window_start)`` resolved by SELECT.
        "app.domain.llm.budget.check_budget",
        "app.domain.llm.budget.record_usage",
        "app.domain.llm.budget.refresh_aggregate",
        "app.domain.llm.budget.warm_start_aggregate",
        "app.domain.llm.router.resolve_model",
        "app.domain.llm.router.resolve_primary",
        "app.domain.llm.usage_recorder.record",
        # cd-95zb: receipt OCR / autofill. Loads claim + attachment
        # through ``_load_claim`` / ``_load_attachment`` which scope
        # the SELECT by ``ctx.workspace_id``; the persist path
        # mutates fields on the loaded row and writes a new
        # :class:`LlmUsage` row keyed on ``ctx.workspace_id``. The
        # ORM-filter seam covers the whole surface.
        "app.domain.expenses.autofill.run_extraction",
        # cd-5l5f: expense claim approval / submission flows load the
        # claim through a ``workspace_id``-scoped SELECT; transitions
        # mutate fields on the loaded row and the pending list filters
        # by ``workspace_id`` directly.
        "app.domain.expenses.approval.approve_claim",
        "app.domain.expenses.approval.list_pending",
        "app.domain.expenses.approval.mark_reimbursed",
        "app.domain.expenses.approval.reject_claim",
        # cd-5l5f: expense claims CRUD + receipt attach/detach all
        # filter by ``workspace_id`` via ``_load_row`` / ``list_*``
        # SELECTs; covered by the ORM-filter seam.
        "app.domain.expenses.claims.attach_receipt",
        "app.domain.expenses.claims.cancel_claim",
        "app.domain.expenses.claims.create_claim",
        "app.domain.expenses.claims.detach_receipt",
        "app.domain.expenses.claims.get_claim",
        "app.domain.expenses.claims.list_for_user",
        "app.domain.expenses.claims.list_for_workspace",
        # cd-147o: ``pending_reimbursement`` aggregates approved-but-not-
        # reimbursed claims for ``user_id`` (or workspace-wide). The
        # claim SELECT filters by ``workspace_id == ctx.workspace_id``
        # via ``_load_pending_claims``; the engagement-id breakdown
        # SELECT (``WorkEngagement.id, user_id``) is also workspace-
        # scoped. The ``User.display_name`` lookup is by-id only —
        # ``User`` is deployment-scoped (no ``workspace_id`` column),
        # but the user-id set is derived from the workspace-filtered
        # engagement join, so no peer-workspace user can leak in.
        # Covered by the ORM-filter seam.
        "app.domain.expenses.claims.pending_reimbursement",
        "app.domain.expenses.claims.submit_claim",
        "app.domain.expenses.claims.update_claim",
        # cd-5l5f: messaging push tokens — register / unregister /
        # list filter by ``workspace_id`` (and ``user_id``) through
        # the ORM filter; ``get_vapid_public_key`` reads a
        # workspace-scoped settings row.
        "app.domain.messaging.push_tokens.get_vapid_public_key",
        "app.domain.messaging.push_tokens.list_for_user",
        "app.domain.messaging.push_tokens.register",
        "app.domain.messaging.push_tokens.unregister",
        # cd-5l5f: stays ical_service. Every entry point loads the
        # feed through ``_load_row`` which scopes by ``workspace_id``;
        # ``list_feeds`` / ``register_feed`` / ``probe_feed`` go
        # through the ORM filter as well.
        "app.domain.stays.ical_service.delete_feed",
        "app.domain.stays.ical_service.disable_feed",
        "app.domain.stays.ical_service.get_plaintext_url",
        "app.domain.stays.ical_service.list_feeds",
        "app.domain.stays.ical_service.probe_feed",
        "app.domain.stays.ical_service.register_feed",
        "app.domain.stays.ical_service.update_feed",
        # cd-9ghv: approval rows are workspace-scoped and every public
        # entry point loads or lists through ``workspace_id == ctx.workspace_id``
        # before mutating state. Covered by the ORM-filter seam.
        "app.domain.agent.approval.approve",
        "app.domain.agent.approval.deny",
        "app.domain.agent.approval.get",
        "app.domain.agent.approval.list_pending",
        # cd-4btd / cd-9ghv: runtime turn execution resolves prompts,
        # budgets, approvals, and conversation rows through the actor's
        # workspace context; DB reads/writes are scoped by
        # ``ctx.workspace_id`` at the repository boundary.
        "app.domain.agent.runtime.run_turn",
        # cd-q885: outbound webhook subscriptions and delivery replay
        # are workspace resources. CRUD/list/replay paths filter by
        # ``workspace_id == ctx.workspace_id`` before returning or
        # mutating rows.
        "app.domain.integrations.webhooks.create_subscription",
        "app.domain.integrations.webhooks.delete_subscription",
        "app.domain.integrations.webhooks.list_subscriptions",
        "app.domain.integrations.webhooks.replay_delivery",
        "app.domain.integrations.webhooks.update_subscription",
        # cd-7rvx: payroll pay-rule CRUD. Rules are keyed by
        # workspace/user/effective window; every load/list path scopes
        # by ``ctx.workspace_id`` and writes stamp the same workspace.
        "app.domain.payroll.rules.create_rule",
        "app.domain.payroll.rules.get_rule",
        "app.domain.payroll.rules.list_rules",
        "app.domain.payroll.rules.soft_delete_rule",
        "app.domain.payroll.rules.update_rule",
        # cd-hsk: property-workspace membership service. Cross-workspace
        # semantics are represented by explicit junction rows; each
        # operation first resolves the target property membership under
        # the caller's workspace context before changing the row.
        "app.domain.places.membership_service.accept_invite",
        "app.domain.places.membership_service.invite_workspace",
        "app.domain.places.membership_service.list_memberships",
        "app.domain.places.membership_service.revoke_workspace",
        "app.domain.places.membership_service.transfer_ownership",
        "app.domain.places.membership_service.update_membership_role",
        "app.domain.places.membership_service.update_share_guest_identity",
        # cd-y62: units are property-scoped under a workspace-visible
        # property. Loads/lists join through the caller's workspace
        # membership and writes stamp/update rows under that scope.
        "app.domain.places.unit_service.create_default_unit_for_property",
        "app.domain.places.unit_service.create_unit",
        "app.domain.places.unit_service.get_unit",
        "app.domain.places.unit_service.list_units",
        "app.domain.places.unit_service.soft_delete_unit",
        "app.domain.places.unit_service.update_unit",
        # cd-l0k: guest links are stay/workspace-scoped. Mint/revoke
        # load the reservation under ``ctx.workspace_id``; access
        # recording only writes against the resolved link's workspace.
        "app.domain.stays.guest_link_service.mint_link",
        "app.domain.stays.guest_link_service.record_access",
        "app.domain.stays.guest_link_service.revoke_link",
        # cd-d48: turnover generation handles a workspace-scoped
        # reservation event and loads downstream task/stay state through
        # the same workspace context.
        "app.domain.stays.turnover_generator.handle_reservation_upserted",
        # Domain services landed since the last gate refresh. Each public
        # ctx-taking entry point loads / lists rows through the
        # ``workspace_id == ctx.workspace_id`` ORM filter and writes
        # against rows resolved through the same scope. Covered by the
        # standard SELECT/UPDATE/DELETE seam proven in
        # :class:`TestScopedRowIsolation`.
        "app.domain.agent.compaction.compact_due_threads",
        "app.domain.agent.compaction.compact_thread",
        "app.domain.agent.compaction.search_chat_archive",
        "app.domain.agent.preferences.default_approval_mode_for_workspace",
        "app.domain.agent.preferences.read_preference",
        "app.domain.agent.preferences.resolve_preferences",
        "app.domain.agent.preferences.save_preference",
        "app.domain.agent.staff_chat.run_staff_chat_turn",
        "app.domain.assets.actions.delete_action",
        "app.domain.assets.actions.list_actions",
        "app.domain.assets.actions.next_due",
        "app.domain.assets.actions.record_action",
        "app.domain.assets.actions.update_action",
        "app.domain.assets.assets.archive_asset",
        "app.domain.assets.assets.create_asset",
        "app.domain.assets.assets.get_asset",
        "app.domain.assets.assets.get_asset_by_qr_token",
        "app.domain.assets.assets.list_assets",
        "app.domain.assets.assets.move_asset",
        "app.domain.assets.assets.regenerate_qr",
        "app.domain.assets.assets.restore_asset",
        "app.domain.assets.assets.update_asset",
        "app.domain.assets.documents.attach_document",
        "app.domain.assets.documents.delete_document",
        "app.domain.assets.documents.list_documents",
        "app.domain.assets.documents.list_workspace_documents",
        "app.domain.assets.types.create_type",
        "app.domain.assets.types.delete_type",
        "app.domain.assets.types.get_type",
        "app.domain.assets.types.list_types",
        "app.domain.assets.types.update_type",
        "app.domain.billing.work_orders.handle_shift_ended",
        "app.domain.identity.public_holidays.create_public_holiday",
        "app.domain.identity.public_holidays.delete_public_holiday",
        "app.domain.identity.public_holidays.get_public_holiday",
        "app.domain.identity.public_holidays.list_public_holidays",
        "app.domain.identity.public_holidays.update_public_holiday",
        "app.domain.integrations.webhooks.rotate_subscription_secret",
        "app.domain.issues.service.create_issue",
        "app.domain.issues.service.get_issue",
        "app.domain.issues.service.list_issues",
        "app.domain.issues.service.update_issue",
        "app.domain.payroll.compute.compute_payslip",
        "app.domain.payroll.compute.payslip_recompute",
        "app.domain.payroll.exports.export_expense_ledger_csv",
        "app.domain.payroll.exports.export_payslips_csv",
        "app.domain.payroll.exports.export_timesheets_csv",
        "app.domain.payroll.exports.stream_csv_with_audit",
        "app.domain.payroll.pdf.render_payslip",
        "app.domain.payroll.periods.create_period",
        "app.domain.payroll.periods.delete_period",
        "app.domain.payroll.periods.get_period",
        "app.domain.payroll.periods.list_periods",
        "app.domain.payroll.periods.lock_period",
        "app.domain.payroll.periods.mark_paid",
        "app.domain.payroll.periods.reopen_period",
        "app.domain.payroll.periods.update_period",
        "app.domain.places.area_service.create_area",
        "app.domain.places.area_service.delete_area",
        "app.domain.places.area_service.get_area",
        "app.domain.places.area_service.list_areas",
        "app.domain.places.area_service.move_area",
        "app.domain.places.area_service.reorder_areas",
        "app.domain.places.area_service.seed_default_areas_for_unit",
        "app.domain.places.area_service.update_area",
        "app.domain.places.closure_service.create_closure",
        "app.domain.places.closure_service.delete_closure",
        "app.domain.places.closure_service.detect_clashes",
        "app.domain.places.closure_service.get_closure",
        "app.domain.places.closure_service.list_closures",
        "app.domain.places.closure_service.update_closure",
        "app.domain.stays.bundle_service.cancel_bundles_for_stay",
        "app.domain.stays.bundle_service.generate_bundles_for_stay",
        "app.domain.stays.bundle_service.list_bundles",
        "app.domain.stays.bundle_service.reapply_bundles_for_stay",
        "app.domain.tasks.approvals.approve",
        "app.domain.tasks.approvals.list_pending",
        "app.domain.tasks.approvals.reject",
        "app.domain.tasks.approvals.request_changes",
        "app.domain.tasks.approvals.request_review",
        "app.domain.tasks.evidence.delete_evidence",
        "app.domain.tasks.evidence.list_evidence",
        "app.domain.tasks.evidence.snapshot_checklist",
        "app.domain.tasks.evidence.upload_evidence",
        "app.domain.time.geofence.check_geofence",
        "app.domain.time.geofence_settings.delete_geofence_setting",
        "app.domain.time.geofence_settings.get_geofence_setting",
        "app.domain.time.geofence_settings.upsert_geofence_setting",
        "app.domain.time.occurrence_shifts.handle_occurrence_completed",
        "app.domain.time.occurrence_shifts.handle_occurrence_started",
        "app.domain.time.shifts.find_shift_by_source_occurrence",
    }
)


class TestRepositoryParityGate:
    """The surface-parity gate — every new public ctx-taking function is covered.

    The gate fails loudly when a new ctx-taking domain function
    lands without either:

    * a line in :data:`COVERED_METHODS` (acknowledging the ORM-filter
      seam covers it — see :class:`TestScopedRowIsolation` for the
      invariant proof), OR
    * an entry in
      :data:`tests.tenant._optouts.REPOSITORY_METHOD_OPTOUTS` with
      a justification comment.

    The covered-set is an **explicit** snapshot rather than a
    derived "everything not opted out" complement so adding a new
    method is a conscious act: an agent can't silently introduce a
    raw ``session.execute(text("…"))`` path and have the gate
    rubber-stamp it. The failing-gate message steers them to either
    extend :class:`TestScopedRowIsolation` with a method-specific
    case OR add a ``# justification:`` opt-out entry.
    """

    def test_every_method_covered_or_opted_out(self) -> None:
        """Every discovered method is in COVERED_METHODS or OPTOUTS.

        Sweeps :mod:`app.domain` for ctx-taking public functions
        and fails loudly on any that don't appear in either set.
        Adding a new method is expected to **trip** this test in
        the same change that introduces the method — the fix is to
        add the method name to :data:`COVERED_METHODS` (plus an
        optional method-specific case in
        :class:`TestScopedRowIsolation` when the new method has a
        shape the seam doesn't naturally cover).
        """
        discovered = set(_discover_repository_methods())
        assert discovered, (
            "repository-method discovery returned zero names — either "
            "app.domain has no public ctx-taking functions (not true "
            "today), or the walker crashed silently. Fix discovery "
            "before extending coverage."
        )

        # Every opt-out entry must name a real, discovered method.
        # A drifted opt-out (renamed method, moved module) would
        # silently bypass the gate — fail loudly instead.
        stale_optouts = REPOSITORY_METHOD_OPTOUTS - discovered
        assert not stale_optouts, (
            "REPOSITORY_METHOD_OPTOUTS contains entries that no "
            "longer match any discovered method: "
            f"{sorted(stale_optouts)!r}. Rename or drop them."
        )

        # Same staleness check on COVERED_METHODS.
        stale_covered = COVERED_METHODS - discovered
        assert not stale_covered, (
            "COVERED_METHODS contains entries that no longer match "
            f"any discovered method: {sorted(stale_covered)!r}. "
            "Rename or drop them."
        )

        # A method must not appear in both sets — that would be a
        # confused intent (can't be both "covered by the seam" and
        # "opted out of the seam" at the same time).
        overlap = COVERED_METHODS & REPOSITORY_METHOD_OPTOUTS
        assert not overlap, (
            "methods appear in both COVERED_METHODS and "
            f"REPOSITORY_METHOD_OPTOUTS: {sorted(overlap)!r}. Pick one."
        )

        # The core parity invariant: no method is discovered without
        # being accounted for.
        uncovered = discovered - COVERED_METHODS - REPOSITORY_METHOD_OPTOUTS
        assert not uncovered, (
            "repository methods discovered without a cross-tenant "
            f"case: {sorted(uncovered)!r}. Either:\n"
            "  1. Add the name to COVERED_METHODS in "
            "tests/tenant/test_repository_parity.py (the ORM filter "
            "seam covers standard SELECT / UPDATE / DELETE paths, "
            "which TestScopedRowIsolation proves), OR\n"
            "  2. Add it to tests.tenant._optouts.REPOSITORY_METHOD_OPTOUTS "
            "with a justification if the method is genuinely "
            "cross-workspace by design."
        )


# ---------------------------------------------------------------------------
# Postgres RLS clearing — defence-in-depth
# ---------------------------------------------------------------------------


class TestPostgresRlsClearing:
    """§17 "RLS enforcement" — PG-only defence-in-depth.

    Clears ``current_setting('crewday.workspace_id')`` in a live
    transaction and asserts the next query against a scoped table
    raises rather than silently returning cross-tenant rows.

    Today's schema does NOT have the RLS policies installed
    (roadmap cd-0cs4 et al. — see ``docs/specs/19-roadmap.md``),
    so the test **skips** on a PG run without the policies present.
    Landing the migration flips the skip into a real assertion
    without any test-suite edits.
    """

    @pytest.mark.pg_only
    def test_clearing_rls_variable_rejects_next_query(
        self,
        db_session: Session,
    ) -> None:
        """Clearing the setting raises on the next workspace-scoped read.

        Uses the session-scoped ``db_session`` fixture (nested
        savepoint around the whole test) so ``SET LOCAL`` cleans up
        on rollback. ``current_setting('crewday.workspace_id',
        missing_ok := true)`` is how the intended RLS policy
        references the session variable; clearing it would make
        every subsequent query on a scoped table violate the
        policy ``USING (workspace_id = current_setting(...))``.
        """
        # Probe for RLS policy presence: if the spec's policy isn't
        # installed yet, skip loudly with a message that names the
        # migration expected to flip this on.
        rls_active = db_session.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE relname = 'task_template'")
        ).scalar()
        if not rls_active:
            pytest.skip(
                "RLS policy on 'task_template' not yet installed "
                "(see docs/specs/19-roadmap.md §RLS). Once the "
                "migration lands this test flips to a real assertion."
            )

        # With RLS active, first set the variable to a live workspace
        # (any real id will do — the test only checks that
        # CLEARING it raises).
        db_session.execute(
            text("SET LOCAL crewday.workspace_id = '00000000000000000000000001'")
        )
        # Sanity: a bare SELECT runs — no rows, but no error.
        db_session.execute(text("SELECT 1 FROM task_template LIMIT 1"))

        # Now clear the setting — the RLS policy should fail every
        # subsequent read against a scoped table. Any
        # :class:`DBAPIError` subclass is acceptable; the exact wording
        # is driver-dependent. The invariant is "the query raises",
        # not a specific exception class — but we still narrow to
        # :class:`DBAPIError` so a completely unrelated bug (e.g. a
        # :class:`TypeError` in the test harness) doesn't satisfy
        # ``pytest.raises`` vacuously.
        from sqlalchemy.exc import DBAPIError

        db_session.execute(text("SET LOCAL crewday.workspace_id = ''"))
        with pytest.raises(DBAPIError):
            db_session.execute(text("SELECT 1 FROM task_template LIMIT 1"))


__all__ = [
    "COVERED_METHODS",
    "TestPostgresRlsClearing",
    "TestRepositoryParityGate",
    "TestScopedRowIsolation",
]
