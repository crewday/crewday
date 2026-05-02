"""Hand-written ``crewday tasks complete`` override.

Spec ``docs/specs/13-cli.md`` §"crewday tasks": ``complete <id>
[--photo <path>] [--note "..."]`` is one user-facing verb but two
HTTP calls: each ``--evidence`` (or ``--photo``) path uploads a file
via multipart ``POST /tasks/{id}/evidence`` and the resulting
evidence row's ``id`` is collected; once every upload succeeds, the
final ``POST /tasks/{id}/complete`` carries the IDs in
``photo_evidence_ids`` so the server's evidence-policy gate is
satisfied (§06 "Evidence policy").

Atomicity-ish: if any upload fails, the loop stops and the task
stays open — partial uploads (the evidence rows that did land) are
not rolled back, but the user-visible state of the task is unchanged
(no ``/complete`` call was made). The user re-runs after fixing the
broken upload and the server's idempotency surface ensures duplicate
evidence is acceptable; complete itself is not idempotent across
distinct invocations, so we make sure the call only happens once we
have every piece.
"""

from __future__ import annotations

import json
import mimetypes
import pathlib
from typing import Final

import click

from crewday._client import CrewdayClient
from crewday._globals import CrewdayContext
from crewday._main import ConfigError, CrewdayError
from crewday._overrides import cli_override
from crewday._runtime import _default_client_factory, _resolve_profile_context

__all__ = ["register"]


# The ``photo`` evidence kind is the only one we send for
# ``--evidence`` paths today — that is the verb the spec command
# tree exposes (``complete <id> [--photo <path>]``). Voice / GPS
# evidence have specialised payload shapes (audio file, JSON
# coordinate doc) and live behind future override flags rather than
# squatting on ``--evidence``.
_DEFAULT_EVIDENCE_KIND: Final[str] = "photo"


def _guess_content_type(path: pathlib.Path) -> str:
    """Return a best-guess MIME for the upload.

    The server sniffs the actual bytes server-side per spec §15
    "Input validation" so the multipart-declared MIME is only a hint;
    sending the right one anyway keeps the multipart envelope honest
    and avoids a spurious 415 on platforms whose sniffer is stricter.
    Falls back to ``application/octet-stream`` for unknown extensions.
    """
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _upload_one_evidence(
    client: CrewdayClient,
    *,
    ctx: CrewdayContext,
    task_route_root: str,
    task_id: str,
    file_path: pathlib.Path,
) -> str:
    """Upload one file to ``POST /tasks/{id}/evidence``; return the row id.

    Reads the entire file into memory — task evidence is capped at
    25 MiB by the route per spec §15 "Input validation", which is
    well under any sane CLI ceiling. Streaming from disk would buy us
    nothing on a one-shot client and would complicate the retry loop.

    Mints a fresh ``Idempotency-Key`` per call (§12 "Idempotency") so
    a transient transport error during the upload is safely retried by
    the client's retry loop — without a key, ``_should_retry`` declines
    POST retries (a blind retry could double-create the row), forcing
    the operator to re-run the whole composite and risk duplicates.
    """
    if not file_path.is_file():
        raise ConfigError(f"--evidence path is not a file: {file_path}")
    contents = file_path.read_bytes()
    response = client.request(
        "POST",
        f"{task_route_root}/{task_id}/evidence",
        data={"kind": _DEFAULT_EVIDENCE_KIND},
        files={
            "file": (
                file_path.name,
                contents,
                _guess_content_type(file_path),
            ),
        },
        idempotency_key=ctx.idempotency_key_factory(),
    )
    payload = response.json()
    if not isinstance(payload, dict):
        # Server contract violation; surface as a clean error rather
        # than tracebacking on a missing key.
        raise CrewdayError(
            f"unexpected /evidence response on {file_path.name}: "
            f"expected JSON object, got {type(payload).__name__}"
        )
    evidence_id = payload.get("id")
    if not isinstance(evidence_id, str) or not evidence_id:
        raise CrewdayError(
            f"server did not return an evidence id for {file_path.name}: {payload!r}"
        )
    return evidence_id


