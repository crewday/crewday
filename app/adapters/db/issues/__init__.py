"""issues - workspace-scoped issue reports."""

from __future__ import annotations

from app.adapters.db.issues.models import IssueReport
from app.tenancy.registry import register

register("issue_report")

__all__ = ["IssueReport"]
