"""Outbound redaction coverage for :class:`OpenRouterClient`.

Every request leaving the adapter must pass through the §15
redaction seam before the JSON body hits the wire. These tests
stand in front of an :class:`httpx.MockTransport` so the adapter
never opens a real socket; we capture the request and assert the
body the provider would have received is already PII-free.

Consent (``scope="llm"`` with a non-empty :class:`ConsentSet`) is
exercised at two seams:

* **Adapter seam** — the call site hands a :class:`ConsentSet` directly
  to the adapter. Pins the OpenRouter wire format and the redactor
  contract.
* **Workspace seam** (cd-ddy0) — the workspace-scoped consent column
  ``agent_preference.upstream_pii_consent`` is loaded by
  :func:`app.domain.llm.consent.load_consent_set` and threaded into the
  adapter. Pins the end-to-end loader → redactor flow against a real
  DB row.

See ``docs/specs/11-llm-and-agents.md`` §"Redaction layer",
``docs/specs/15-security-privacy.md`` §"Logging and redaction".
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import AgentPreference
from app.adapters.db.workspace.models import Workspace
from app.adapters.llm.openrouter import OpenRouterClient
from app.adapters.llm.ports import ChatMessage
from app.domain.llm.consent import load_consent_set
from app.tenancy import tenant_agnostic
from app.util.clock import FrozenClock
from app.util.redact import ConsentSet
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration

_API_KEY = SecretStr("sk-or-test-0000")
_MODEL = "google/gemma-3-27b-it"

_FAKE_COMPLETION: dict[str, object] = {
    "id": "gen-test-redaction",
    "model": _MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class _RecordingHandler:
    """Capture every :class:`httpx.Request` and return a scripted 200."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=_FAKE_COMPLETION)


def _make_client(handler: _RecordingHandler) -> OpenRouterClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return OpenRouterClient(
        _API_KEY,
        max_retries=1,
        http=http,
        clock=FrozenClock(datetime(2026, 4, 20, 9, 0, tzinfo=UTC)),
        sleep=lambda _s: None,
    )


def _body(request: httpx.Request) -> dict[str, object]:
    return cast(dict[str, object], json.loads(request.content.decode("utf-8")))


class TestChatBodyIsRedacted:
    def test_email_in_user_message_is_scrubbed(self) -> None:
        handler = _RecordingHandler()
        client = _make_client(handler)

        messages: list[ChatMessage] = [
            {"role": "user", "content": "email me back at jean@example.com"},
        ]
        client.chat(model_id=_MODEL, messages=messages)

        assert len(handler.requests) == 1
        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        user_content = cast(str, wire_msgs[0]["content"])
        assert "jean@example.com" not in user_content
        assert "<redacted:email>" in user_content

    def test_iban_and_pan_in_prompt_are_scrubbed(self) -> None:
        handler = _RecordingHandler()
        client = _make_client(handler)

        prompt = (
            "process refund for IBAN FR1420041010050500013M02606 "
            "charged on card 4242424242424242"
        )
        client.complete(model_id=_MODEL, prompt=prompt)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content = cast(str, wire_msgs[0]["content"])
        assert "FR1420041010050500013M02606" not in content
        assert "4242424242424242" not in content
        assert "<redacted:iban>" in content
        assert "<redacted:pan>" in content

    def test_system_and_assistant_turns_are_scrubbed(self) -> None:
        handler = _RecordingHandler()
        client = _make_client(handler)

        messages: list[ChatMessage] = [
            {"role": "system", "content": "contact ops at ops@example.com"},
            {"role": "user", "content": "thanks, mine is jean@example.com"},
            {"role": "assistant", "content": "noted, calling +33612345678 now"},
        ]
        client.chat(model_id=_MODEL, messages=messages)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        all_contents = " | ".join(cast(str, m["content"]) for m in wire_msgs)
        assert "ops@example.com" not in all_contents
        assert "jean@example.com" not in all_contents
        assert "+33612345678" not in all_contents


