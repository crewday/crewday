"""Instructions domain service — workspace-scoped CRUD + version bump.

Owns five operations on standing instructions (SOPs, house rules,
safety notes) per ``docs/specs/07-instructions-kb.md`` §"Data model"
/ §"Editing semantics" + ``docs/specs/02-domain-model.md``
§"instruction" / §"instruction_version":

* :func:`create` — mints the ``instruction`` row + its first
  ``instruction_version`` (``version_num = 1``) atomically inside the
  caller's UoW. Emits one ``instruction.created`` audit row whose
  diff carries the freshly-minted ``version_id``.

* :func:`update_metadata` — partial update of title / scope / tags
  WITHOUT minting a fresh version. Body content is untouched and
  ``current_version_id`` does not move. Emits one
  ``instruction.metadata_updated`` audit row whose diff carries a
  before/after of the changed columns. Refuses to write on an
  archived instruction (:class:`ArchivedInstructionError`).

* :func:`update_body` — body edit. Hashes the new body
  (``sha256(body_md.encode("utf-8"))`` — matches the cd-d00j backfill
  exactly so existing rows + new rows share a hash convention) and
  compares against the latest version. **Idempotent**: when the new
  hash equals the current version's hash, no fresh version is minted
  and no audit row fires. When the hashes differ, mints v ``current
  + 1`` and re-points ``current_version_id``. The bump is monotonic:
  the new version-num is the per-instruction running max + 1, NOT
  the parent instruction's "version count + 1" — the two diverge if
  a future task hard-deletes a version row, but the running-max
  contract still holds. Emits one ``instruction.body_updated`` audit
  row whose diff carries the new ``revision_id`` (per cd-oyq
  acceptance: "Audit rows include ``revision_id`` for body edits").
  Refuses to write on an archived instruction.

* :func:`archive` — sets ``archived_at = clock.now()``. Idempotent:
  archiving an already-archived row is a no-op for the DB column but
  still writes one ``instruction.archived`` audit row so the
  forensic trail stays linear (matches the
  :func:`app.services.employees.service.archive_employee` shape).
  Restoration is intentionally NOT shipped here — spec §"Archived /
  Retractable" describes restore as part of the version-history
  surface (cd-t5j); cd-oyq leaves the seam open via
  :func:`restore_to_revision` below.

* :func:`restore_to_revision` — version-history seam. Raises
  :class:`NotImplementedError` until cd-t5j (``feat(instructions):
  version history view + restore-to-revision``) lands; the signature
  is pinned so the HTTP layer (cd-xkfe) can wire its route now and
  the version-history work fills it in without re-shaping the
  caller.

**Scope semantics** (§"instruction" constraint table):

* ``global`` ⇒ no property / area set. Stored as ``scope_kind =
  "workspace", scope_id = NULL`` against the v1 wider scope-kind
  enum (cd-bce). The model docstring documents the scope-kind
  projection: spec §07's narrower ``global | property | area``
  surface lands on top of the wider taxonomy at the service
  boundary.
* ``property`` ⇒ ``property_id`` set, ``area_id`` NULL.
* ``area`` ⇒ ``area_id`` set; ``property_id`` is mirrored from the
  area's parent and consistency-checked against any caller-supplied
  override.

Scope validation raises :class:`ScopeValidationError` on every
constraint-table violation (each error names the offending field).
The router maps it to a 422 with field-level detail.

**Tag normalisation.** Tags are trimmed, lower-cased, and deduped
case-insensitively before hitting the DB; the post-normalised list
caps at 20 entries (spec §"Tag chips"). The cap counts unique tags,
so a list of 30 entries that dedupe to 12 is fine; 30 entries that
dedupe to 21 raises :class:`TagValidationError` with the offending
field name. Empty tags (post-trim) are dropped.

**Capability.** Every mutation runs through
``instructions.edit`` (workspace OR property scope per
``docs/specs/05-employees-and-roles.md`` §"Rule-driven actions" —
default-allow ``owners, managers``). The current cd-oyq slice
checks the workspace-scoped capability; the property-scoped variant
ships when the HTTP layer (cd-xkfe) lands its property-narrow
listing endpoint.

**Tenancy.** Every read / write rides the repository seam
(:class:`~app.domain.instructions.ports.InstructionsRepository`)
which the SA concretion implements with workspace-id predicates on
every statement. The caller's
:class:`~app.tenancy.WorkspaceContext` pins ``workspace_id`` for
the entire flow.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation writes
one :mod:`app.audit` row in the same transaction.

See ``docs/specs/07-instructions-kb.md``,
``docs/specs/02-domain-model.md`` §"instruction" / §"instruction_version",
and ``docs/specs/05-employees-and-roles.md`` action ``instructions.edit``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from app.audit import write_audit
from app.authz import (
    InvalidScope,
    PermissionDenied,
    UnknownActionKey,
    require,
)
from app.domain.instructions.ports import (
    InstructionResolutionRow,
    InstructionRow,
    InstructionsRepository,
    InstructionVersionRow,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ArchivedInstructionError",
    "InstructionNotFound",
    "InstructionPermissionDenied",
    "InstructionResult",
    "InstructionScope",
    "InstructionVersionView",
    "InstructionView",
    "ResolvedInstruction",
    "ScopeValidationError",
    "TagValidationError",
    "archive",
    "create",
    "resolve_instructions",
    "restore_to_revision",
    "update_body",
    "update_metadata",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


InstructionScope = Literal["global", "property", "area"]
"""Spec-level scope enum (§07 "Properties of the system")."""

InstructionProvenance = Literal[
    "scope:area",
    "scope:property",
    "scope:global",
    "link:task_template",
    "link:schedule",
    "link:work_role",
    "link:task",
    "link:asset",
    "link:stay",
]


# ``scope_kind`` values stored on the ``instruction`` row. Spec §07
# uses the narrower ``global | property | area`` enum; the v1 model
# (cd-bce) carries the wider taxonomy. The service projects between
# the two: ``"global"`` ⇒ ``"workspace"``; the others map identity-
# wise.
_SCOPE_KIND_FOR_SCOPE: dict[InstructionScope, str] = {
    "global": "workspace",
    "property": "property",
    "area": "area",
}

_MAX_TAGS = 20


@dataclass(frozen=True, slots=True)
class InstructionView:
    """Immutable read projection of an instruction row.

    Carries the spec-narrow ``scope`` enum (not the wider model
    ``scope_kind``) so the HTTP layer (cd-xkfe) and tests don't need
    to know about the wider taxonomy. The
    :func:`_view_from_row` projector handles the narrow ⇄ wide
    mapping.
    """

    id: str
    workspace_id: str
    slug: str
    title: str
    scope: InstructionScope
    property_id: str | None
    area_id: str | None
    current_version_id: str | None
    tags: tuple[str, ...]
    archived_at: datetime | None
    created_by: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InstructionVersionView:
    """Immutable read projection of an instruction_version row."""

    id: str
    instruction_id: str
    version_num: int
    body_md: str
    body_hash: str
    author_id: str | None
    change_note: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InstructionResult:
    """Bundled return value for paths that touch instruction + version atomically.

    :func:`create` and :func:`update_body` always return both shapes
    so the caller doesn't issue a follow-up read for the freshly-
    minted (or unchanged) current revision.
    """

    instruction: InstructionView
    revision: InstructionVersionView


@dataclass(frozen=True, slots=True)
class ResolvedInstruction:
    """Instruction body selected for a concrete work context."""

    instruction_id: str
    current_revision_id: str
    body_md: str
    provenance: InstructionProvenance


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InstructionNotFound(LookupError):
    """The target instruction is not visible in this workspace.

    404-equivalent. Raised when ``instruction_id`` is unknown to the
    caller's workspace — the cross-tenant collapse to "not found"
    is deliberate (§01 "tenant surface is not enumerable").
    """


class ScopeValidationError(ValueError):
    """Scope payload violates the §07 constraint table.

    422-equivalent. The :attr:`field` attribute names the offending
    parameter (``"scope"``, ``"property_id"``, ``"area_id"``) so the
    HTTP layer can surface field-level detail.
    """

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


class TagValidationError(ValueError):
    """Tag payload violates the §07 normalisation rules.

    422-equivalent. The :attr:`field` is always ``"tags"``; the
    attribute carries the cap (or other limit) the caller hit.
    """

    field = "tags"

    def __init__(self, message: str, *, limit: int) -> None:
        super().__init__(message)
        self.limit = limit


class ArchivedInstructionError(RuntimeError):
    """Caller tried to mutate an archived instruction.

    409-/422-equivalent (the HTTP layer chooses the envelope). Spec
    §"Retractable": archived instructions cannot be edited until
    restored.
    """


class InstructionPermissionDenied(PermissionError):
    """Caller lacks ``instructions.edit`` on this workspace.

    403-equivalent. Raised by the capability gate on every mutation
    path.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or :class:`SystemClock`."""
    return (clock if clock is not None else SystemClock()).now()


def _hash_body(body_md: str) -> str:
    """Return ``sha256(body_md)`` as a 64-char hex digest.

    Matches the cd-d00j migration backfill verbatim: no normalisation,
    raw UTF-8 bytes through SHA-256. Existing rows + new rows share
    one hash convention so the idempotency check stays consistent.
    Any future normalisation pass MUST land in a paired migration
    that re-hashes existing rows.
    """
    return hashlib.sha256(body_md.encode("utf-8")).hexdigest()


def _normalise_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """Return the trimmed, lower-cased, deduped tag tuple.

    Empty tags (post-trim) are dropped. Order is preserved for the
    first occurrence; subsequent duplicates (case-insensitive) are
    skipped. Raises :class:`TagValidationError` when the
    post-normalised count exceeds :data:`_MAX_TAGS` so the caller
    sees a structured 422 instead of a silent truncation.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        if not isinstance(raw, str):
            raise TagValidationError(
                f"tag must be str, got {type(raw).__name__}",
                limit=_MAX_TAGS,
            )
        cleaned = raw.strip().lower()
        if not cleaned:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    if len(out) > _MAX_TAGS:
        raise TagValidationError(
            f"too many tags: {len(out)} (cap is {_MAX_TAGS})",
            limit=_MAX_TAGS,
        )
    return tuple(out)


