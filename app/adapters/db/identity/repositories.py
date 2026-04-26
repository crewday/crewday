"""SA-backed repositories implementing the identity-context Protocol seams.

The concrete classes here adapt SQLAlchemy ``Session`` work to the
Protocol surfaces declared on the identity context's port files:

* :class:`SqlAlchemyMeScheduleQueryRepository` — wraps the five
  SELECTs the schedule aggregator runs (cd-lot5). Reads from
  :mod:`app.adapters.db.availability.models` (rota / overrides /
  leaves), :mod:`app.adapters.db.holidays.models` (workspace
  holidays), and :mod:`app.adapters.db.tasks.models` (assigned
  occurrence refs). Consumed by
  :mod:`app.domain.identity.me_schedule`.

* :class:`SqlAlchemyEmailChangeRepository` — wraps the four ORM
  classes the self-service email-change flow touches (cd-24im):
  :class:`User` (display-name + email swap),
  :class:`PasskeyCredential` (cool-off lookup),
  :class:`EmailChangePending` (CRUD), and
  :func:`canonicalise_email`. Consumed by
  :mod:`app.domain.identity.email_change`.

Reaches into multiple adapter packages directly. Adapter-to-adapter
imports are allowed by the import-linter — only ``app.domain →
app.adapters`` is forbidden. We re-use the row-projection helpers
(``_to_weekly_row`` / ``_to_override_row`` / ``_to_leave_row``)
from :mod:`app.adapters.db.availability.repositories` rather than
duplicating the field-by-field copies; both adapters convert the
same ORM types into the same seam-level rows, so a single source
of truth keeps them aligned when columns land on the underlying
tables.

The repos carry an open ``Session`` and never commit — the caller's
UoW owns the transaction boundary (§01 "Key runtime invariants" #3).
Mutating methods flush so the caller's next read (and the audit
writer's FK reference to ``entity_id``) sees the new row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from datetime import date as _date_cls

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.availability.models import (
    UserAvailabilityOverride,
    UserLeave,
    UserWeeklyAvailability,
)

# Re-use the cd-r5j2 / cd-2upg row-projection helpers from the
# availability adapter rather than copy-pasting the field-by-field
# converters. Both files own the conversion of the same ORM types
# into the same seam-level rows declared on
# :mod:`app.domain.identity.availability_ports`; duplicating them
# would invite drift the moment a column lands. Adapter-to-adapter
# imports are allowed by the import-linter (only ``app.domain →
# app.adapters`` is forbidden), and the underscore-prefixed
# crossing mirrors the same trade-off
# :mod:`app.domain.identity.me_schedule` accepts when re-using the
# sibling services' ``_row_to_view`` projections.
from app.adapters.db.availability.repositories import (
    _to_leave_row,
    _to_override_row,
    _to_weekly_row,
)
from app.adapters.db.holidays.models import PublicHoliday
from app.adapters.db.identity.models import (
    EmailChangePending,
    PasskeyCredential,
    User,
    canonicalise_email,
)
from app.adapters.db.tasks.models import Occurrence
from app.domain.identity.availability_ports import (
    UserAvailabilityOverrideRow,
    UserLeaveRow,
    UserWeeklyAvailabilityRow,
)
from app.domain.identity.email_change_ports import (
    EmailChangePendingRow,
    EmailChangeRepository,
    UserIdentityRow,
)
from app.domain.identity.me_schedule_ports import (
    MeScheduleQueryRepository,
    OccurrenceRefRow,
    PublicHolidayRow,
)
from app.tenancy import tenant_agnostic

__all__ = [
    "SqlAlchemyEmailChangeRepository",
    "SqlAlchemyMeScheduleQueryRepository",
]


# ---------------------------------------------------------------------------
# Row projections
# ---------------------------------------------------------------------------


def _to_holiday_row(row: PublicHoliday) -> PublicHolidayRow:
    """Project an ORM ``PublicHoliday`` into the seam-level row."""
    return PublicHolidayRow(
        id=row.id,
        name=row.name,
        date=row.date,
        country=row.country,
        scheduling_effect=row.scheduling_effect,
        reduced_starts_local=row.reduced_starts_local,
        reduced_ends_local=row.reduced_ends_local,
        payroll_multiplier=row.payroll_multiplier,
    )


def _to_occurrence_ref_row(row: Occurrence) -> OccurrenceRefRow:
    """Project an ORM ``Occurrence`` into the lightweight calendar ref.

    Falls back to ``starts_at.isoformat()`` when ``scheduled_for_local``
    is null. The cd-22e generator always populates the local column,
    so this fallback only fires for legacy rows (or hand-seeded test
    fixtures that skip the column); keeping it deterministic in the
    adapter avoids a JSON ``null`` leaking into a UI that expects a
    renderable timestamp.
    """
    if row.scheduled_for_local is not None:
        local = row.scheduled_for_local
    else:
        local = row.starts_at.isoformat()
    return OccurrenceRefRow(id=row.id, scheduled_for_local=local)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class SqlAlchemyMeScheduleQueryRepository(MeScheduleQueryRepository):
    """SA-backed concretion of :class:`MeScheduleQueryRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never writes —
    the schedule feed is pure aggregation. Reads run inside the
    caller's UoW so the five SELECTs see a consistent snapshot.

    Defence-in-depth pins every read to the caller's ``workspace_id``
    even though the ORM tenant filter already narrows them; a
    misconfigured filter must fail loud, not silently.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_weekly_pattern(
        self,
        *,
        workspace_id: str,
        user_id: str,
    ) -> Sequence[UserWeeklyAvailabilityRow]:
        rows = self._session.scalars(
            select(UserWeeklyAvailability)
            .where(
                UserWeeklyAvailability.workspace_id == workspace_id,
                UserWeeklyAvailability.user_id == user_id,
            )
            .order_by(UserWeeklyAvailability.weekday.asc())
        ).all()
        return [_to_weekly_row(r) for r in rows]

    def list_overrides_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        from_date: _date_cls,
        to_date: _date_cls,
    ) -> Sequence[UserAvailabilityOverrideRow]:
        rows = self._session.scalars(
            select(UserAvailabilityOverride)
            .where(
                UserAvailabilityOverride.workspace_id == workspace_id,
                UserAvailabilityOverride.user_id == user_id,
                UserAvailabilityOverride.deleted_at.is_(None),
                UserAvailabilityOverride.date >= from_date,
                UserAvailabilityOverride.date <= to_date,
            )
            .order_by(UserAvailabilityOverride.date.asc())
        ).all()
        return [_to_override_row(r) for r in rows]

    def list_leaves_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        from_date: _date_cls,
        to_date: _date_cls,
    ) -> Sequence[UserLeaveRow]:
        # Standard interval-overlap predicate — see the
        # :class:`MeScheduleQueryRepository` Protocol docstring for
        # the §06 wording. A leave covers the window iff
        # ``starts_on <= to_date AND ends_on >= from_date``.
        rows = self._session.scalars(
            select(UserLeave)
            .where(
                UserLeave.workspace_id == workspace_id,
                UserLeave.user_id == user_id,
                UserLeave.deleted_at.is_(None),
                UserLeave.starts_on <= to_date,
                UserLeave.ends_on >= from_date,
            )
            .order_by(UserLeave.starts_on.asc())
        ).all()
        return [_to_leave_row(r) for r in rows]

    def list_holidays_in_window(
        self,
        *,
        workspace_id: str,
        from_date: _date_cls,
        to_date: _date_cls,
    ) -> Sequence[PublicHolidayRow]:
        rows = self._session.scalars(
            select(PublicHoliday)
            .where(
                PublicHoliday.workspace_id == workspace_id,
                PublicHoliday.deleted_at.is_(None),
                PublicHoliday.date >= from_date,
                PublicHoliday.date <= to_date,
            )
            .order_by(PublicHoliday.date.asc())
        ).all()
        return [_to_holiday_row(r) for r in rows]

    def list_assigned_occurrences_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> Sequence[OccurrenceRefRow]:
        # Walks the ``ix_occurrence_workspace_assignee_starts``
        # composite index — leading ``workspace_id`` carries the tenant
        # filter, and ``starts_at`` ranges inside the index.
        rows = self._session.scalars(
            select(Occurrence)
            .where(
                Occurrence.workspace_id == workspace_id,
                Occurrence.assignee_user_id == user_id,
                Occurrence.starts_at >= window_start_utc,
                Occurrence.starts_at <= window_end_utc,
            )
            .order_by(Occurrence.starts_at.asc())
        ).all()
        return [_to_occurrence_ref_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Email-change repository (cd-24im)
# ---------------------------------------------------------------------------


def _to_user_identity_row(row: User) -> UserIdentityRow:
    """Project an ORM :class:`User` into the seam-level identity row.

    Narrow projection — only the four columns email-change touches.
    Frozen so the domain never mutates the ORM-managed instance
    through a shared reference.
    """
    return UserIdentityRow(
        id=row.id,
        email=row.email,
        email_lower=row.email_lower,
        display_name=row.display_name,
    )


def _to_email_change_pending_row(row: EmailChangePending) -> EmailChangePendingRow:
    """Project an ORM :class:`EmailChangePending` into the seam-level row.

    Field-by-field copy. The ``revert_jti`` / ``verified_at`` /
    ``revert_expires_at`` / ``reverted_at`` columns may be NULL when
    the row is still in the request-pending state — the projection
    propagates the ``None``s verbatim so the domain branches on the
    same lifecycle the model docstring pins.
    """
    return EmailChangePendingRow(
        id=row.id,
        user_id=row.user_id,
        request_jti=row.request_jti,
        revert_jti=row.revert_jti,
        previous_email=row.previous_email,
        previous_email_lower=row.previous_email_lower,
        new_email=row.new_email,
        new_email_lower=row.new_email_lower,
        created_at=row.created_at,
        verified_at=row.verified_at,
        revert_expires_at=row.revert_expires_at,
        reverted_at=row.reverted_at,
    )


class SqlAlchemyEmailChangeRepository(EmailChangeRepository):
    """SA-backed concretion of :class:`EmailChangeRepository` (cd-24im).

    Wraps an open :class:`~sqlalchemy.orm.Session` and never commits —
    the caller's UoW owns the transaction boundary (§01 "Key runtime
    invariants" #3). Mutating methods flush so the audit writer's FK
    reference to ``entity_id`` (and any peer read in the same UoW)
    sees the new row.

    Email-change is identity-scoped — every read and write runs under
    :func:`app.tenancy.tenant_agnostic` because the rows it touches
    have no ``workspace_id`` column. The wrapping is centralised here
    so the domain service does not have to litter its callsites with
    the context manager.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        Exposed for callers that need to thread the same UoW through
        :func:`app.audit.write_audit` (which still takes a concrete
        ``Session`` today). Drops when the audit writer gains its own
        Protocol port. Returns the SA :class:`Session` even though
        the Protocol declares :class:`object` — see the Protocol's
        ``session`` property docstring for the reasoning.
        """
        return self._session

    # -- Pure helpers ----------------------------------------------------

    def canonicalise_email(self, email: str) -> str:
        return canonicalise_email(email)

    # -- User reads / writes --------------------------------------------

    def get_user(self, *, user_id: str) -> UserIdentityRow | None:
        with tenant_agnostic():
            user = self._session.get(User, user_id)
        if user is None:
            return None
        return _to_user_identity_row(user)

    def update_user_email(self, *, user_id: str, new_email: str) -> UserIdentityRow:
        with tenant_agnostic():
            user = self._session.get(User, user_id)
            if user is None:
                # The caller already gated on get_user; reaching here
                # means the row vanished mid-UoW. Surface as a
                # programming error — the domain service maps the
                # earlier ``get_user is None`` branch to its own
                # PendingNotFound vocabulary.
                raise RuntimeError(f"update_user_email: user {user_id!r} not found")
            # Atomic swap — the ``before_update`` listener on
            # :class:`User` rewrites ``email_lower`` from ``email``
            # so we don't have to do it manually.
            user.email = new_email
            self._session.flush()
        return _to_user_identity_row(user)

    # -- Email-uniqueness + cool-off probes ------------------------------

    def email_taken_by_other(
        self, *, new_email_lower: str, current_user_id: str
    ) -> bool:
        with tenant_agnostic():
            existing = self._session.scalars(
                select(User).where(User.email_lower == new_email_lower)
            ).first()
        if existing is None:
            return False
        return existing.id != current_user_id

    def latest_passkey_created_at(self, *, user_id: str) -> datetime | None:
        with tenant_agnostic():
            stmt = (
                select(PasskeyCredential.created_at)
                .where(PasskeyCredential.user_id == user_id)
                .order_by(PasskeyCredential.created_at.desc())
                .limit(1)
            )
            latest = self._session.scalar(stmt)
        if latest is None:
            return None
        # Normalise tzinfo for the SQLite roundtrip — the column is
        # ``DateTime(timezone=True)`` but the SQLite dialect strips
        # tzinfo on the way back out. The domain compares against an
        # aware UTC ``datetime`` so we re-attach UTC here, matching
        # the same pattern used by :func:`app.auth.magic_link._check_row_expiry`.
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=UTC)
        return latest

    # -- EmailChangePending CRUD -----------------------------------------

    def insert_pending(
        self,
        *,
        pending_id: str,
        user_id: str,
        request_jti: str,
        previous_email: str,
        previous_email_lower: str,
        new_email: str,
        new_email_lower: str,
        created_at: datetime,
    ) -> EmailChangePendingRow:
        pending = EmailChangePending(
            id=pending_id,
            user_id=user_id,
            request_jti=request_jti,
            revert_jti=None,
            previous_email=previous_email,
            previous_email_lower=previous_email_lower,
            new_email=new_email,
            new_email_lower=new_email_lower,
            created_at=created_at,
            verified_at=None,
            revert_expires_at=None,
            reverted_at=None,
        )
        with tenant_agnostic():
            self._session.add(pending)
            self._session.flush()
        return _to_email_change_pending_row(pending)

    def find_pending_by_request_jti(
        self, *, request_jti: str
    ) -> EmailChangePendingRow | None:
        with tenant_agnostic():
            row = self._session.scalars(
                select(EmailChangePending).where(
                    EmailChangePending.request_jti == request_jti
                )
            ).first()
        if row is None:
            return None
        return _to_email_change_pending_row(row)

    def find_pending_by_revert_jti(
        self, *, revert_jti: str
    ) -> EmailChangePendingRow | None:
        with tenant_agnostic():
            row = self._session.scalars(
                select(EmailChangePending).where(
                    EmailChangePending.revert_jti == revert_jti
                )
            ).first()
        if row is None:
            return None
        return _to_email_change_pending_row(row)

    def mark_verified(
        self,
        *,
        pending_id: str,
        revert_jti: str,
        revert_expires_at: datetime,
        verified_at: datetime,
    ) -> EmailChangePendingRow:
        with tenant_agnostic():
            row = self._session.get(EmailChangePending, pending_id)
            if row is None:
                raise RuntimeError(f"mark_verified: pending {pending_id!r} not found")
            row.revert_jti = revert_jti
            row.revert_expires_at = revert_expires_at
            row.verified_at = verified_at
            self._session.flush()
        return _to_email_change_pending_row(row)

    def mark_reverted(
        self, *, pending_id: str, reverted_at: datetime
    ) -> EmailChangePendingRow:
        with tenant_agnostic():
            row = self._session.get(EmailChangePending, pending_id)
            if row is None:
                raise RuntimeError(f"mark_reverted: pending {pending_id!r} not found")
            row.reverted_at = reverted_at
            self._session.flush()
        return _to_email_change_pending_row(row)
