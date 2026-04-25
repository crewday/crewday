"""Default :class:`~app.adapters.storage.ports.VirusScanner` implementations.

Spec §15 "Input validation" requires uploads to be scanned before they
land in the blob store. The deployment-time wiring (ClamAV daemon,
vendor REST API, …) is not yet in this repo; the
:class:`NullVirusScanner` here is the "no scanner configured" stub so
the upload pipeline can boot without one and still document the gap.

Real scanners plug in behind the
:class:`~app.adapters.storage.ports.VirusScanner` Protocol; the app
factory selects the implementation based on
``settings.virus_scanner_backend`` (a follow-up Beads task tracks the
real wiring).

See ``docs/specs/15-security-privacy.md`` §"Input validation".
"""

from __future__ import annotations

import logging
from threading import Lock

from app.adapters.storage.ports import VirusScanResult

__all__ = ["NullVirusScanner"]


_log = logging.getLogger(__name__)


class NullVirusScanner:
    """Default :class:`~app.adapters.storage.ports.VirusScanner` — never blocks.

    Returns :class:`VirusScanResult` with ``status='unknown'`` for every
    payload and emits a single ``WARNING`` per process so operators
    notice the deployment shipped without virus scanning. The caller
    treats ``unknown`` as "allow" (per spec §15: "no scanner configured
    = allow with logged warning").

    Thread-safe: the one-shot warning is gated by a lock so two
    concurrent uploads don't both emit it. Subsequent uploads stay
    silent so the log stream isn't flooded.
    """

    __slots__ = ("_warned", "_warned_lock")

    def __init__(self) -> None:
        self._warned = False
        self._warned_lock = Lock()

    def scan(self, payload: bytes, *, content_type: str | None) -> VirusScanResult:
        """Return ``unknown`` and warn once per process."""
        # Reading + writing ``_warned`` under a lock keeps the warning
        # at exactly one emission even when two threads race the first
        # upload through the scanner. The unused-arg pattern mirrors
        # the rest of the storage seam — a real scanner reads payload.
        _ = payload, content_type
        with self._warned_lock:
            if not self._warned:
                _log.warning(
                    "virus scanner not configured; "
                    "uploads land without antivirus inspection. "
                    "Wire a real scanner per docs/specs/15-security-privacy.md "
                    "§'Input validation'.",
                )
                self._warned = True
        return VirusScanResult(status="unknown")