def _resolve_scope(
    repo: InstructionsRepository,
    *,
    workspace_id: str,
    scope: InstructionScope,
    property_id: str | None,
    area_id: str | None,
) -> tuple[str, str | None]:
    """Validate the scope payload and return ``(scope_kind, scope_id)``.

    Enforces the §"instruction" constraint table:

    * ``global`` ⇒ ``property_id`` AND ``area_id`` MUST be ``None``.
    * ``property`` ⇒ ``property_id`` MUST be set; ``area_id`` MUST be
      ``None``; the property MUST belong to the caller's workspace.
    * ``area`` ⇒ ``area_id`` MUST be set; the area MUST exist in this
      workspace; ``property_id`` (if supplied) MUST equal the area's
      parent ``property_id`` (the spec calls this "mirrored ...
      consistency-checked"). The v1 row stores the area id in
      ``scope_id``; repository projections carry the parent property
      back out on reads.

    Returns the ``(scope_kind, scope_id)`` pair the model column
    expects.
    """
    if scope == "global":
        if property_id is not None:
            raise ScopeValidationError(
                "global scope must not carry property_id",
                field="property_id",
            )
        if area_id is not None:
            raise ScopeValidationError(
                "global scope must not carry area_id",
                field="area_id",
            )
        return _SCOPE_KIND_FOR_SCOPE["global"], None

    if scope == "property":
        if property_id is None:
            raise ScopeValidationError(
                "property scope requires property_id",
                field="property_id",
            )
        if area_id is not None:
            raise ScopeValidationError(
                "property scope must not carry area_id",
                field="area_id",
            )
        if not repo.property_exists_in_workspace(
            workspace_id=workspace_id, property_id=property_id
        ):
            raise ScopeValidationError(
                f"property {property_id!r} not found in workspace",
                field="property_id",
            )
        return _SCOPE_KIND_FOR_SCOPE["property"], property_id

    # scope == "area"
    if area_id is None:
        raise ScopeValidationError(
            "area scope requires area_id",
            field="area_id",
        )
    area = repo.get_area(workspace_id=workspace_id, area_id=area_id)
    if area is None:
        raise ScopeValidationError(
            f"area {area_id!r} not found in workspace",
            field="area_id",
        )
    if property_id is not None and property_id != area.property_id:
        raise ScopeValidationError(
            f"property_id {property_id!r} does not match area's parent "
            f"{area.property_id!r}",
            field="property_id",
        )
    # The model only carries ``scope_id`` (no separate property
    # mirror column on this v1 schema). The repository projects the
    # area's parent property back into ``InstructionRow.property_id``.
    return _SCOPE_KIND_FOR_SCOPE["area"], area_id


