"""Instructions context — service surface (cd-oyq).

Re-exports the workspace-scoped instruction + revision CRUD service
plus its DTOs and exception types so callers (the v1 HTTP routes
landing under cd-xkfe, the agent / KB readers) reach a single
public surface — never the submodule.
"""

from __future__ import annotations

from app.services.instructions.service import (
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
