"""Shared networking primitives.

Holds the SSRF-guarded HTTP fetch helpers — :mod:`app.net.fetch_guard`.
Callers import directly from the submodule (no re-exports here) so
``mypy --strict`` keeps pinning the originating symbol on every
``from app.net.fetch_guard import …``. See
``docs/specs/15-security-privacy.md`` §"SSRF" for the threat model and
per-feature inheritance rules.
"""

from __future__ import annotations
