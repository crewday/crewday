"""Deterministic :class:`~app.adapters.llm.ports.LLMClient` fake.

``EchoLLMClient`` echoes back the prompt (or last chat message), emits
trivial token counts, and raises :class:`LLMCapabilityMissing("ocr")`
on :meth:`ocr` so tests that expect the "this client can't OCR"
branch exercise it.

See ``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from app.adapters.llm.ports import (
    ChatMessage,
    LLMCapabilityMissing,
    LLMResponse,
    LLMUsage,
)

__all__ = ["EchoLLMClient"]


class EchoLLMClient:
    """Deterministic LLM stub.

    * :meth:`complete` returns the prompt verbatim.
    * :meth:`chat` / :meth:`stream_chat` return / yield the last
      ``user`` or ``assistant`` turn (or ``""`` when ``messages`` is
      empty).
    * :meth:`ocr` raises :class:`LLMCapabilityMissing` with
      ``capability="ocr"``.
    """

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        return LLMResponse(
            text=prompt,
            usage=LLMUsage(
                prompt_tokens=len(prompt),
                completion_tokens=len(prompt),
                total_tokens=2 * len(prompt),
            ),
            model_id=model_id,
            finish_reason="stop",
        )

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        last = messages[-1]["content"] if messages else ""
        return LLMResponse(
            text=last,
            usage=LLMUsage(
                prompt_tokens=len(last),
                completion_tokens=len(last),
                total_tokens=2 * len(last),
            ),
            model_id=model_id,
            finish_reason="stop",
        )

    def ocr(self, *, model_id: str, image_bytes: bytes) -> str:
        raise LLMCapabilityMissing("ocr")

    def stream_chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Iterator[str]:
        last = messages[-1]["content"] if messages else ""
        yield from last.split()
