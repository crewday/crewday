"""IssueReport SQLAlchemy model.

Persistence slice for worker / manager issue reports from spec 10
"Issue reports". API and domain behavior land separately; this module
only defines the workspace-scoped table shape and database-level enum
guards needed by that layer.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets - see :mod:`app.adapters.db` package
# docstring for the load-order contract.
from app.adapters.db.identity import models as _identity_models  # noqa: F401
from app.adapters.db.places import models as _places_models  # noqa: F401
from app.adapters.db.tasks import models as _tasks_models  # noqa: F401
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = ["IssueReport"]


_SEVERITY_VALUES: tuple[str, ...] = ("low", "normal", "high", "urgent")
_CATEGORY_VALUES: tuple[str, ...] = (
    "damage",
    "broken",
    "supplies",
    "safety",
    "other",
)
_STATE_VALUES: tuple[str, ...] = (
    "open",
    "in_progress",
    "resolved",
    "wont_fix",
)


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', ...)`` CHECK body fragment."""
    return "'" + "', '".join(values) + "'"


class IssueReport(Base):
    """Workspace-scoped issue report raised from a property, area, or task."""

    __tablename__ = "issue_report"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    reported_by_user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="RESTRICT"),
        nullable=False,
    )
    area_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("area.id", ondelete="SET NULL"),
        nullable=True,
    )
    area_label: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("occurrence.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    description_md: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="normal",
        server_default="normal",
    )
    category: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="open",
        server_default="open",
    )
    attachment_file_ids_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    converted_to_task_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("occurrence.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolution_note: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    resolved_by: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            f"severity IN ({_in_clause(_SEVERITY_VALUES)})",
            name="severity",
        ),
        CheckConstraint(
            f"category IN ({_in_clause(_CATEGORY_VALUES)})",
            name="category",
        ),
        CheckConstraint(
            f"state IN ({_in_clause(_STATE_VALUES)})",
            name="state",
        ),
        Index("ix_issue_report_workspace", "workspace_id"),
        Index("ix_issue_report_workspace_property", "workspace_id", "property_id"),
        Index(
            "ix_issue_report_workspace_state_created",
            "workspace_id",
            "state",
            "created_at",
        ),
        Index("ix_issue_report_workspace_created", "workspace_id", "created_at"),
    )
