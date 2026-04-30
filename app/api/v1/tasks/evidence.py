"""Task evidence routes."""

from __future__ import annotations

from typing import Annotated

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

from app.domain.tasks.completion import (
    EvidenceContentTypeNotAllowed,
    EvidenceGpsPayloadInvalid,
    EvidenceTooLarge,
    FileEvidenceKind,
    add_file_evidence,
    add_note_evidence,
    list_evidence,
)
from app.domain.tasks.completion import TaskNotFound as CompletionTaskNotFound

from .deps import _Ctx, _Db, _MimeSniffer, _Storage
from .errors import _http, _task_not_found
from .payloads import EvidenceListResponse, EvidencePayload

router = APIRouter()


@router.get(
    "/tasks/{task_id}/evidence",
    response_model=EvidenceListResponse,
    operation_id="list_task_evidence",
    summary="List evidence rows on a task",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "evidence-list"}},
)
def list_task_evidence_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
) -> EvidenceListResponse:
    """Return every evidence row anchored to ``task_id``.

    The response envelope carries ``next_cursor`` / ``has_more`` for
    forward compatibility with cd-evidence-pagination; today the
    helper returns the full set because the expected per-task
    evidence count (template checklist + a handful of ad-hoc photos)
    is well below a single page.
    """
    try:
        views = list_evidence(session, ctx, task_id=task_id)
    except CompletionTaskNotFound as exc:
        raise _task_not_found() from exc
    return EvidenceListResponse(
        data=[EvidencePayload.from_view(v) for v in views],
        next_cursor=None,
        has_more=False,
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
    chunk_size = 64 * 1024
    total = 0
    pieces: list[bytes] = []
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_FILE_EVIDENCE_BYTES:
            await upload.close()
            raise _http(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "evidence_too_large",
                kind=kind,
                message=(
                    f"upload exceeds the {_MAX_FILE_EVIDENCE_BYTES - 1}-byte "
                    "router-level cap"
                ),
            )
        pieces.append(chunk)
    await upload.close()
    return b"".join(pieces)


@router.post(
    "/tasks/{task_id}/evidence",
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
        if file is not None:
            # A note carries no binary payload; reject the mix so a
            # confused client learns loudly.
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

    if kind not in _FILE_EVIDENCE_KINDS:
        # Anything outside the §06 "Evidence" enum is caller error —
        # 422 ``evidence_invalid_kind``. Consume any uploaded stream
        # first so the multipart parser doesn't leak a tempfile.
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

    # File-bearing kind. The upload body is required.
    if file is None:
        raise _http(
            422,
            "evidence_file_required",
            message=f"kind={kind!r} evidence requires a multipart file upload",
        )
    if note_md is not None:
        # A photo / voice / gps payload carries the body, not the
        # field. Any ``note_md`` (including whitespace-only) signals a
        # confused client; reject so the contract stays narrow and a
        # misuse never silently slips past as an empty string.
        await file.close()
        raise _http(
            422,
            "evidence_file_with_note",
            message=(
                f"kind={kind!r} evidence must not carry a 'note_md' form field; "
                "use kind='note' for notes"
            ),
        )
    declared_type = file.content_type
    if declared_type is None or declared_type == "":
        await file.close()
        raise _http(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "evidence_content_type_missing",
            kind=kind,
            message=(
                f"kind={kind!r} evidence requires a 'Content-Type' header on the "
                "uploaded file part"
            ),
        )

    payload = await _read_file_capped(file, kind=kind)
    # Narrow ``kind`` from the loose ``str`` form field to the typed
    # :data:`FileEvidenceKind` Literal the domain seam expects. The
    # earlier ``in _FILE_EVIDENCE_KINDS`` check guarantees membership;
    # the per-branch ``cast`` keeps mypy --strict honest without an
    # explicit ``cast(...)`` call.
    file_kind: FileEvidenceKind
    if kind == "photo":
        file_kind = "photo"
    elif kind == "voice":
        file_kind = "voice"
    else:
        file_kind = "gps"

    try:
        view = add_file_evidence(
            session,
            ctx,
            task_id=task_id,
            kind=file_kind,
            payload=payload,
            content_type=declared_type,
            storage=storage,
            mime_sniffer=mime_sniffer,
        )
    except CompletionTaskNotFound as exc:
        raise _task_not_found() from exc
    except EvidenceContentTypeNotAllowed as exc:
        # ``exc.content_type`` carries the **sniffed** type per spec
        # §15 ("MIME sniffed server-side; we trust the sniff, not the
        # header"). Surface both ``content_type`` (the sniff) and
        # ``sniffed_type`` (an explicit alias) so the operator
        # inspecting the audit envelope sees the actual shape of the
        # bytes — ``application/x-msdownload`` for a PE smuggled as
        # ``image/png`` — rather than the multipart-form lie.
        # ``declared_type`` is preserved alongside for the forensic
        # "client claimed X, sniff said Y" trail.
        raise _http(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "evidence_content_type_rejected",
            kind=exc.kind,
            content_type=exc.content_type,
            sniffed_type=exc.content_type,
            declared_type=declared_type,
            message=str(exc),
        ) from exc
    except EvidenceTooLarge as exc:
        raise _http(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "evidence_too_large",
            kind=exc.kind,
            size_bytes=exc.size_bytes,
            cap_bytes=exc.cap_bytes,
            message=str(exc),
        ) from exc
    except EvidenceGpsPayloadInvalid as exc:
        raise _http(
            422,
            "evidence_gps_payload_invalid",
            message=str(exc),
        ) from exc
    except ValueError as exc:
        # Remaining ValueErrors (empty payload, unknown kind that the
        # earlier branch let through somehow) collapse to 422 with a
        # generic envelope so the client still learns the rejection.
        raise _http(422, "evidence_invalid", message=str(exc)) from exc
    return EvidencePayload.from_view(view)
