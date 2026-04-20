"""IcalFeed / Reservation / StayBundle SQLAlchemy models.

v1 slice per cd-1b2 — sufficient for seeding the external-calendar →
reservation → turnover-bundle chain that drives property turnover.
Richer §02 / §04 columns (``reservation.unit_id``,
``nightly_rate_cents``, ``guest_kind``; ``ical_feed.unit_id`` /
``poll_cadence`` / ``last_error``; ``stay_bundle_state`` as an enum;
etc.) land with the domain-layer follow-ups (cd-1ai ical registration,
cd-l0k guest welcome pages) without breaking this migration's public
write contract.

Every table carries a ``workspace_id`` column and is registered as
workspace-scoped via the package's ``__init__``. FK hygiene mirrors
the rest of the app:

* Cascading parents (``property → ical_feed``, ``property →
  reservation``, ``reservation → stay_bundle``) use ``CASCADE`` so
  sweeping a parent sweeps its descendants.
* ``reservation.ical_feed_id`` uses ``SET NULL`` — a reservation
  captured from iCal outlives the feed's deletion (think: agency
  swaps provider, but the booking remains real work).

See ``docs/specs/02-domain-model.md`` §"reservation", §"ical_feed",
§"stay_bundle", and ``docs/specs/04-properties-and-stays.md``
§"Stay (reservation)" / §"iCal feed".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = ["IcalFeed", "Reservation", "StayBundle"]


# Allowed ``ical_feed.provider`` values, enforced by a CHECK
# constraint. Matches the v1 slice — ``custom`` covers generic ICS
# feeds (Google Calendar, arbitrary URLs) until the domain layer
# introduces a richer taxonomy.
_PROVIDER_VALUES: tuple[str, ...] = ("airbnb", "vrbo", "booking", "custom")

# Allowed ``reservation.status`` values — the v1 lifecycle. The
# fuller §04 machine (``tentative | confirmed | in_house |
# checked_out | cancelled``) maps onto this simpler set at the
# domain boundary and lands with cd-1ai.
_STATUS_VALUES: tuple[str, ...] = (
    "scheduled",
    "checked_in",
    "completed",
    "cancelled",
)

# Allowed ``reservation.source`` values — the v1 ingestion channels.
# The §02 ``stay_source`` enum is a superset; the simpler three-value
# set here lands the shape the current API needs.
_SOURCE_VALUES: tuple[str, ...] = ("ical", "manual", "api")

# Allowed ``stay_bundle.kind`` values, enforced by a CHECK
# constraint. Matches §04 "Stay task bundles" — three canonical
# rule types the scheduler materialises against a reservation.
_BUNDLE_KIND_VALUES: tuple[str, ...] = ("turnover", "welcome", "deep_clean")


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', …)`` CHECK body fragment.

    Mirrors the helper in sibling ``tasks`` / ``places`` modules so
    the enum CHECK constraints below stay readable.
    """
    return "'" + "', '".join(values) + "'"


class IcalFeed(Base):
    """External calendar URL the poller ingests reservations from.

    The v1 slice carries the minimum needed to identify and track a
    feed: ``url`` (the iCal endpoint), ``provider`` (canonical
    channel enum), ``last_polled_at`` / ``last_etag`` (conditional-
    GET plumbing), and ``enabled`` (operator kill switch). The
    richer §04 columns (``unit_id``, ``poll_cadence``, ``last_error``)
    land with cd-1ai. FK cascades on ``property_id`` so deleting the
    property sweeps its feeds.
    """

    __tablename__ = "ical_feed"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The operator-supplied iCal URL. §04's SSRF guard
    # (``ical_url_insecure_scheme`` / ``ical_url_private_address``)
    # runs in the domain layer, not at the DB.
    url: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    # ``NULL`` means the feed has never been polled — a fresh
    # registration. The poller treats a null value as "due now".
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Last ``ETag`` seen on a 200; the next poll sends it as
    # ``If-None-Match`` to save bandwidth.
    last_etag: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"provider IN ({_in_clause(_PROVIDER_VALUES)})",
            name="provider",
        ),
        Index("ix_ical_feed_workspace_property", "workspace_id", "property_id"),
    )


class Reservation(Base):
    """A booked stay — either ingested from iCal or entered manually.

    The v1 slice carries the minimum needed to generate turnover
    work: check-in / check-out instants (both ``DateTime(timezone=
    True)`` — resolved UTC at rest), guest identity hints, the
    lifecycle status enum, and the ``external_uid`` used to
    idempotently re-ingest the same VEVENT. The ``(ical_feed_id,
    external_uid)`` uniqueness is what makes a re-poll safe: the
    next poll upserts on the pair rather than inserting a duplicate.

    When ``ical_feed_id IS NULL`` the reservation was manual or API;
    both Postgres and SQLite treat NULLs as distinct in unique
    indexes by default, so manual entries with the same
    ``external_uid`` never collide. OK for v1 — the domain layer
    will own the richer §04 "uniqueness by (unit, source, external)"
    rule when cd-1ai lands.

    ``raw_summary`` / ``raw_description`` preserve the upstream
    VEVENT body so downstream parsers can re-analyse without
    re-polling.
    """

    __tablename__ = "reservation"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Nullable + ``SET NULL``: a reservation outlives its feed if the
    # feed is deleted (agency swap, manual recapture). The domain
    # layer's re-ingest path doesn't depend on the feed surviving.
    ical_feed_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("ical_feed.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Provider UID (Airbnb HMAC id, VRBO reservation id, etc.). Kept
    # as plain text — callers never parse it.
    external_uid: Mapped[str] = mapped_column(String, nullable=False)
    check_in: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    check_out: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    guest_name: Mapped[str | None] = mapped_column(String, nullable=True)
    guest_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="scheduled")
    source: Mapped[str] = mapped_column(String, nullable=False, default="ical")
    # Raw VEVENT body kept verbatim so the domain layer can re-parse
    # without another HTTP fetch.
    raw_summary: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_description: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_in_clause(_STATUS_VALUES)})",
            name="status",
        ),
        CheckConstraint(
            f"source IN ({_in_clause(_SOURCE_VALUES)})",
            name="source",
        ),
        CheckConstraint("check_out > check_in", name="check_out_after_check_in"),
        # Idempotent re-poll: the upsert path targets this composite.
        UniqueConstraint(
            "ical_feed_id",
            "external_uid",
            name="uq_reservation_feed_external_uid",
        ),
        # Per-acceptance: "reservations for this property in time order".
        Index("ix_reservation_property_check_in", "property_id", "check_in"),
    )


class StayBundle(Base):
    """Group of tasks materialised against a :class:`Reservation`.

    One bundle per (reservation, rule) pair — see §04 "Stay task
    bundles". The v1 slice persists the ``kind`` (``turnover`` /
    ``welcome`` / ``deep_clean``) and a JSON payload with template
    refs + metadata the scheduler uses to spawn occurrences.
    Cascades on the parent reservation so a cancelled booking
    sweeps its unstarted work.
    """

    __tablename__ = "stay_bundle"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    reservation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("reservation.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # Flat list of ``{template_id, metadata, …}`` payloads. The outer
    # ``Any`` is scoped to SQLAlchemy's JSON column type — callers
    # writing a typed payload should use a TypedDict locally and
    # coerce into this column. The domain layer validates shape at
    # write time.
    tasks_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_BUNDLE_KIND_VALUES)})",
            name="kind",
        ),
        Index("ix_stay_bundle_reservation", "reservation_id"),
    )
