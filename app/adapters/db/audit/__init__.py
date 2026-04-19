"""Audit-context SQLAlchemy adapter: ``audit_log`` model + scope registration.

Importing this package registers ``audit_log`` as a workspace-scoped
table with :mod:`app.tenancy.registry`, so the ORM tenant filter
(:mod:`app.tenancy.orm_filter`) auto-injects
``AND workspace_id = :ctx.workspace_id`` on every SELECT / UPDATE /
DELETE against it. The registration happens as a module side-effect
so alembic's ``env.py`` loader — which walks
``app.adapters.db.<context>.models`` and imports each package —
picks the scope up without any extra wiring. Future per-context
``app/adapters/db/<context>/__init__.py`` modules follow the same
pattern for every workspace-scoped table they introduce.

See ``docs/specs/01-architecture.md`` §"Tenant filter enforcement"
and ``docs/specs/02-domain-model.md`` §"audit_log".
"""

from __future__ import annotations

from app.adapters.db.audit.models import AuditLog
from app.tenancy.registry import register

register("audit_log")

__all__ = ["AuditLog"]
