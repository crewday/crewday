"""Default :class:`~app.adapters.storage.ports.MimeSniffer` implementation.

Spec §15 "Input validation": "MIME sniffed server-side; we trust the
sniff, not the header." This module wraps the pure-Python
:mod:`filetype` library so the upload pipeline can validate against
the bytes themselves, not the multipart-declared ``Content-Type``.

``filetype`` covers every magic-byte format we accept on the task
evidence surface (PNG / JPEG / WEBP / HEIC / WAV / MP3 / WebM / OGG /
MP4 / AAC) plus the two executable formats whose magic bytes the
library knows: Windows PE (``MZ`` → ``application/x-msdownload``)
and ELF (``\\x7fELF`` → ``application/x-executable``). It does **not**
recognise Mach-O (``\\xfe\\xed\\xfa\\xce`` / ``\\xfe\\xed\\xfa\\xcf``),
Java class files (``\\xca\\xfe\\xba\\xbe``), shell shebangs, or any
text-shaped script (HTML / SVG / JS) — those return ``None``. The
seam contract is "reject what we can't classify" rather than "reject
the malware shapes we know about", so an unrecognised payload still
never lands in the blob store. Picked over ``python-magic`` to avoid
pulling a libmagic system dependency — matches the project's "no
system dep where avoidable" posture.

A second consequence of magic-byte-only detection: ``filetype``
always returns ``video/webm`` for any WebM container (even one
carrying an audio-only Opus stream — Chrome / Firefox MediaRecorder's
default voice format) and ``video/mp4`` for a generic ``ftyp/isom``
MP4. The voice allow-list at
:data:`app.domain.tasks.completion._VOICE_ALLOWED_MIME` carries
both the audio-named MIME and the container-named MIME for this
reason — the sniffer can't introspect the codec so we widen the
allow-list, not the sniffer.

JSON is text and not magic-byte detectable; the sniffer falls back to
a small structural check (``json.loads`` succeeds + at least one of
``lat`` / ``lon`` is present) so the GPS evidence kind has a sniff
verdict to validate against. This is the **only** text-shaped format
the seam recognises — every other text payload returns ``None`` and
is rejected by the caller.

See ``docs/specs/15-security-privacy.md`` §"Input validation".
"""

from __future__ import annotations

import json

import filetype

__all__ = ["FiletypeMimeSniffer"]


class FiletypeMimeSniffer:
    """Default :class:`~app.adapters.storage.ports.MimeSniffer` —
    magic-byte sniff first, JSON structural check as a fallback.

    The sniff algorithm:

    1. Hand the first ~262 bytes to :func:`filetype.guess`. A non-
       ``None`` match returns its IANA MIME type (e.g. PNG bytes
       sniff to ``image/png`` regardless of what the multipart
       header claimed).
    2. When the magic-byte sniff is empty, attempt to parse the
       payload as JSON. A successful parse whose top-level value is
       an object **and** carries at least one of the GPS keys
       (``lat`` / ``lon``) returns ``application/json``. Anything
       else returns ``None``.

    The JSON branch is deliberately narrow. We don't want the sniffer
    to vouch for arbitrary JSON — only the GPS evidence kind expects
    a JSON payload, and that kind already runs a stricter structural
    validator downstream. The ``lat`` / ``lon`` smell-test prevents
    an attacker from smuggling a generic JSON document under the
    declared header ``application/json`` and having the sniffer
    bless it.

    The class carries no state; instances are interchangeable. The
    Protocol port is satisfied structurally — no inheritance needed.
    """

    __slots__ = ()

    def sniff(self, payload: bytes, *, hint: str | None = None) -> str | None:
        """Return the IANA MIME type of ``payload``, or ``None`` if undetectable.

        ``hint`` is currently consulted only to gate the JSON
        structural fallback: we attempt the JSON parse only when the
        hint advertises a JSON-shaped media type, so a binary upload
        misdeclared as ``image/png`` whose bytes happen to start with
        ``{"lat":...}`` doesn't sniff to ``application/json`` (and
        thereby slip past the photo allow-list). The magic-byte path
        ignores ``hint`` entirely — a PE executable sniffs to
        ``application/x-msdownload`` regardless of any declared type.
        """
        # Empty payload — nothing to sniff. The caller already rejects
        # zero-byte uploads upstream; returning ``None`` keeps the
        # contract symmetric (no bytes → no verdict).
        if not payload:
            return None

        # Magic-byte sniff. ``filetype.guess`` reads the first ~262
        # bytes and returns a ``filetype.types.Type`` instance whose
        # ``.mime`` attribute is the IANA media type, or ``None``
        # when no signature matches.
        match = filetype.guess(payload)
        if match is not None:
            mime = match.mime
            if isinstance(mime, str) and mime:
                return mime

        # JSON structural fallback — only when the caller hinted JSON.
        # The narrow gate keeps the sniffer from vouching for an
        # ``image/png``-declared payload that happens to be JSON-shaped.
        if hint is not None and hint.lower().startswith("application/json"):
            return _sniff_json(payload)

        return None


def _sniff_json(payload: bytes) -> str | None:
    """Return ``application/json`` when ``payload`` is a GPS-shaped JSON object.

    Narrow on purpose: the sniffer is a verdict for the rest of the
    pipeline, not a parser. We accept only the shape the GPS evidence
    kind produces (an object carrying ``lat`` and / or ``lon``); every
    other JSON payload returns ``None`` so the caller rejects it. The
    full coordinate validation (range checks, ``accuracy_m`` typing)
    lives downstream in :func:`app.domain.tasks.completion.
    _validate_gps_payload` — this sniff is the gate, not the parse.
    """
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    # The smell-test: at least one GPS key must be present. A bare
    # ``{}`` or an unrelated object (``{"foo": 1}``) never sniffs to
    # JSON — the sniffer would otherwise bless any well-formed JSON
    # as a valid GPS payload, defeating the per-kind check.
    if "lat" not in parsed and "lon" not in parsed:
        return None
    return "application/json"
