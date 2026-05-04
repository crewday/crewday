"""LLM ports.

Defines the seam domain code uses to talk to a language-model
provider. Concrete v1 implementation is ``OpenRouterClient`` (see
``docs/specs/11-llm-and-agents.md``). Capability routing (which model
to pick for which task, which workspace budget to charge) is a
domain-layer concern; this protocol stays transport-agnostic.

Optional capabilities (e.g. OCR, streaming) are part of the same
protocol; adapters that do not implement a capability raise
:class:`LLMCapabilityMissing` with the capability name. Callers either
feature-detect beforehand (by asking the router) or handle the
exception.

The ``consents`` keyword on every method is the workspace-scoped
:class:`~app.util.redact.ConsentSet` that lets specific PII fields
pass through the §15 redaction seam. ``None`` (the default) means
"redact everything" — see :meth:`ConsentSet.none`. Adapters without
upstream calls (the in-process fake) accept the argument as a no-op
so the seam stays uniform across providers.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from app.util.redact import ConsentSet

__all__ = [
    "ChatMessage",
    "LLMCapabilityMissing",
    "LLMClient",
    "LLMResponse",
    "LLMUsage",
    "LlmContentRefused",
    "LlmProviderError",
    "LlmRateLimited",
    "LlmTransportError",
    "Tool",
    "ToolCall",
]


class LLMCapabilityMissing(Exception):
    """Raised by adapters that do not implement an optional capability.

    The string argument is the capability name (e.g. ``"ocr"``,
    ``"stream_chat"``). Callers can feature-detect by catching this
    exception or by asking the router up front.
    """

    def __init__(self, capability: str) -> None:
        super().__init__(f"LLM capability not supported by this client: {capability}")
        self.capability = capability


class LlmRateLimited(RuntimeError):
    """Raised after the retry budget is exhausted on provider rate limits."""


class LlmTransportError(RuntimeError):
    """Raised for transport-level provider failures."""


class LlmProviderError(RuntimeError):
    """Raised when the provider rejects a request as non-retryable."""


class LlmContentRefused(RuntimeError):
    """Raised when every rung in a chain refused on content grounds."""

    __slots__ = ("correlation_id", "fallback_attempts")

    def __init__(
        self,
        *args: object,
        fallback_attempts: int = 0,
        correlation_id: str = "",
    ) -> None:
        super().__init__(*args)
        self.fallback_attempts = fallback_attempts
        self.correlation_id = correlation_id


class ChatMessage(TypedDict):
    """A single role-tagged chat turn."""

    role: Literal["system", "user", "assistant"]
    content: str


class Tool(TypedDict):
    """A function-calling tool advertised to the model.

    Mirrors the OpenAI ``function`` shape (``name`` / ``description`` /
    ``parameters``) but uses ``input_schema`` to match the in-tree
    convention. Adapters serialise these into whatever wire shape the
    upstream provider expects (see :class:`OpenRouterClient` for the
    OpenAI-compatible mapping).

    ``input_schema`` is a JSON-Schema-shaped dict that is passed
    through to the provider verbatim — the runtime / adapter does not
    rewrite or validate it. Tool definitions are deployment-controlled
    (the dispatcher knows which tools exist), not user-input, so they
    skip the per-call PII redaction tuning.
    """

    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single native function-call request emitted by the model.

    ``arguments`` is decoded from the provider's wire format (OpenAI
    serialises it as a JSON-encoded string under ``function.arguments``;
    the OpenRouter adapter calls :func:`json.loads` once when parsing
    the response). The runtime feeds ``arguments`` into its own
    ``ToolCall.input`` dict — the seam stays mapping-typed so adapters
    are free to hand back an immutable view.
    """

    id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token accounting returned alongside every completion."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A non-streaming completion result.

    ``tool_calls`` carries the model's native function-calling output
    when the adapter supports it (OpenAI-compatible providers fill it
    from ``message.tool_calls``); the field defaults to an empty tuple
    so existing call sites keep working unchanged.
    """

    text: str
    usage: LLMUsage
    model_id: str
    finish_reason: str
    tool_calls: tuple[ToolCall, ...] = ()


class LLMClient(Protocol):
    """Language-model client.

    ``model_id`` is always provided by the caller — model selection is
    a domain-level concern, not an adapter concern.
    """

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        """Single-shot text completion."""
        ...

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: Sequence[Tool] | None = None,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        """Multi-turn chat completion.

        ``tools`` advertises native function-calling tools to the
        model. Adapters that surface the call structurally fill
        :attr:`LLMResponse.tool_calls`; adapters without function-
        calling support either ignore the argument or fall back to a
        text protocol (the agent runtime parses both).
        """
        ...

    def ocr(
        self,
        *,
        model_id: str,
        image_bytes: bytes,
        consents: ConsentSet | None = None,
    ) -> str:
        """Extract text from an image.

        Optional capability; adapters without vision raise
        :class:`LLMCapabilityMissing` with ``"ocr"``.
        """
        ...

    def stream_chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: Sequence[Tool] | None = None,
        consents: ConsentSet | None = None,
    ) -> Iterator[str]:
        """Stream chat tokens as they arrive.

        Optional capability; adapters without streaming raise
        :class:`LLMCapabilityMissing` with ``"stream_chat"``.

        ``tools`` mirrors :meth:`chat`; streaming tool-call deltas are
        out of scope for v1, but the surface stays symmetric so a
        future adapter can light up streaming function calls without a
        port revision.
        """
        ...