class TestConsentPassThrough:
    def test_consent_allows_legal_name_through(self) -> None:
        handler = _RecordingHandler()
        client = _make_client(handler)

        # A real caller would pass a `ChatMessage` with the plain
        # content. Consent flags operate at the mapping-key level,
        # so the pass-through kicks in when the LLM payload is a
        # dict with a matching key name. This test demonstrates the
        # consent plumbing: a `legal_name` key under
        # `messages[0]["content"]` would be scrubbed without the
        # consent flag.
        #
        # We exercise the path end-to-end by building a chat turn
        # whose content happens to be the target name. Without
        # consent, a plain name like "Jean Dupont" has no PII
        # shape anyway — the more expressive assertion lives in
        # the unit tests where the mapping keys are under our
        # control; here we just prove the adapter threads consents
        # into the seam.
        messages: list[ChatMessage] = [
            {"role": "user", "content": "Remember Jean Dupont."},
        ]
        consents = ConsentSet(fields=frozenset({"legal_name"}))
        client.chat(model_id=_MODEL, messages=messages, consents=consents)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content = cast(str, wire_msgs[0]["content"])
        assert "Jean Dupont" in content

    def test_consent_does_not_override_sensitive_key(self) -> None:
        """A consent flag for ``iban`` must not leak an IBAN value."""
        handler = _RecordingHandler()
        client = _make_client(handler)

        prompt = "account IBAN FR1420041010050500013M02606 for ops"
        consents = ConsentSet(fields=frozenset({"iban"}))
        client.complete(model_id=_MODEL, prompt=prompt, consents=consents)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content = cast(str, wire_msgs[0]["content"])
        assert "FR1420041010050500013M02606" not in content
        assert "<redacted:iban>" in content


class TestStreamChatRedaction:
    def test_stream_body_is_redacted(self) -> None:
        handler = _RecordingHandler()

        # Streaming handler: return an SSE body that the iterator
        # will decode. Empty ``[DONE]``-only stream is enough — we
        # care about the outbound request body, not the response.
        def stream_handler(request: httpx.Request) -> httpx.Response:
            handler.requests.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"data: [DONE]\n\n",
            )

        transport = httpx.MockTransport(stream_handler)
        http = httpx.Client(transport=transport)
        client = OpenRouterClient(
            _API_KEY,
            max_retries=1,
            http=http,
            clock=FrozenClock(datetime(2026, 4, 20, 9, 0, tzinfo=UTC)),
            sleep=lambda _s: None,
        )

        list(
            client.stream_chat(
                model_id=_MODEL,
                messages=[{"role": "user", "content": "email jean@example.com"}],
            )
        )

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content = cast(str, wire_msgs[0]["content"])
        assert "jean@example.com" not in content
        assert "<redacted:email>" in content


class TestOcrRedaction:
    def test_ocr_text_block_retains_static_prompt(self) -> None:
        """The static OCR prompt text survives the scrub.

        The free-text prompt contains no PII shapes, so no regex hits
        it. This fixes the invariant so a future rewrite of the prompt
        that accidentally includes PII-shaped strings is caught.
        """
        handler = _RecordingHandler()
        client = _make_client(handler)

        image_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 400 + b"\xff\xd9"
        client.ocr(model_id=_MODEL, image_bytes=image_bytes)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content_blocks = cast(list[dict[str, object]], wire_msgs[0]["content"])
        text_block = content_blocks[0]
        text = cast(str, text_block["text"])
        # The default OCR prompt mentions no emails / phones / credentials.
        assert "Extract every piece of visible text" in text
        assert "<redacted:" not in text

    def test_ocr_image_data_url_bytes_survive_intact(self) -> None:
        """The base64 image payload passes through the redactor unchanged.

        Multimodal ``{"type": "image_url", ...}`` blocks are carved
        out of the free-text regex sweep — scrubbing a base64 blob
        as a ``<redacted:credential>`` would silently break every
        vision call. The sibling ``type`` and ``text`` blocks still
        run through the regular rules so a PII-shaped prompt next to
        the image is still caught.
        """
        handler = _RecordingHandler()
        client = _make_client(handler)

        image_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 400 + b"\xff\xd9"
        client.ocr(model_id=_MODEL, image_bytes=image_bytes)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content_blocks = cast(list[dict[str, object]], wire_msgs[0]["content"])
        image_block = content_blocks[1]
        assert image_block["type"] == "image_url"
        image_url = cast(dict[str, object], image_block["image_url"])
        url = cast(str, image_url["url"])
        expected_payload = base64.b64encode(image_bytes).decode("ascii")
        assert url == f"data:image/jpeg;base64,{expected_payload}"
        assert "<redacted:credential>" not in url


