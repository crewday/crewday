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
    Tool,
    ToolCall,
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

    A pre-canned ``tool_calls`` tuple may be supplied at construction;
    when set, every :meth:`chat` invocation returns the same response
    text but with the canned :class:`ToolCall` tuple attached. This
    lets domain-layer tests drive the runtime through the native
    function-calling path without a richer scripted client.
    """

    def __init__(
        self,
        *,
        tool_calls: tuple[ToolCall, ...] = (),
    ) -> None:
        self._tool_calls = tool_calls

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
        tools: Sequence[Tool] | None = None,
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
            tool_calls=self._tool_calls,
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
        tools: Sequence[Tool] | None = None,
    ) -> Iterator[str]:
        last = messages[-1]["content"] if messages else ""
        yield from last.split()
