"""Task evidence routes."""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)

from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.api.uploads import read_upload_capped, require_upload_content_type
from app.domain.tasks.completion import (
    ChecklistItemNotFound,
    EvidenceContentTypeNotAllowed,
    EvidenceGpsPayloadInvalid,
    EvidenceTooLarge,
    FileEvidenceKind,
    TaskTerminal,
    add_file_evidence,
    add_note_evidence,
    attach_checklist_evidence,
    list_evidence,
)
from app.domain.tasks.completion import TaskNotFound as CompletionTaskNotFound

from .deps import _Ctx, _Db, _MimeSniffer, _Storage
from .errors import _http, _task_not_found
from .payloads import EvidenceListResponse, EvidencePayload, TaskChecklistItemPayload

router = APIRouter()


@router.get(
    "/{task_id}/evidence",
    response_model=EvidenceListResponse,
    operation_id="list_task_evidence",
    summary="List evidence rows on a task",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "evidence-list"}},
)
def list_task_evidence_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> EvidenceListResponse:
    """Return a cursor-paginated page of evidence rows anchored to ``task_id``."""
    try:
        after_id = decode_cursor(cursor)
        views = list_evidence(
            session, ctx, task_id=task_id, after_id=after_id, limit=limit + 1
        )
    except CompletionTaskNotFound as exc:
        raise _task_not_found() from exc
    page = paginate(views, limit=limit, key_getter=lambda v: v.id)
    return EvidenceListResponse(
        data=[EvidencePayload.from_view(v) for v in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


_FILE_EVIDENCE_KINDS: frozenset[str] = frozenset({"photo", "voice", "gps"})

# Hard ceiling on the file part the multipart parser will ever buffer
# in memory before this route's domain seam runs the per-kind cap.
# Pinned at the largest per-kind cap (voice — 25 MiB per spec §15
# "Input validation") + 1 byte so a 25 MiB voice memo lands but a
# pathological 1 GiB upload short-circuits before we hash it. The
# domain seam re-enforces the per-kind cap so this is defence in depth,
# not the only gate.
_MAX_FILE_EVIDENCE_BYTES: int = 25 * 1024 * 1024 + 1


def _check_evidence_content_length(request: Request) -> None:
    """Raise 413 when the client advertises an oversized body.

    Mirrors :func:`app.api.v1.auth.me_avatar._check_content_length`.
    Exposed as a FastAPI dep (not an inline call) so it runs **before**
    Starlette's multipart body parser — otherwise FastAPI would buffer
    the entire upload to a :class:`SpooledTemporaryFile` to populate
    the :class:`UploadFile` parameter before the handler body could
    look at the header. Dependencies are resolved ahead of body
    params, so this dep is the first gate the router opens.

    Content-Length can be absent (chunked transfer) or lie; the
    streaming guard in :func:`_read_file_capped` is the authoritative
    check. This fast-path saves the buffering cost when the client
    admits to an oversized upload — the common well-behaved rejection
    shape.
    """
    cl = request.headers.get("content-length")
    if cl is None:
        return
    try:
        size = int(cl)
    except ValueError:
        # Malformed Content-Length — let Starlette's normal parsing
        # surface the underlying error rather than translating it
        # here. A non-numeric header isn't specifically a "too large"
        # condition.
        return
    if size > _MAX_FILE_EVIDENCE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "error": "evidence_too_large",
                "message": (
                    f"upload exceeds the {_MAX_FILE_EVIDENCE_BYTES - 1}-byte "
                    "router-level cap"
                ),
            },
        )


_EvidenceContentLengthGuard = Annotated[None, Depends(_check_evidence_content_length)]


async def _read_file_capped(upload: UploadFile, *, kind: str) -> bytes:
    """Buffer the upload body, raising 413 past :data:`_MAX_FILE_EVIDENCE_BYTES`.

    Mirrors :func:`app.api.v1.auth.me_avatar._read_capped` — streams in
    64 KiB chunks so a client that lies about ``Content-Length`` can't
    exhaust memory. The per-kind cap re-checks inside the domain seam
    so a misconfigured router still can't admit a 30 MiB GPS payload.

    This is the second of the two router-level gates: the
    :func:`_check_evidence_content_length` dep rejects an oversized
    advertised body **before** the multipart parser runs; this
    function bounds an unadvertised / lying body during the read.
    """
    return await read_upload_capped(
        upload,
        max_bytes=_MAX_FILE_EVIDENCE_BYTES,
        too_large=lambda: _http(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "evidence_too_large",
            kind=kind,
            message=(
                f"upload exceeds the {_MAX_FILE_EVIDENCE_BYTES - 1}-byte "
                "router-level cap"
            ),
        ),
    )


