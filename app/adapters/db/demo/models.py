"""Demo-mode SQLAlchemy models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = ["DemoWorkspace"]


class DemoWorkspace(Base):
    """Ephemeral demo workspace marker (§24)."""

    __tablename__ = "demo_workspace"

    id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    scenario_key: Mapped[str] = mapped_column(String, nullable=False)
    seed_digest: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cookie_binding_digest: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("ix_demo_workspace_expires_at", "expires_at"),)