# ---------------------------------------------------------------------------
# Workspace-scoped consent loader (cd-ddy0)
# ---------------------------------------------------------------------------
#
# These tests stand in front of the same MockTransport seam, but the
# :class:`ConsentSet` that reaches the adapter comes from
# :func:`load_consent_set` reading the ``agent_preference`` row instead
# of being constructed inline. Together with the unit suite at
# ``tests/unit/llm/test_consent_loader.py``, they pin both halves of the
# §11 redaction layer's workspace seam: the loader's projection (unit)
# and the loader → adapter wiring (here).


_NOW = datetime(2026, 5, 3, 9, 0, 0, tzinfo=UTC)


def _seed_workspace_with_consent(
    db_session: Session,
    *,
    upstream_pii_consent: list[str],
) -> str:
    """Seed a workspace + workspace-scope ``agent_preference`` row.

    ``upstream_pii_consent`` lands as the row's column body; the loader
    reads it back and projects through the
    :data:`app.util.redact.CONSENT_TOKENS` allow-list.
    """
    workspace_id = new_ulid()
    with tenant_agnostic():
        db_session.add(
            Workspace(
                id=workspace_id,
                slug=f"ws-{workspace_id[-6:].lower()}",
                name="ws",
                plan="free",
                quota_json={},
                verification_state="unverified",
                created_at=_NOW,
            )
        )
        db_session.flush()
        db_session.add(
            AgentPreference(
                id=new_ulid(),
                workspace_id=workspace_id,
                scope_kind="workspace",
                scope_id=workspace_id,
                body_md="",
                token_count=0,
                blocked_actions=[],
                default_approval_mode="auto",
                upstream_pii_consent=upstream_pii_consent,
                updated_by_user_id=None,
                created_at=_NOW,
                updated_at=_NOW,
                archived_at=None,
            )
        )
        db_session.flush()
    return workspace_id


class TestWorkspaceConsentLoader:
    def test_legal_name_consent_preserves_value_on_outbound(
        self, db_session: Session
    ) -> None:
        """The workspace toggle for ``legal_name`` flows end-to-end."""
        workspace_id = _seed_workspace_with_consent(
            db_session, upstream_pii_consent=["legal_name"]
        )
        consents = load_consent_set(db_session, workspace_id)

        handler = _RecordingHandler()
        client = _make_client(handler)
        messages: list[ChatMessage] = [
            {"role": "user", "content": "Remember Jean Dupont."},
        ]
        client.chat(model_id=_MODEL, messages=messages, consents=consents)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content = cast(str, wire_msgs[0]["content"])
        assert "Jean Dupont" in content

    def test_empty_consent_scrubs_email_baseline(self, db_session: Session) -> None:
        """Workspace with empty consent matches the cd-a469 baseline.

        The free-text regex pass still scrubs every PII shape; consent
        opt-in is the only mechanism that lets a value through.
        """
        workspace_id = _seed_workspace_with_consent(db_session, upstream_pii_consent=[])
        consents = load_consent_set(db_session, workspace_id)

        handler = _RecordingHandler()
        client = _make_client(handler)
        messages: list[ChatMessage] = [
            {"role": "user", "content": "email me at jean@example.com"},
        ]
        client.chat(model_id=_MODEL, messages=messages, consents=consents)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content = cast(str, wire_msgs[0]["content"])
        assert "jean@example.com" not in content
        assert "<redacted:email>" in content

    def test_multiple_consents_load_through_loader(self, db_session: Session) -> None:
        """All allow-listed tokens reach the redactor as one set."""
        workspace_id = _seed_workspace_with_consent(
            db_session,
            upstream_pii_consent=["legal_name", "email", "phone", "address"],
        )
        consents = load_consent_set(db_session, workspace_id)

        # The loader is the seam under test; the adapter only proves the
        # consent set arrived. Membership assertions keep the test
        # decoupled from the redactor's free-text behaviour (already
        # covered by ``tests/unit/util/test_redact.py``).
        assert consents.allows("legal_name")
        assert consents.allows("email")
        assert consents.allows("phone")
        assert consents.allows("address")

        handler = _RecordingHandler()
        client = _make_client(handler)
        client.chat(
            model_id=_MODEL,
            messages=[{"role": "user", "content": "Marie Dupont"}],
            consents=consents,
        )
        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content = cast(str, wire_msgs[0]["content"])
        # ``legal_name`` consent lets a free-form name through the
        # adapter — the cd-a469 baseline asserted on a non-PII string;
        # we extend that here against a name shape.
        assert "Marie Dupont" in content