async def _add_note_payload(
    session: _Db,
    ctx: _Ctx,
    *,
    task_id: str,
    note_md: str | None,
    file: UploadFile | None,
) -> EvidencePayload:
    if file is not None:
        await file.close()
        raise _http(
            422,
            "evidence_note_with_file",
            message="kind='note' evidence must not carry a file upload",
        )
    if note_md is None or not note_md.strip():
        raise _http(
            422,
            "evidence_note_empty",
            message="kind='note' evidence requires a non-empty note_md",
        )
    try:
        view = add_note_evidence(session, ctx, task_id=task_id, note_md=note_md)
    except CompletionTaskNotFound as exc:
        raise _task_not_found() from exc
    except ValueError as exc:
        raise _http(422, "evidence_note_empty", message=str(exc)) from exc
    return EvidencePayload.from_view(view)


async def _require_file_upload(
    *,
    kind: str,
    note_md: str | None,
    file: UploadFile | None,
) -> UploadFile:
    if kind not in _FILE_EVIDENCE_KINDS:
        if file is not None:
            await file.close()
        raise _http(
            422,
            "evidence_invalid_kind",
            message=(
                f"kind={kind!r} is not a valid evidence kind; expected "
                "one of 'note', 'photo', 'voice', 'gps'"
            ),
        )
    if file is None:
        raise _http(
            422,
            "evidence_file_required",
            message=f"kind={kind!r} evidence requires a multipart file upload",
        )
    if note_md is not None:
        await file.close()
        raise _http(
            422,
            "evidence_file_with_note",
            message=(
                f"kind={kind!r} evidence must not carry a 'note_md' form field; "
                "use kind='note' for notes"
            ),
        )
    return file


async def _require_declared_type(
    file: UploadFile,
    *,
    kind: str,
    missing_message: str,
) -> str:
    try:
        return require_upload_content_type(
            file,
            missing=lambda: _http(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "evidence_content_type_missing",
                kind=kind,
                message=missing_message,
            ),
        )
    except HTTPException:
        await file.close()
        raise


def _file_kind(kind: str) -> FileEvidenceKind:
    if kind == "photo":
        return "photo"
    if kind == "voice":
        return "voice"
    return "gps"


