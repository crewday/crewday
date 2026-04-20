"""S3-backed :class:`~app.adapters.storage.ports.Storage` — placeholder.

Deferred to the deploy-phase task. Until that task lands, the only
:class:`~app.adapters.storage.ports.Storage` implementation is
:class:`app.adapters.storage.localfs.LocalFsStorage`; operators
selecting ``storage_backend = "s3"`` in :class:`app.config.Settings`
will be rejected at service construction time by the caller that
wires the adapter.

See ``docs/specs/01-architecture.md`` §"Adapters/storage" and
``docs/specs/19-roadmap.md`` for the schedule.
"""

from __future__ import annotations

__all__: list[str] = []
