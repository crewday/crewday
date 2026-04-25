"""Worker task: receipt OCR / autofill (cd-95zb).

Thin wrapper around :func:`app.domain.expenses.autofill.run_extraction`
that the :func:`app.domain.expenses.claims.attach_receipt` hook can
call after inserting an attachment row.

**v1 sync semantic.** The "worker" runs the extraction synchronously
in the same transaction as the attach — there is no background queue
scaffolding in this tree yet. A future Beads task lands the queue
runtime + queue-shape contract, at which point :func:`run_receipt_ocr`
becomes the entry point the queue consumer dispatches against. The
sync v1 keeps the seam in place so the swap is a one-line wiring
change at the call site (move the call from inside ``attach_receipt``
to the queue consumer; pass the runner ``None`` so the attach path
stops calling it inline).

The wrapper deliberately offers the same signature shape as a queue
consumer so the eventual swap is mechanical: ``(session, ctx,
claim_id, attachment_id)`` matches the
``(payload['claim_id'], payload['attachment_id'])`` shape a queue
job's body would carry, with the session + ctx threaded by the
worker harness. Adapter dependencies (LLM client, Storage) are
DI-only — neither lives on the call site so a future queue consumer
can plug a different LLM client (e.g. a sticky-priority retry rung)
without touching the domain seam.

See ``docs/specs/09-time-payroll-expenses.md`` §"Submission flow
(worker)", ``docs/specs/01-architecture.md`` §"Worker".
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.adapters.llm.ports import LLMClient
from app.adapters.storage.ports import Storage
from app.config import Settings
from app.domain.expenses.autofill import ExtractionResult, run_extraction
from app.tenancy import WorkspaceContext
from app.util.clock import Clock

__all__ = ["run_receipt_ocr"]


def run_receipt_ocr(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    attachment_id: str,
    llm: LLMClient,
    storage: Storage,
    clock: Clock | None = None,
    settings: Settings | None = None,
) -> ExtractionResult:
    """Synchronous entry point for the receipt-OCR / autofill task.

    Kept as a one-liner over :func:`run_extraction` so the queue
    consumer (cd-worker-queue follow-up) can swap call sites without
    chasing a behaviour change. The single source of truth for the
    extraction contract — error taxonomy, autofill rule, audit
    shape, LLM-usage row — lives in
    :mod:`app.domain.expenses.autofill`.
    """
    return run_extraction(
        session,
        ctx,
        claim_id=claim_id,
        attachment_id=attachment_id,
        llm=llm,
        storage=storage,
        clock=clock,
        settings=settings,
    )