def _raise_file_evidence_error(exc: Exception, *, declared_type: str) -> NoReturn:
    if isinstance(exc, CompletionTaskNotFound):
        # code-health: ignore[duplicate] Repeated wire shape is intentional.
        raise _task_not_found() from exc
    # code-health: ignore[duplicate] Repeated wire shape is intentional.
    if isinstance(exc, EvidenceContentTypeNotAllowed):
        raise _http(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "evidence_content_type_rejected",
            kind=exc.kind,
            content_type=exc.content_type,
            sniffed_type=exc.content_type,
            declared_type=declared_type,
            message=str(exc),
        ) from exc
    if isinstance(exc, EvidenceTooLarge):
        raise _http(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "evidence_too_large",
            kind=exc.kind,
            size_bytes=exc.size_bytes,
            cap_bytes=exc.cap_bytes,
            message=str(exc),
        ) from exc
    if isinstance(exc, EvidenceGpsPayloadInvalid):
        raise _http(422, "evidence_gps_payload_invalid", message=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise _http(422, "evidence_invalid", message=str(exc)) from exc
    raise exc


def _raise_checklist_evidence_error(exc: Exception, *, declared_type: str) -> NoReturn:
    if isinstance(exc, CompletionTaskNotFound | ChecklistItemNotFound):
        raise _task_not_found() from exc
    if isinstance(exc, TaskTerminal):
        raise _http(
            status.HTTP_409_CONFLICT,
            "task_terminal",
            state=exc.state,
            message=str(exc),
        ) from exc
    if isinstance(exc, EvidenceContentTypeNotAllowed):
        raise _http(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "evidence_content_type_rejected",
            kind=exc.kind,
            content_type=exc.content_type,
            sniffed_type=exc.content_type,
            declared_type=declared_type,
            message=str(exc),
        ) from exc
    if isinstance(exc, EvidenceTooLarge):
        raise _http(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "evidence_too_large",
            kind=exc.kind,
            size_bytes=exc.size_bytes,
            cap_bytes=exc.cap_bytes,
            message=str(exc),
        ) from exc
    if isinstance(exc, ValueError):
        raise _http(422, "evidence_invalid", message=str(exc)) from exc
    raise exc


@router.post(
    "/{task_id}/evidence",
    status_code=status.HTTP_201_CREATED,
    response_model=EvidencePayload,
    operation_id="upload_task_evidence",
    summary="Attach evidence to a task",
)
async def upload_task_evidence_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
    storage: _Storage,
    mime_sniffer: _MimeSniffer,
    _: _EvidenceContentLengthGuard,
    kind: Annotated[str, Form(max_length=16)],
    note_md: Annotated[str | None, Form(max_length=20_000)] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> EvidencePayload:
    # code-health: ignore[params] Preserves multipart OpenAPI fields.
    """Accept ``multipart/form-data``; wire every §06 evidence kind end-to-end.

    Routing by ``kind``:

    * ``note`` — :func:`~app.domain.tasks.completion.add_note_evidence`;
      the ``note_md`` form field is required and the upload body MUST
      be empty. Bridge until the ``completion_note_md`` task column
      lands.
    * ``photo`` / ``voice`` — :func:`~app.domain.tasks.completion.
      add_file_evidence`; the upload body is hashed (SHA-256), handed
      to the content-addressed :class:`Storage` port, and an
      :class:`Evidence` row points at the resulting blob. Per spec
      §15 "Input validation": the body is sniffed server-side via
      the injectable :class:`MimeSniffer` and the **sniffed** type
      is validated against the per-kind allow-list (the multipart
      header is informational only). Size cap per kind.
    * ``gps`` — :func:`~app.domain.tasks.completion.add_file_evidence`
      with the multipart-declared ``Content-Type`` (which the client
      MUST set to ``application/json`` per spec §06 "Evidence" — the
      §15 sniffer's JSON structural fallback is gated on a JSON-shaped
      hint, so a non-JSON declared type closes the gate and earns
      415). The upload body MUST be a small JSON document carrying
      ``lat`` / ``lon`` / optional ``accuracy_m``. Routes through
      Storage so every evidence row shares the same content-addressed
      pipeline.
    """
    if kind == "note":
        return await _add_note_payload(
            session,
            ctx,
            task_id=task_id,
            note_md=note_md,
            file=file,
        )

    file = await _require_file_upload(kind=kind, note_md=note_md, file=file)
    declared_type = await _require_declared_type(
        file,
        kind=kind,
        missing_message=(
            f"kind={kind!r} evidence requires a 'Content-Type' header on the "
            "uploaded file part"
        ),
    )

    payload = await _read_file_capped(file, kind=kind)

    try:
        view = add_file_evidence(
            session,
            ctx,
            task_id=task_id,
            kind=_file_kind(kind),
            payload=payload,
            content_type=declared_type,
            storage=storage,
            mime_sniffer=mime_sniffer,
        )
    except Exception as exc:
        _raise_file_evidence_error(exc, declared_type=declared_type)
    return EvidencePayload.from_view(view)


@router.patch(
    "/{task_id}/checklist/{item_id}/evidence",
    response_model=TaskChecklistItemPayload,
    operation_id="attach_task_checklist_evidence",
    summary="Attach a photo blob to a checklist item",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "checklist-evidence"}},
)
async def attach_task_checklist_evidence_route(
    task_id: str,
    item_id: str,
    ctx: _Ctx,
    session: _Db,
    storage: _Storage,
    mime_sniffer: _MimeSniffer,
    _: _EvidenceContentLengthGuard,
    file: Annotated[UploadFile, File()],
) -> TaskChecklistItemPayload:
    # code-health: ignore[params] Preserves multipart OpenAPI fields.
    """Stamp a checklist item's :attr:`evidence_blob_hash` from a multipart upload.

    Mirrors :func:`upload_task_evidence_route`'s photo branch
    end-to-end: the same router-level content-length dep, the same
    streaming-cap reader, the same per-kind MIME allow-list / size
    cap / sniff inside the domain seam, and the same Storage port.
    The kind is pinned to ``photo`` because checklist items only
    carry photographic evidence (see ``ChecklistItem.requires_photo``)
    — voice / gps belong on the ad-hoc ``POST /tasks/{id}/evidence``
    surface.

    The endpoint sets :attr:`ChecklistItem.evidence_blob_hash` and
    audits ``task.checklist.evidence.add``; it does NOT write a
    sibling :class:`Evidence` row because the checklist column is the
    per-item pointer and the evidence list is the ad-hoc trail (§02 /
    §06 keep them cleanly separated).
    """
    declared_type = await _require_declared_type(
        file,
        kind="photo",
        missing_message=(
            "checklist evidence requires a 'Content-Type' header on the "
            "uploaded file part"
        ),
    )

    payload = await _read_file_capped(file, kind="photo")

    try:
        view = attach_checklist_evidence(
            session,
            ctx,
            task_id=task_id,
            item_id=item_id,
            payload=payload,
            content_type=declared_type,
            storage=storage,
            mime_sniffer=mime_sniffer,
        )
    except Exception as exc:
        _raise_checklist_evidence_error(exc, declared_type=declared_type)
    return TaskChecklistItemPayload.from_evidence_view(view)
