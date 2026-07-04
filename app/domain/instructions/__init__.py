"""Instructions context — KB entries, versioning, scope resolution.

Re-exports the workspace-scoped instruction + revision CRUD service
plus its DTOs and exception types so callers (the v1 HTTP routes, the
agent / KB readers) reach a single public surface — never the
submodule.

See docs/specs/07-instructions-kb.md.
"""

from __future__ import annotations

from app.domain.instructions.service import (
    ArchivedInstructionError,
    CurrentRevisionRestoreRejected,
    InstructionNotFound,
    InstructionPermissionDenied,
    InstructionResult,
    InstructionRevisionPage,
    InstructionScope,
    InstructionVersionView,
    InstructionView,
    ScopeValidationError,
    TagValidationError,
    archive,
    create,
    list_revisions,
    restore_to_revision,
    update_body,
    update_metadata,
)

__all__ = [
    "ArchivedInstructionError",
    "CurrentRevisionRestoreRejected",
    "InstructionNotFound",
    "InstructionPermissionDenied",
    "InstructionResult",
    "InstructionRevisionPage",
    "InstructionScope",
    "InstructionVersionView",
    "InstructionView",
    "ScopeValidationError",
    "TagValidationError",
    "archive",
    "create",
    "list_revisions",
    "restore_to_revision",
    "update_body",
    "update_metadata",
]