def _view_from_row(row: InstructionRow) -> InstructionView:
    """Project an :class:`InstructionRow` into the spec-narrow view.

    Maps the model's wider ``scope_kind`` taxonomy back onto the
    spec's ``global | property | area`` enum. The router-facing
    surface only ever sees the narrow shape.
    """
    scope: InstructionScope
    property_id: str | None
    area_id: str | None
    if row.scope_kind == "workspace":
        scope, property_id, area_id = "global", None, None
    elif row.scope_kind == "property":
        scope, property_id, area_id = "property", row.scope_id, None
    elif row.scope_kind == "area":
        scope = "area"
        area_id = row.scope_id
        property_id = row.property_id
    else:
        # The wider model taxonomy includes ``template | asset | stay
        # | role`` from cd-bce. Spec §07 doesn't surface these as
        # spec-level scopes; if a future cd-* widens the spec, the
        # mapping above grows. Until then, an unexpected value here
        # is a programming error (a row written outside this service)
        # — surface as a 500 rather than silently picking a scope.
        raise ValueError(
            f"unexpected scope_kind {row.scope_kind!r} on instruction {row.id!r}; "
            "service only writes 'workspace' / 'property' / 'area'"
        )
    return InstructionView(
        id=row.id,
        workspace_id=row.workspace_id,
        slug=row.slug,
        title=row.title,
        scope=scope,
        property_id=property_id,
        area_id=area_id,
        current_version_id=row.current_version_id,
        tags=row.tags,
        archived_at=row.archived_at,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _version_view_from_row(row: InstructionVersionRow) -> InstructionVersionView:
    return InstructionVersionView(
        id=row.id,
        instruction_id=row.instruction_id,
        version_num=row.version_num,
        body_md=row.body_md,
        body_hash=row.body_hash,
        author_id=row.author_id,
        change_note=row.change_note,
        created_at=row.created_at,
    )


def _require_edit(repo: InstructionsRepository, ctx: WorkspaceContext) -> None:
    """Enforce ``instructions.edit`` on the caller's workspace or raise.

    Raises :class:`InstructionPermissionDenied` on a deny; a
    misconfigured action catalog (unknown key, invalid scope) is a
    server-side bug — surfaces as :class:`RuntimeError` so the
    router answers 500, not 403. Same wrapper shape as
    :func:`app.services.employees.service._require_edit_other`.
    """
    try:
        require(
            repo.session,
            ctx,
            action_key="instructions.edit",
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for 'instructions.edit': {exc!s}"
        ) from exc
    except PermissionDenied as exc:
        raise InstructionPermissionDenied(
            f"actor {ctx.actor_id!r} lacks instructions.edit on workspace "
            f"{ctx.workspace_id!r}"
        ) from exc


def _require_not_archived(row: InstructionRow) -> None:
    """Refuse a write on an archived instruction (§"Retractable")."""
    if row.archived_at is not None:
        raise ArchivedInstructionError(
            f"instruction {row.id!r} is archived since {row.archived_at.isoformat()}; "
            "cannot edit until restored"
        )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_instructions(
    repo: InstructionsRepository,
    ctx: WorkspaceContext,
    *,
    property_id: str | None = None,
    area_id: str | None = None,
    template_id: str | None = None,
    schedule_id: str | None = None,
    task_id: str | None = None,
    asset_id: str | None = None,
    stay_id: str | None = None,
    work_role_id: str | None = None,
) -> list[ResolvedInstruction]:
    """Return the deduped, ordered instruction set for a work context."""
    resolved_area_id: str | None = None
    resolved_property_id = property_id
    if area_id is not None:
        area = repo.get_area(workspace_id=ctx.workspace_id, area_id=area_id)
        if area is not None:
            resolved_area_id = area_id
            resolved_property_id = area.property_id

    out: list[ResolvedInstruction] = []
    seen: set[str] = set()

    def append_rows(
        rows: Sequence[InstructionResolutionRow],
        provenance: InstructionProvenance,
    ) -> None:
        for row in rows:
            if row.instruction_id in seen:
                continue
            seen.add(row.instruction_id)
            out.append(
                ResolvedInstruction(
                    instruction_id=row.instruction_id,
                    current_revision_id=row.current_revision_id,
                    body_md=row.body_md,
                    provenance=provenance,
                )
            )

    if resolved_area_id is not None:
        append_rows(
            repo.list_live_current_by_scope(
                workspace_id=ctx.workspace_id,
                scope_kind="area",
                scope_id=resolved_area_id,
            ),
            "scope:area",
        )
    if resolved_property_id is not None:
        append_rows(
            repo.list_live_current_by_scope(
                workspace_id=ctx.workspace_id,
                scope_kind="property",
                scope_id=resolved_property_id,
            ),
            "scope:property",
        )
    append_rows(
        repo.list_live_current_by_scope(
            workspace_id=ctx.workspace_id,
            scope_kind="workspace",
            scope_id=None,
        ),
        "scope:global",
    )

    linked_targets: tuple[
        tuple[str, str | None, InstructionProvenance],
        ...,
    ] = (
        ("task_template", template_id, "link:task_template"),
        ("schedule", schedule_id, "link:schedule"),
        ("work_role", work_role_id, "link:work_role"),
        ("task", task_id, "link:task"),
        ("asset", asset_id, "link:asset"),
        ("stay", stay_id, "link:stay"),
    )
    for target_kind, target_id, provenance in linked_targets:
        if target_id is None:
            continue
        append_rows(
            repo.list_live_current_by_link(
                workspace_id=ctx.workspace_id,
                target_kind=target_kind,
                target_id=target_id,
            ),
            provenance,
        )

    return out


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create(
    repo: InstructionsRepository,
    ctx: WorkspaceContext,
    *,
    slug: str,
    title: str,
    body_md: str,
    scope: InstructionScope,
    tags: Sequence[str] = (),
    property_id: str | None = None,
    area_id: str | None = None,
    change_note: str | None = None,
    clock: Clock | None = None,
) -> InstructionResult:
    """Create an instruction + its first revision atomically.

    Validates scope + tags, mints the ``instruction`` row with
    ``current_version_id = NULL``, mints v1 of
    ``instruction_version`` carrying the body + its sha256 hash,
    flips ``current_version_id`` to v1's id, and writes one
    ``instruction.created`` audit row whose diff carries the
    freshly-minted version_id.

    ``slug`` is a workspace-unique handle (UNIQUE
    ``(workspace_id, slug)`` per cd-bce). Callers are responsible for
    deriving / picking the slug; the service does not auto-slugify
    today. If the slug collides, the underlying SA write surfaces an
    :class:`~sqlalchemy.exc.IntegrityError` — the HTTP layer maps it
    to 409. The cd-oyq slice does not pre-check the slug to avoid a
    TOCTOU window between the SELECT and the INSERT (the UNIQUE
    constraint is the source of truth).

    Tag normalisation runs before the scope check so the caller sees
    ``TagValidationError`` (422 / "tags") rather than a wasted scope
    DB read.
    """
    _require_edit(repo, ctx)
    normalised_tags = _normalise_tags(tags)
    scope_kind, scope_id = _resolve_scope(
        repo,
        workspace_id=ctx.workspace_id,
        scope=scope,
        property_id=property_id,
        area_id=area_id,
    )
    now = _now(clock)
    instruction_id = new_ulid(clock=clock)
    version_id = new_ulid(clock=clock)
    body_hash = _hash_body(body_md)

    repo.insert_instruction(
        instruction_id=instruction_id,
        workspace_id=ctx.workspace_id,
        slug=slug,
        title=title,
        scope_kind=scope_kind,
        scope_id=scope_id,
        tags=normalised_tags,
        created_by=ctx.actor_id,
        created_at=now,
    )
    version_row = repo.insert_version(
        version_id=version_id,
        workspace_id=ctx.workspace_id,
        instruction_id=instruction_id,
        version_num=1,
        body_md=body_md,
        body_hash=body_hash,
        author_id=ctx.actor_id,
        change_note=change_note,
        created_at=now,
    )
    instruction_row = repo.set_current_version(
        workspace_id=ctx.workspace_id,
        instruction_id=instruction_id,
        version_id=version_id,
    )

    write_audit(
        repo.session,
        ctx,
        entity_kind="instruction",
        entity_id=instruction_id,
        action="instruction.created",
        diff={
            "title": title,
            "slug": slug,
            "scope": scope,
            "property_id": property_id,
            "area_id": area_id,
            "tags": list(normalised_tags),
            "revision_id": version_id,
            "version_num": 1,
        },
        clock=clock,
    )
    return InstructionResult(
        instruction=_view_from_row(instruction_row),
        revision=_version_view_from_row(version_row),
    )


def update_metadata(
    repo: InstructionsRepository,
    ctx: WorkspaceContext,
    *,
    instruction_id: str,
    title: str | None = None,
    tags: Sequence[str] | None = None,
    scope: InstructionScope | None = None,
    property_id: str | None = None,
    area_id: str | None = None,
    clock: Clock | None = None,
) -> InstructionView:
    """Partial update of metadata WITHOUT minting a fresh revision.

    Only the supplied fields are touched; absent fields keep their
    current value. ``current_version_id`` does NOT move — body
    edits live on :func:`update_body`.

    When ``scope`` is supplied the scope payload is re-validated end
    to end (the constraint table treats scope as one fused field;
    callers who flip from ``property`` to ``area`` must supply the
    new ``area_id`` in the same call). When ``scope`` is omitted but
    ``property_id`` / ``area_id`` are supplied, the call is rejected
    — the service refuses to guess at a scope change.

    No-op writes (no field actually changed) are silent: no DB
    write, no audit row. A request that toggles ``title`` to its
    current value still records an audit row only when the row
    actually changed.

    Raises :class:`InstructionNotFound` when the instruction is
    unknown to this workspace; :class:`ArchivedInstructionError`
    when the row is archived; :class:`ScopeValidationError` /
    :class:`TagValidationError` for shape errors.
    """
    _require_edit(repo, ctx)
    existing = repo.get_instruction(
        workspace_id=ctx.workspace_id, instruction_id=instruction_id
    )
    if existing is None:
        raise InstructionNotFound(instruction_id)
    _require_not_archived(existing)

    if scope is None and (property_id is not None or area_id is not None):
        raise ScopeValidationError(
            "scope must be supplied alongside property_id / area_id changes",
            field="scope",
        )

    new_scope_kind: str | None = None
    new_scope_id: str | None = None
    if scope is not None:
        new_scope_kind, new_scope_id = _resolve_scope(
            repo,
            workspace_id=ctx.workspace_id,
            scope=scope,
            property_id=property_id,
            area_id=area_id,
        )

    new_tags: tuple[str, ...] | None = None
    if tags is not None:
        new_tags = _normalise_tags(tags)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    if title is not None and title != existing.title:
        before["title"] = existing.title
        after["title"] = title
    if new_scope_kind is not None and (
        new_scope_kind != existing.scope_kind or new_scope_id != existing.scope_id
    ):
        before["scope_kind"] = existing.scope_kind
        before["scope_id"] = existing.scope_id
        after["scope_kind"] = new_scope_kind
        after["scope_id"] = new_scope_id
    if new_tags is not None and new_tags != existing.tags:
        before["tags"] = list(existing.tags)
        after["tags"] = list(new_tags)

    if not after:
        # No actual change — return the current view without an
        # audit row.
        return _view_from_row(existing)

    refreshed = repo.update_metadata(
        workspace_id=ctx.workspace_id,
        instruction_id=instruction_id,
        title=title if "title" in after else None,
        scope_kind=new_scope_kind if "scope_kind" in after else None,
        scope_id=new_scope_id if "scope_kind" in after else None,
        scope_id_provided="scope_kind" in after,
        tags=new_tags if "tags" in after else None,
    )

    write_audit(
        repo.session,
        ctx,
        entity_kind="instruction",
        entity_id=instruction_id,
        action="instruction.metadata_updated",
        diff={"before": before, "after": after},
        clock=clock,
    )
    return _view_from_row(refreshed)


def update_body(
    repo: InstructionsRepository,
    ctx: WorkspaceContext,
    *,
    instruction_id: str,
    body_md: str,
    change_note: str | None = None,
    clock: Clock | None = None,
) -> InstructionResult:
    """Mint a new revision IF the body content actually changed.

    Hashes the new body and compares against the current version's
    ``body_hash``. When equal, the call is a no-op for the DB and
    audit (matches mock UX where save is idempotent — spec
    §"Editing semantics" rationale). When different, mints v
    ``running-max + 1``, re-points ``current_version_id``, and
    writes one ``instruction.body_updated`` audit row whose diff
    carries the new ``revision_id`` (per cd-oyq acceptance: "Audit
    rows include ``revision_id`` for body edits").

    The version-num is the **running max** + 1 (loaded via
    :meth:`InstructionsRepository.get_max_version_num`), NOT
    ``current_version.version_num + 1``. The two diverge only if a
    future task hard-deletes a version row (out of scope today); the
    running-max contract preserves monotonicity even then.

    Raises :class:`InstructionNotFound` when the instruction is
    unknown to this workspace; :class:`ArchivedInstructionError`
    when the row is archived.
    """
    _require_edit(repo, ctx)
    existing = repo.get_instruction_for_update(
        workspace_id=ctx.workspace_id, instruction_id=instruction_id
    )
    if existing is None:
        raise InstructionNotFound(instruction_id)
    _require_not_archived(existing)

    new_hash = _hash_body(body_md)
    current_version = _current_version_or_raise(
        repo, workspace_id=ctx.workspace_id, instruction=existing
    )
    if current_version.body_hash == new_hash:
        # Idempotent no-op — body content is unchanged; the caller's
        # save is a re-confirmation of the existing v ``version_num``.
        return InstructionResult(
            instruction=_view_from_row(existing),
            revision=_version_view_from_row(current_version),
        )

    next_version_num = (
        repo.get_max_version_num(
            workspace_id=ctx.workspace_id, instruction_id=instruction_id
        )
        + 1
    )
    now = _now(clock)
    version_id = new_ulid(clock=clock)
    version_row = repo.insert_version(
        version_id=version_id,
        workspace_id=ctx.workspace_id,
        instruction_id=instruction_id,
        version_num=next_version_num,
        body_md=body_md,
        body_hash=new_hash,
        author_id=ctx.actor_id,
        change_note=change_note,
        created_at=now,
    )
    refreshed = repo.set_current_version(
        workspace_id=ctx.workspace_id,
        instruction_id=instruction_id,
        version_id=version_id,
    )

    write_audit(
        repo.session,
        ctx,
        entity_kind="instruction",
        entity_id=instruction_id,
        action="instruction.body_updated",
        diff={
            "revision_id": version_id,
            "version_num": next_version_num,
            "previous_revision_id": current_version.id,
            "previous_version_num": current_version.version_num,
            "body_hash": new_hash,
        },
        clock=clock,
    )
    return InstructionResult(
        instruction=_view_from_row(refreshed),
        revision=_version_view_from_row(version_row),
    )


def archive(
    repo: InstructionsRepository,
    ctx: WorkspaceContext,
    *,
    instruction_id: str,
    clock: Clock | None = None,
) -> InstructionView:
    """Soft-archive the instruction; idempotent on already-archived rows.

    Sets ``archived_at = clock.now()`` on a live row; on an already-
    archived row the column is untouched (the original archive
    instant is preserved). Either path writes one
    ``instruction.archived`` audit row with a flag indicating
    whether the row was already archived — the trail stays linear
    (matches :func:`app.services.employees.service.archive_employee`).

    Raises :class:`InstructionNotFound` when the instruction is
    unknown to this workspace.
    """
    _require_edit(repo, ctx)
    existing = repo.get_instruction(
        workspace_id=ctx.workspace_id, instruction_id=instruction_id
    )
    if existing is None:
        raise InstructionNotFound(instruction_id)

    was_archived = existing.archived_at is not None
    if was_archived:
        refreshed = existing
    else:
        now = _now(clock)
        refreshed = repo.set_archived_at(
            workspace_id=ctx.workspace_id,
            instruction_id=instruction_id,
            archived_at=now,
        )

    write_audit(
        repo.session,
        ctx,
        entity_kind="instruction",
        entity_id=instruction_id,
        action="instruction.archived",
        diff={
            "was_already_archived": was_archived,
            "archived_at": (
                refreshed.archived_at.isoformat()
                if refreshed.archived_at is not None
                else None
            ),
        },
        clock=clock,
    )
    return _view_from_row(refreshed)


def restore_to_revision(
    repo: InstructionsRepository,
    ctx: WorkspaceContext,
    *,
    instruction_id: str,
    revision_id: str,
    clock: Clock | None = None,
) -> InstructionResult:
    """Restore the instruction's current pointer to a prior revision.

    **Seam — not implemented in cd-oyq.** Ships in
    ``cd-t5j`` (``feat(instructions): version history view +
    restore-to-revision``). The signature is pinned here so the
    HTTP route lands in cd-xkfe without re-shaping the caller; the
    body raises :class:`NotImplementedError` until the version-
    history work fills it in.
    """
    del repo, ctx, instruction_id, revision_id, clock
    raise NotImplementedError(
        "ships in p6.version.history (cd-t5j: version history view + "
        "restore-to-revision)"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _current_version_or_raise(
    repo: InstructionsRepository,
    *,
    workspace_id: str,
    instruction: InstructionRow,
) -> InstructionVersionRow:
    """Return the current version row or raise :class:`RuntimeError`.

    A null ``current_version_id`` after :func:`create` finishes is
    a data-integrity bug — the create path always flips the pointer
    before returning. Surface the inconsistency as a 500-style error
    rather than a misleading 404; the caller's audit trail will
    capture the broken row id.
    """
    if instruction.current_version_id is None:
        raise RuntimeError(
            f"instruction {instruction.id!r} has no current_version_id; "
            "create() must have flipped the pointer atomically — investigate"
        )
    version = repo.get_version(
        workspace_id=workspace_id, version_id=instruction.current_version_id
    )
    if version is None:
        raise RuntimeError(
            f"instruction {instruction.id!r} points at version "
            f"{instruction.current_version_id!r} which is missing — investigate"
        )
    return version