@click.command(name="complete")
@click.argument("task_id", metavar="TASK_ID")
@click.option(
    "--note",
    "note",
    default=None,
    help="Markdown note attached to the completion (sent as note_md).",
)
@click.option(
    "--evidence",
    "evidence_paths",
    multiple=True,
    type=click.Path(
        exists=True,
        dir_okay=False,
        readable=True,
        path_type=pathlib.Path,
    ),
    help=(
        "Path to a photo evidence file. Repeat for multiple uploads "
        "(each lands as one Evidence row of kind='photo')."
    ),
)
@click.pass_obj
def complete(
    ctx: CrewdayContext,
    *,
    task_id: str,
    note: str | None,
    evidence_paths: tuple[pathlib.Path, ...],
) -> None:
    """Mark a task done — uploading evidence first if any was supplied.

    Composite flow:

    1. ``GET /tasks/{id}`` — confirm the task exists and that the
       caller can see it. Surfaces a clean 404 before any side effect.
    2. For each ``--evidence`` path: ``POST /tasks/{id}/evidence``
       (multipart). Each row's ``id`` is collected into
       ``photo_evidence_ids``. A failure stops the loop and prevents
       the ``/complete`` call below.
    3. ``POST /tasks/{id}/complete`` with ``note_md`` and the
       collected ``photo_evidence_ids``.

    Output is one summary line — the task id, new state, and evidence
    count — followed by the JSON body so an agent that pipes through
    ``jq`` can still extract every field.
    """
    resolved_ctx = _resolve_profile_context(ctx, client_factory=_client_factory_for)
    if resolved_ctx.workspace is None or not resolved_ctx.workspace:
        raise ConfigError(
            "this command targets /w/<slug>/... but no workspace is set "
            "(pass --workspace or set CREWDAY_WORKSPACE)."
        )

    task_route_root = f"/w/{resolved_ctx.workspace}/api/v1/tasks"
    with _client_factory_for(resolved_ctx) as client:
        # Sanity-check the task exists + caller can see it. A 404 here
        # raises ApiError with exit code 1 (CLIENT_ERROR) so the
        # message stays focused on the missing-task case rather than
        # confusing the user with a partial-upload state.
        client.request("GET", f"{task_route_root}/{task_id}")

        evidence_ids: list[str] = []
        for path in evidence_paths:
            evidence_id = _upload_one_evidence(
                client,
                ctx=resolved_ctx,
                task_route_root=task_route_root,
                task_id=task_id,
                file_path=path,
            )
            evidence_ids.append(evidence_id)

        body: dict[str, object] = {"photo_evidence_ids": evidence_ids}
        if note is not None:
            body["note_md"] = note

        response = client.request(
            "POST",
            f"{task_route_root}/{task_id}/complete",
            json=body,
            idempotency_key=resolved_ctx.idempotency_key_factory(),
        )
        payload = response.json()

    state_label = payload.get("state", "?") if isinstance(payload, dict) else "?"
    click.echo(
        f"Task {task_id} → state={state_label} (evidence uploaded: {len(evidence_ids)})"
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=False, default=str))


_client_factory_for = _default_client_factory
"""Configured client factory for the active context.

Tests patch this symbol with a mock-transport factory. Keeping it as a
module-level variable lets the override share the generated runtime's
profile/default-workspace handling without giving up that unit-test seam.
"""


# Stamp the override metadata. ``upload_task_evidence`` and
# ``complete_task`` are the two operationIds covered by this composite.
complete = cli_override(
    "tasks",
    "complete",
    covers=["upload_task_evidence", "complete_task"],
)(complete)


def register(root: click.Group) -> None:
    """Attach ``tasks complete`` under the existing ``tasks`` group.

    Codegen registers ``tasks`` with verbs like ``list`` / ``show`` /
    ``patch``. Our override shadows the generated ``complete`` (which
    can't model the multipart upload + state-transition pair) so the
    resulting ``crewday tasks complete`` always routes through this
    composite.
    """
    tasks_group = root.commands.get("tasks")
    if tasks_group is None:
        tasks_group = click.Group(name="tasks", help="tasks commands")
        root.add_command(tasks_group)
    if not isinstance(tasks_group, click.Group):
        raise RuntimeError(
            "expected 'tasks' to be a click.Group; cannot attach 'complete' "
            "override to a leaf command."
        )
    # Last writer wins per Click; this overwrites any generated
    # ``complete`` command silently. The parity gate counts on
    # ``covers=[...]`` to know that's intentional, not an accident.
    tasks_group.add_command(complete)
