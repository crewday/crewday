"""Deployment-admin LLM graph routes."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import math
import subprocess
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self, cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as FormUploadFile

from app.adapters.db.llm.models import (
    LlmAssignment,
    LlmCapabilityInheritance,
    LlmModel,
    LlmPromptTemplate,
    LlmPromptTemplateRevision,
    LlmProvider,
    LlmProviderModel,
    LlmUsage,
)
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.llm.fake import FakeLLMClient
from app.adapters.llm.fastembed import (
    FastEmbedEmbeddingClient,
    FastEmbedEmbeddingError,
)
from app.adapters.llm.ollama import OllamaClient
from app.adapters.llm.openrouter import (
    OpenRouterClient,
    OpenRouterModelMetadata,
    fetch_openrouter_model_metadata,
    normalize_openrouter_model_id,
)
from app.adapters.llm.ports import (
    ChatImageUrlBlock,
    ChatInputAudioBlock,
    ChatInputAudioRef,
    ChatMessage,
    ChatTextBlock,
    LLMClient,
    LlmProviderError,
    LlmRateLimited,
    LlmTransportError,
)
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.adapters.storage.ports import EnvelopeDecryptError, EnvelopeOwner
from app.api.admin.deps import (
    require_deployment_scope,
    require_deployment_session_scope,
)
from app.api.deps import db_session
from app.api.transport import admin_sse
from app.api.transport.correlation_id import request_correlation_id
from app.config import Settings
from app.domain.agent.compaction import (
    COMPACT_CAPABILITY as _COMPACT_CAPABILITY,
)
from app.domain.agent.compaction import (
    _default_compaction_prompt,
)
from app.domain.agent.runtime import _default_system_prompt
from app.domain.errors import (
    Conflict,
    NotFound,
    NotImplementedFeature,
    ServiceUnavailable,
    UpstreamUnavailable,
    Validation,
)
from app.domain.llm.router import (
    DEFAULT_LLM_CAPABILITY,
    DEPLOYMENT_DEFAULT_CACHE_WORKSPACE_ID,
)
from app.events.bus import bus as default_event_bus
from app.events.types import LlmAssignmentChanged
from app.net.fetch_guard import FetchGuardError, FetchGuardSizeLimit, safe_fetch
from app.tenancy import DeploymentContext, tenant_agnostic
from app.util.redact import ConsentSet, scrub_string
from app.util.ulid import new_ulid

__all__ = ["build_admin_llm_router"]


_Db = Annotated[Session, Depends(db_session)]
_ReadCtx = Annotated[
    DeploymentContext, Depends(require_deployment_scope("deployment.llm:read"))
]
_WriteCtx = Annotated[
    DeploymentContext, Depends(require_deployment_scope("deployment.llm:write"))
]
_SessionWriteCtx = Annotated[
    DeploymentContext,
    Depends(require_deployment_session_scope("deployment.llm:write")),
]


def _llm_cli(verb: str, summary: str, *, mutates: bool) -> dict[str, object]:
    return {
        "x-cli": {
            "group": "llm",
            "verb": verb,
            "summary": summary,
            "mutates": mutates,
        },
    }


def _llm_secret_cli(verb: str, summary: str) -> dict[str, object]:
    return {
        **_llm_cli(verb, summary, mutates=True),
        "x-agent-forbidden": True,
        "x-interactive-only": True,
    }


_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1"
_LLM_PROVIDER_API_KEY_PURPOSE = "llm_provider.api_key"
_ROW_BACKED_ENVELOPE_VERSION = 0x02
_CAPABILITIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        DEFAULT_LLM_CAPABILITY,
        "Deployment default fallback chain inherited by unassigned capabilities",
        ("chat", "function_calling"),
    ),
    (
        "tasks.nl_intake",
        "Parse a free-text description into a task / template / schedule draft",
        ("chat", "json_mode"),
    ),
    ("tasks.assist", "Staff chat assistant", ("chat",)),
    ("digest.manager", "Morning owner/manager digest composition", ("chat",)),
    ("digest.employee", "Morning worker digest composition", ("chat",)),
    (
        "anomaly.detect",
        "Compare recent completions and flag anomalies",
        ("chat", "json_mode"),
    ),
    ("expenses.autofill", "OCR and structure a receipt image", ("vision", "json_mode")),
    ("instructions.draft", "Suggest an instruction from a conversation", ("chat",)),
    (
        "issue.triage",
        "Classify severity/category of a reported issue",
        ("chat", "json_mode"),
    ),
    ("stay.summarize", "Summarize a stay", ("chat",)),
    ("voice.transcribe", "Turn a voice note into text", ("audio_input",)),
    (
        "chat.manager",
        "Owner/manager-side embedded chat agent",
        ("chat", "function_calling"),
    ),
    ("chat.employee", "Worker-side embedded chat agent", ("chat", "function_calling")),
    (
        "chat.admin",
        "Deployment-admin embedded chat agent",
        ("chat", "function_calling"),
    ),
    ("chat.compact", "Summarise resolved topics in a chat thread", ("chat",)),
    (
        "chat.detect_language",
        "Detect message language for auto-translation",
        ("chat", "json_mode"),
    ),
    (
        "chat.translate",
        "Translate a message into the workspace default language",
        ("chat",),
    ),
    ("documents.ocr", "Vision fallback for image-bearing documents", ("vision",)),
    (
        "feedback.moderate",
        "Moderate and reformulate a marketing-site suggestion",
        ("chat", "json_mode"),
    ),
    ("feedback.embed", "Compute dense embeddings for texts", ("embeddings",)),
    (
        "feedback.cluster",
        "Classify a reformulated marketing-site submission against clusters",
        ("chat", "json_mode"),
    ),
)
_CAPABILITY_REQUIRED: dict[str, list[str]] = {
    key: list(required) for key, _description, required in _CAPABILITIES
}
_CAPABILITY_DESCRIPTIONS: dict[str, str] = {
    key: description for key, description, _required in _CAPABILITIES
}
_MODEL_CAPABILITY_TAGS = frozenset(
    {
        "chat",
        "vision",
        "audio_input",
        "reasoning",
        "function_calling",
        "json_mode",
        "streaming",
        "embeddings",
    }
)
_PLAYGROUND_IMAGE_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
_PLAYGROUND_AUDIO_UPLOAD_MAX_BYTES = 25 * 1024 * 1024
_PLAYGROUND_IMAGE_URL_FETCH_TIMEOUT_S: Final[float] = 5.0
_PLAYGROUND_IMAGE_URL_FETCH_SCHEMES: Final[frozenset[str]] = frozenset({"https"})
_PLAYGROUND_MEDIA_CONVERSION_TIMEOUT_S: Final[float] = 15.0
_PLAYGROUND_MAX_TOKENS_LIMIT = 32_000
_PLAYGROUND_DEFAULT_VISION_PROMPT: Final[str] = "Extract the text from this image."
_PLAYGROUND_DEFAULT_AUDIO_PROMPT: Final[str] = "Transcribe this audio."
_PLAYGROUND_AUDIO_FORMAT_BY_CONTENT_TYPE: Final[dict[str, str]] = {
    "audio/aac": "aac",
    "audio/aiff": "aiff",
    "audio/flac": "flac",
    "audio/m4a": "m4a",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/webm": "webm",
    "audio/x-aiff": "aiff",
    "audio/x-flac": "flac",
    "audio/x-m4a": "m4a",
    "audio/x-wav": "wav",
    "video/webm": "webm",
}
_PLAYGROUND_AUDIO_FORMATS: Final[frozenset[str]] = frozenset(
    {"aac", "aiff", "flac", "m4a", "mp3", "ogg", "pcm16", "pcm24", "wav", "webm"}
)
type PlaygroundAudioRef = ChatInputAudioRef
type PlaygroundMediaKind = Literal["audio", "image"]
LlmThinkingLevel = Literal["disabled", "low", "medium", "high"]
LlmThinkingStrategy = Literal[
    "none",
    "gemma_system_token",
    "glm_extra_body",
    "openrouter_extra_body",
]
LlmProviderType = Literal[
    "openrouter", "openai_compatible", "ollama", "fake", "local_embedding"
]
LlmAudioInputTransform = Literal["passthrough", "wav_16khz_mono"]
LlmImageInputFormat = Literal["preserve", "jpeg", "png", "webp"]


def _image_input_format(value: str | None) -> LlmImageInputFormat:
    if value == "jpeg":
        return "jpeg"
    if value == "png":
        return "png"
    if value == "webp":
        return "webp"
    return "preserve"


def _thinking_level(value: str | None) -> LlmThinkingLevel:
    if value in {"disabled", "low", "medium", "high"}:
        return cast(LlmThinkingLevel, value)
    return "disabled"


def _thinking_strategy(value: str | None) -> LlmThinkingStrategy:
    if value in {
        "none",
        "gemma_system_token",
        "glm_extra_body",
        "openrouter_extra_body",
    }:
        return cast(LlmThinkingStrategy, value)
    return "none"


class LlmProviderResponse(BaseModel):
    id: str
    name: str
    provider_type: LlmProviderType
    endpoint: str
    api_key_ref: str | None
    api_key_status: Literal["present", "missing", "rotating"]
    default_model: str | None
    requests_per_minute: int
    timeout_s: int
    is_enabled: bool
    provider_model_count: int
    spend_usd_30d: float = 0.0
    calls_30d: int = 0


class LlmModelResponse(BaseModel):
    id: str
    canonical_name: str
    display_name: str
    capabilities: list[str]
    context_window: int | None
    max_output_tokens: int | None
    embedding_dimensions: int | None
    temperature: float | None
    thinking_level: LlmThinkingLevel
    thinking_strategy: LlmThinkingStrategy
    price_source: Literal["openrouter", "manual", ""]
    price_source_model_id: str | None
    is_active: bool
    notes: str | None
    provider_model_count: int
    spend_usd_30d: float = 0.0
    calls_30d: int = 0


class LlmProviderModelResponse(BaseModel):
    id: str
    provider_id: str
    model_id: str
    api_model_id: str
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    fixed_cost_per_call_usd: float | None = None
    audio_cost_per_hour_usd: float | None = None
    audio_input_transform: LlmAudioInputTransform
    image_input_format: LlmImageInputFormat
    image_input_max_edge_px: int | None
    max_tokens_override: int | None
    supports_system_prompt: bool
    supports_temperature: bool
    thinking_strategy_override: LlmThinkingStrategy | None
    effective_thinking_strategy: LlmThinkingStrategy
    extra_api_params: dict[str, Any]
    price_source_override: Literal["", "none", "openrouter"]
    price_source_model_id_override: str | None
    price_last_synced_at: str | None
    is_enabled: bool
    spend_usd_30d: float = 0.0
    calls_30d: int = 0


class LlmCapabilityEntry(BaseModel):
    key: str
    description: str
    required_capabilities: list[str]
    spend_usd_30d: float = 0.0
    calls_30d: int = 0
    direct_spend_usd_30d: float = 0.0
    direct_calls_30d: int = 0
    inherited_spend_usd_30d: float = 0.0
    inherited_calls_30d: int = 0


class LlmCapabilityInheritanceResponse(BaseModel):
    capability: str
    inherits_from: str
    source: Literal["explicit", "implicit_default"] = "explicit"


class LlmAssignmentResponse(BaseModel):
    id: str
    capability: str
    description: str
    priority: int
    provider_model_id: str
    max_tokens: int | None
    temperature: float | None
    thinking_level_override: LlmThinkingLevel | None
    effective_thinking_level: LlmThinkingLevel
    effective_thinking_strategy: LlmThinkingStrategy
    extra_api_params: dict[str, Any]
    required_capabilities: list[str]
    is_enabled: bool
    last_used_at: str | None
    spend_usd_30d: float
    calls_30d: int
    direct_spend_usd_30d: float = 0.0
    direct_calls_30d: int = 0
    inherited_spend_usd_30d: float = 0.0
    inherited_calls_30d: int = 0
    is_deployment_default: bool = False


class LlmAssignmentIssue(BaseModel):
    assignment_id: str
    capability: str
    missing_capabilities: list[str]


class LlmPromptTemplateResponse(BaseModel):
    id: str
    capability: str
    name: str
    version: int
    is_active: bool
    is_customised: bool
    default_hash: str
    updated_at: str
    revisions_count: int
    preview: str


class LlmPromptTemplateDetail(LlmPromptTemplateResponse):
    template: str
    notes: str | None


class LlmPromptRevisionResponse(BaseModel):
    id: str
    template_id: str
    version: int
    body: str
    notes: str | None
    created_at: str
    created_by_user_id: str | None


class LlmCallResponse(BaseModel):
    """One row of the /admin/usage call feed.

    ``model_id`` and ``provider_model_id`` are intentionally distinct
    fields and **not** redundant — do not collapse them:

    * ``model_id`` is the **wire-name string** that flowed across the
      network on this call (sourced from ``llm_usage.provider_model_id``,
      which stores the free-form provider wire name — see
      :class:`~app.adapters.db.llm.models.LlmUsage`). Always present;
      survives a registry row's retirement so historical calls still
      render.
    * ``provider_model_id`` is the **resolved registry id** (a
      ``LlmProviderModel.id``), looked up at read time from the wire
      string via :func:`_llm_usage_provider_model_id`. ``None`` when
      the registry row has been retired since the call was made.

    The DB column was renamed in cd-v6dj from ``model_id`` to
    ``provider_model_id`` to match the §02 spec, but the JSON wire
    contract here keeps both fields under their pre-existing names
    because the SPA and OpenAPI consumers depend on the shape.
    """

    at: str
    capability: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    cost_cents: int
    latency_ms: int
    status: Literal["ok", "error", "redacted_block"]
    assignment_id: str | None = None
    provider_model_id: str | None = None
    prompt_template_id: str | None = None
    prompt_version: int | None = None
    fallback_attempts: int = 0
    raw_response_available: bool = False


class LlmGraphTotals(BaseModel):
    spend_usd_30d: float
    calls_30d: int
    provider_count: int
    model_count: int
    capability_count: int
    unassigned_capabilities: list[str]


class LlmGraphPayload(BaseModel):
    providers: list[LlmProviderResponse]
    models: list[LlmModelResponse]
    provider_models: list[LlmProviderModelResponse]
    capabilities: list[LlmCapabilityEntry]
    inheritance: list[LlmCapabilityInheritanceResponse]
    assignments: list[LlmAssignmentResponse]
    assignment_issues: list[LlmAssignmentIssue]
    totals: LlmGraphTotals


class LlmSyncPricingDelta(BaseModel):
    provider_model_id: str
    api_model_id: str
    source: Literal["openrouter"] | None = None
    lookup_id: str | None = None
    input_before: float
    input_after: float
    output_before: float
    output_after: float
    fixed_before: float | None = None
    fixed_after: float | None = None
    audio_before: float | None = None
    audio_after: float | None = None
    price_last_synced_at: str | None = None
    status: Literal["updated", "unchanged", "skipped_not_syncable", "error"]


class LlmSyncPricingResult(BaseModel):
    started_at: str
    deltas: list[LlmSyncPricingDelta]
    updated: int
    skipped: int
    errors: int


class LlmSyncPricingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_model_ids: list[str] | None = None
    dry_run: bool = False


class LlmProviderModelSyncPricingResponse(BaseModel):
    provider_model: LlmProviderModelResponse
    pricing_sync_result: LlmSyncPricingDelta


class LlmProviderModelPlaygroundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["direct", "assignment"] = "direct"
    prompt: str = Field(default="", max_length=16_000)
    system_prompt: str | None = Field(default=None, max_length=8_000)
    max_tokens: int | None = Field(default=None, ge=1, le=32_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    image_url: str | None = Field(default=None, max_length=262_144)
    audio_url: str | None = Field(default=None, max_length=262_144)
    assignment_id: str | None = None
    thinking_level: LlmThinkingLevel | None = None
    thinking_strategy: LlmThinkingStrategy | None = None

    @field_validator("prompt")
    @classmethod
    def _strip_prompt(cls, value: str) -> str:
        return value.strip()

    @field_validator("system_prompt", "image_url", "audio_url")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _validate_mode_assignment(self) -> Self:
        if self.mode == "assignment" and self.assignment_id is None:
            raise ValueError("assignment mode requires assignment_id")
        if self.mode == "direct" and self.assignment_id is not None:
            raise ValueError("assignment_id requires assignment mode")
        return self


def _playground_form_text(form: FormData, name: str) -> str | None:
    value = form.get(name)
    if value is None:
        return None
    if isinstance(value, FormUploadFile):
        raise _unprocessable("playground_field_invalid")
    text = str(value).strip()
    return text or None


async def _playground_upload_data_url(upload: FormUploadFile) -> str:
    content_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    if not content_type.startswith("image/"):
        await upload.close()
        raise _unprocessable("playground_image_type_unsupported")

    total = 0
    pieces: list[bytes] = []
    while True:
        read_size = min(64 * 1024, _PLAYGROUND_IMAGE_UPLOAD_MAX_BYTES + 1 - total)
        chunk = await upload.read(read_size)
        if not chunk:
            break
        total += len(chunk)
        if total > _PLAYGROUND_IMAGE_UPLOAD_MAX_BYTES:
            await upload.close()
            raise _unprocessable("playground_image_file_too_large")
        pieces.append(chunk)
    await upload.close()

    payload = b"".join(pieces)
    if not payload:
        raise _unprocessable("playground_image_empty")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _playground_decode_data_url(
    data_url: str,
    *,
    expected_prefix: str,
    error: str,
) -> tuple[str, bytes]:
    prefix, sep, payload = data_url.partition(",")
    if not sep:
        raise _unprocessable(error)
    media_type = prefix.removeprefix("data:").split(";", 1)[0].lower()
    if not media_type.startswith(expected_prefix):
        raise _unprocessable(error)
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _unprocessable(error) from exc
    if not decoded:
        raise _unprocessable(error)
    return media_type, decoded


def _playground_audio_format(content_type: str, filename_or_url: str = "") -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    filename = filename_or_url.rsplit("/", 1)[-1]
    filename = filename.split("?", 1)[0].split("#", 1)[0]
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    format_name = _PLAYGROUND_AUDIO_FORMAT_BY_CONTENT_TYPE.get(media_type, suffix)
    if format_name == "wave":
        format_name = "wav"
    if format_name not in _PLAYGROUND_AUDIO_FORMATS:
        raise _unprocessable("playground_audio_type_unsupported")
    if media_type and not (
        media_type.startswith("audio/")
        or media_type in {"application/octet-stream", "video/webm"}
    ):
        raise _unprocessable("playground_audio_type_unsupported")
    return format_name


def _playground_audio_ref_from_bytes(
    payload: bytes,
    *,
    content_type: str,
    filename_or_url: str = "",
) -> PlaygroundAudioRef:
    if not payload:
        raise _unprocessable("playground_audio_empty")
    return {
        "data": base64.b64encode(payload).decode("ascii"),
        "format": _playground_audio_format(content_type, filename_or_url),
    }


def _playground_media_suffix(media_type: str) -> str:
    subtype = media_type.split("/", 1)[-1].split("+", 1)[0].lower()
    if subtype in {"jpeg", "jpg"}:
        return ".jpg"
    if subtype in {"mpeg", "mp3"}:
        return ".mp3"
    if subtype:
        return f".{subtype}"
    return ".bin"


def _playground_ffmpeg_convert(
    payload: bytes,
    *,
    input_suffix: str,
    output_suffix: str,
    output_max_bytes: int,
    args: list[str],
    kind: PlaygroundMediaKind,
) -> bytes:
    if not payload:
        raise _unprocessable(f"playground_{kind}_empty")
    with tempfile.TemporaryDirectory(prefix=f"crewday-playground-{kind}-") as tmp_raw:
        tmp = Path(tmp_raw)
        input_path = tmp / f"input{input_suffix}"
        output_path = tmp / f"output{output_suffix}"
        input_path.write_bytes(payload)
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            *args,
            str(output_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=_PLAYGROUND_MEDIA_CONVERSION_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _unprocessable(
                f"playground_{kind}_conversion_failed",
                message=(
                    f"{kind.capitalize()} could not be converted for this provider."
                ),
            ) from exc
        if result.returncode != 0 or not output_path.exists():
            raise _unprocessable(
                f"playground_{kind}_conversion_failed",
                message=(
                    f"{kind.capitalize()} could not be converted for this provider."
                ),
            )
        converted = output_path.read_bytes()
    if not converted:
        raise _unprocessable(f"playground_{kind}_conversion_failed")
    if len(converted) > output_max_bytes:
        raise _unprocessable(
            f"playground_{kind}_file_too_large",
            message=f"Converted {kind} is too large for a playground run.",
        )
    return converted


async def _playground_normalized_audio_ref(
    audio_ref: PlaygroundAudioRef | None,
    provider_model: LlmProviderModel,
) -> PlaygroundAudioRef | None:
    if audio_ref is None or provider_model.audio_input_transform == "passthrough":
        return audio_ref

    try:
        payload = base64.b64decode(audio_ref["data"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _unprocessable("playground_audio_conversion_failed") from exc

    converted = await asyncio.to_thread(
        _playground_ffmpeg_convert,
        payload,
        input_suffix=f".{audio_ref['format']}",
        output_suffix=".wav",
        output_max_bytes=_PLAYGROUND_AUDIO_UPLOAD_MAX_BYTES,
        kind="audio",
        args=[
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            "-f",
            "wav",
        ],
    )
    return {
        "data": base64.b64encode(converted).decode("ascii"),
        "format": "wav",
    }


def _playground_image_output_type(
    media_type: str, image_input_format: LlmImageInputFormat
) -> tuple[str, str, bool]:
    if image_input_format == "preserve":
        normalized_media_type = (
            "image/jpeg" if media_type == "image/jpg" else media_type
        )
    else:
        normalized_media_type = f"image/{image_input_format}"
    suffix = _playground_media_suffix(normalized_media_type)
    return normalized_media_type, suffix, normalized_media_type == "image/jpeg"


def _playground_image_scale_filter(max_edge_px: int | None) -> str | None:
    if max_edge_px is None:
        return None
    return (
        "scale="
        f"'if(gt(max(iw,ih),{max_edge_px}),"
        f"if(gte(iw,ih),{max_edge_px},-1),iw)':"
        f"'if(gt(max(iw,ih),{max_edge_px}),"
        f"if(gte(iw,ih),-1,{max_edge_px}),ih)'"
    )


async def _playground_normalized_image_ref(
    image_ref: str | None,
    provider_model: LlmProviderModel,
) -> str | None:
    if image_ref is None:
        return None
    if (
        provider_model.image_input_format == "preserve"
        and provider_model.image_input_max_edge_px is None
    ):
        return image_ref

    media_type, payload = _playground_decode_data_url(
        image_ref,
        expected_prefix="image/",
        error="playground_image_conversion_failed",
    )
    image_input_format = _image_input_format(provider_model.image_input_format)
    output_media_type, output_suffix, strip_alpha = _playground_image_output_type(
        media_type, image_input_format
    )
    filters = [
        value
        for value in (
            _playground_image_scale_filter(provider_model.image_input_max_edge_px),
            "format=rgb24" if strip_alpha else None,
        )
        if value is not None
    ]
    args = ["-frames:v", "1"]
    if filters:
        args.extend(["-vf", ",".join(filters)])
    converted = await asyncio.to_thread(
        _playground_ffmpeg_convert,
        payload,
        input_suffix=_playground_media_suffix(media_type),
        output_suffix=output_suffix,
        output_max_bytes=_PLAYGROUND_IMAGE_UPLOAD_MAX_BYTES,
        kind="image",
        args=args,
    )
    encoded = base64.b64encode(converted).decode("ascii")
    return f"data:{output_media_type};base64,{encoded}"


async def _playground_upload_audio_ref(
    upload: FormUploadFile,
) -> PlaygroundAudioRef:
    try:
        content_type = upload.content_type or ""
        total = 0
        pieces: list[bytes] = []
        while True:
            read_size = min(
                64 * 1024,
                _PLAYGROUND_AUDIO_UPLOAD_MAX_BYTES + 1 - total,
            )
            chunk = await upload.read(read_size)
            if not chunk:
                break
            total += len(chunk)
            if total > _PLAYGROUND_AUDIO_UPLOAD_MAX_BYTES:
                raise _unprocessable("playground_audio_file_too_large")
            pieces.append(chunk)

        payload = b"".join(pieces)
        return _playground_audio_ref_from_bytes(
            payload,
            content_type=content_type,
            filename_or_url=upload.filename or "",
        )
    finally:
        await upload.close()


def _is_playground_data_image_url(value: str) -> bool:
    prefix, sep, _payload = value.partition(",")
    return (
        bool(sep)
        and prefix.lower().startswith("data:image/")
        and ";base64" in prefix.lower()
    )


async def _playground_image_url_data_url(image_url: str) -> str:
    if _is_playground_data_image_url(image_url):
        return image_url

    try:
        response = await safe_fetch(
            image_url,
            timeout_seconds=_PLAYGROUND_IMAGE_URL_FETCH_TIMEOUT_S,
            max_body_bytes=_PLAYGROUND_IMAGE_UPLOAD_MAX_BYTES,
            allowed_schemes=_PLAYGROUND_IMAGE_URL_FETCH_SCHEMES,
        )
    except FetchGuardSizeLimit as exc:
        raise _unprocessable(
            "playground_image_file_too_large",
            message="Image URL response is too large for a playground run.",
        ) from exc
    except FetchGuardError as exc:
        raise _unprocessable(
            "playground_image_url_unavailable",
            message="Image URL must be a public HTTPS image or a base64 data URL.",
        ) from exc

    if not 200 <= response.status_code < 300:
        raise _unprocessable(
            "playground_image_url_unavailable",
            message="Image URL could not be fetched for the playground run.",
        )

    content_type = response.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type.startswith("image/"):
        raise _unprocessable(
            "playground_image_type_unsupported",
            message="Image URL did not return an image content type.",
        )
    if not response.content:
        raise _unprocessable("playground_image_empty")

    encoded = base64.b64encode(response.content).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _is_playground_data_audio_url(value: str) -> bool:
    prefix, sep, _payload = value.partition(",")
    return (
        bool(sep)
        and prefix.lower().startswith("data:audio/")
        and ";base64" in prefix.lower()
    )


def _playground_data_audio_ref(audio_url: str) -> PlaygroundAudioRef:
    prefix, _sep, payload = audio_url.partition(",")
    media_type = prefix.removeprefix("data:").split(";", 1)[0]
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _unprocessable("playground_audio_url_unavailable") from exc
    return _playground_audio_ref_from_bytes(
        decoded,
        content_type=media_type,
        filename_or_url=audio_url,
    )


async def _playground_audio_url_ref(audio_url: str) -> PlaygroundAudioRef:
    if _is_playground_data_audio_url(audio_url):
        return _playground_data_audio_ref(audio_url)

    try:
        response = await safe_fetch(
            audio_url,
            timeout_seconds=_PLAYGROUND_IMAGE_URL_FETCH_TIMEOUT_S,
            max_body_bytes=_PLAYGROUND_AUDIO_UPLOAD_MAX_BYTES,
            allowed_schemes=_PLAYGROUND_IMAGE_URL_FETCH_SCHEMES,
        )
    except FetchGuardSizeLimit as exc:
        raise _unprocessable(
            "playground_audio_file_too_large",
            message="Audio URL response is too large for a playground run.",
        ) from exc
    except FetchGuardError as exc:
        raise _unprocessable(
            "playground_audio_url_unavailable",
            message="Audio URL must be a public HTTPS audio file or a base64 data URL.",
        ) from exc

    if not 200 <= response.status_code < 300:
        raise _unprocessable(
            "playground_audio_url_unavailable",
            message="Audio URL could not be fetched for the playground run.",
        )

    return _playground_audio_ref_from_bytes(
        response.content,
        content_type=response.headers.get("content-type", ""),
        filename_or_url=audio_url,
    )


async def _playground_image_ref(
    payload: LlmProviderModelPlaygroundRequest, upload_image_url: str | None
) -> str | None:
    if upload_image_url is not None:
        return upload_image_url
    if payload.image_url is None:
        return None
    return await _playground_image_url_data_url(payload.image_url)


async def _playground_audio_ref(
    payload: LlmProviderModelPlaygroundRequest,
    upload_audio_ref: PlaygroundAudioRef | None,
) -> PlaygroundAudioRef | None:
    if upload_audio_ref is not None:
        return upload_audio_ref
    if payload.audio_url is None:
        return None
    return await _playground_audio_url_ref(payload.audio_url)


def _validate_playground_payload(raw: object) -> LlmProviderModelPlaygroundRequest:
    if not isinstance(raw, dict):
        raise _unprocessable("playground_payload_invalid")
    try:
        return LlmProviderModelPlaygroundRequest.model_validate(raw)
    except PydanticValidationError as exc:
        raise _unprocessable("playground_payload_invalid") from exc


async def _playground_request_payload(
    request: Request,
) -> tuple[LlmProviderModelPlaygroundRequest, str | None, PlaygroundAudioRef | None]:
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        try:
            raw = await request.json()
        except ValueError as exc:
            raise _unprocessable("playground_payload_invalid") from exc
        return _validate_playground_payload(raw), None, None

    form = await request.form()
    form_raw: dict[str, object] = {
        "mode": _playground_form_text(form, "mode") or "direct",
        "prompt": _playground_form_text(form, "prompt") or "",
        "system_prompt": _playground_form_text(form, "system_prompt"),
        "max_tokens": _playground_form_text(form, "max_tokens"),
        "temperature": _playground_form_text(form, "temperature"),
        "image_url": _playground_form_text(form, "image_url"),
        "audio_url": _playground_form_text(form, "audio_url"),
        "assignment_id": _playground_form_text(form, "assignment_id"),
        "thinking_level": _playground_form_text(form, "thinking_level"),
        "thinking_strategy": _playground_form_text(form, "thinking_strategy"),
    }
    payload = _validate_playground_payload(form_raw)
    image_upload = form.get("image_file")
    audio_upload = form.get("audio_file")
    if image_upload is not None and not isinstance(image_upload, FormUploadFile):
        if isinstance(audio_upload, FormUploadFile):
            await audio_upload.close()
        raise _unprocessable("playground_image_upload_invalid")
    if audio_upload is not None and not isinstance(audio_upload, FormUploadFile):
        if isinstance(image_upload, FormUploadFile):
            await image_upload.close()
        raise _unprocessable("playground_audio_upload_invalid")
    if image_upload is not None and payload.image_url is not None:
        await image_upload.close()
        if isinstance(audio_upload, FormUploadFile):
            await audio_upload.close()
        raise _unprocessable("playground_image_multiple_sources")
    if audio_upload is not None and payload.audio_url is not None:
        if isinstance(image_upload, FormUploadFile):
            await image_upload.close()
        await audio_upload.close()
        raise _unprocessable("playground_audio_multiple_sources")
    try:
        image_url = (
            await _playground_upload_data_url(image_upload)
            if image_upload is not None
            else None
        )
        audio_ref = (
            await _playground_upload_audio_ref(audio_upload)
            if audio_upload is not None
            else None
        )
    except Exception:
        if isinstance(audio_upload, FormUploadFile):
            await audio_upload.close()
        raise
    return payload, image_url, audio_ref


class LlmProviderModelPlaygroundResponse(BaseModel):
    status: Literal["ok", "error"]
    assistant_text: str | None = None
    reasoning_text: str | None = None
    model_used: str
    provider_used: str
    provider_model_id: str
    assignment_id: str | None = None
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    finish_reason: str | None = None
    stop_reason: str | None = None
    cost_usd: Decimal | None = None
    cost_cents: int | None = None
    error_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class LlmProviderModelEmbeddingSmokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="crew.day local embedding smoke test", max_length=16_000)

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("text must not be blank")
        return stripped


class LlmProviderModelEmbeddingSmokeResponse(BaseModel):
    status: Literal["ok", "error"]
    model_used: str
    provider_used: str
    provider_model_id: str
    latency_ms: int
    embedding_dimensions: int | None = None
    vector_norm: float | None = None
    error_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class LlmProviderKeyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=1, max_length=8192)

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("api_key must not be blank")
        return value


class ProviderPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    provider_type: LlmProviderType
    api_endpoint: str | None = Field(default=None, max_length=2048)
    default_model: str | None = None
    timeout_s: int = Field(default=60, ge=1, le=600)
    requests_per_minute: int = Field(default=60, ge=1, le=100_000)
    is_enabled: bool = True

    @model_validator(mode="after")
    def _validate_endpoint(self) -> Self:
        if (
            self.provider_type in {"openai_compatible", "ollama"}
            and not self.api_endpoint
        ):
            raise ValueError(f"{self.provider_type} providers require api_endpoint")
        return self


class ModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(min_length=1, max_length=240)
    display_name: str = Field(min_length=1, max_length=240)
    capabilities: list[str] = Field(default_factory=list)
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    embedding_dimensions: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    thinking_level: LlmThinkingLevel = "disabled"
    thinking_strategy: LlmThinkingStrategy = "none"
    price_source: Literal["openrouter", "manual", ""] = ""
    price_source_model_id: str | None = Field(default=None, max_length=240)
    is_active: bool = True
    notes: str | None = None

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for tag in value:
            if tag not in _MODEL_CAPABILITY_TAGS:
                raise ValueError(f"unknown model capability tag: {tag}")
            if tag in seen:
                raise ValueError(f"duplicate model capability tag: {tag}")
            seen.add(tag)
        return value


class ProviderModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str
    api_model_id: str = Field(min_length=1, max_length=240)
    input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)
    fixed_cost_per_call_usd: float | None = Field(default=None, ge=0)
    audio_cost_per_hour_usd: float | None = Field(default=None, ge=0)
    audio_input_transform: LlmAudioInputTransform = "passthrough"
    image_input_format: LlmImageInputFormat = "preserve"
    image_input_max_edge_px: int | None = Field(default=None, ge=1)
    max_tokens_override: int | None = Field(default=None, ge=1)
    supports_system_prompt: bool = True
    supports_temperature: bool = True
    thinking_strategy_override: LlmThinkingStrategy | None = None
    extra_api_params: dict[str, Any] = Field(default_factory=dict)
    price_source_override: Literal["", "none", "openrouter"] = ""
    price_source_model_id_override: str | None = None
    is_enabled: bool = True

    @field_validator("thinking_strategy_override", mode="before")
    @classmethod
    def _blank_strategy_override_inherits(cls, value: object) -> object:
        if value == "":
            return None
        return value


class OpenRouterModelPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id_or_url: str


class OpenRouterProviderModelPreview(BaseModel):
    provider_id: str
    provider_name: str
    existing_provider_model_id: str | None
    payload: ProviderModelPayload


class OpenRouterModelPreviewResponse(BaseModel):
    openrouter_model_id: str
    existing_model_id: str | None
    model_payload: ModelPayload
    provider_model_previews: list[OpenRouterProviderModelPreview]


class AssignmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str
    provider_model_id: str
    priority: int = Field(default=0, ge=0)
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    thinking_level_override: LlmThinkingLevel | None = None
    extra_api_params: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] | None = None
    is_enabled: bool = True


class AssignmentUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: int | None = Field(default=None, ge=0)
    provider_model_id: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    thinking_level_override: LlmThinkingLevel | None = None
    extra_api_params: dict[str, Any] | None = None
    required_capabilities: list[str] | None = None
    is_enabled: bool | None = None


class AssignmentReorderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str
    ids_in_priority_order: list[str]


class CapabilityInheritancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str
    inherits_from: str
    clear_direct_assignments: bool = False


class CapabilityInheritanceUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inherits_from: str


class PromptUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1)
    notes: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _money(value: Decimal | int | float | None) -> float:
    if value is None:
        return 0.0
    return float(value)


def _money_decimal(value: float | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _spend_usd(value: Decimal) -> float:
    return round(float(value), 6)


def _hash_body(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _current_prompt_default(capability: str) -> str:
    if capability == "chat.manager":
        return _default_system_prompt("manager")
    if capability == "chat.employee":
        return _default_system_prompt("employee")
    if capability == "chat.admin":
        return _default_system_prompt("admin")
    if capability == _COMPACT_CAPABILITY:
        return _default_compaction_prompt()
    raise NotImplementedFeature(extra={"error": "prompt_default_unavailable"})


def _endpoint(provider: LlmProvider) -> str:
    if provider.api_endpoint:
        return provider.api_endpoint
    if provider.provider_type == "openrouter":
        return _OPENROUTER_ENDPOINT
    return ""


def _provider_response(
    provider: LlmProvider,
    provider_model_counts: Counter[str],
    *,
    usage: dict[str, tuple[int, Decimal]] | None = None,
) -> LlmProviderResponse:
    calls, spend = (usage or {}).get(provider.id, (0, Decimal("0.000000")))
    api_key_status: Literal["present", "missing", "rotating"] = (
        "present" if provider.api_key_envelope_ref else "missing"
    )
    return LlmProviderResponse(
        id=provider.id,
        name=provider.name,
        provider_type=provider.provider_type,
        endpoint=_endpoint(provider),
        api_key_ref=provider.api_key_envelope_ref,
        api_key_status=api_key_status,
        default_model=provider.default_model,
        requests_per_minute=provider.requests_per_minute,
        timeout_s=provider.timeout_s,
        is_enabled=provider.is_enabled,
        provider_model_count=provider_model_counts[provider.id],
        spend_usd_30d=_spend_usd(spend),
        calls_30d=calls,
    )


def _model_response(
    model: LlmModel,
    provider_model_counts: Counter[str],
    *,
    usage: dict[str, tuple[int, Decimal]] | None = None,
) -> LlmModelResponse:
    calls, spend = (usage or {}).get(model.id, (0, Decimal("0.000000")))
    return LlmModelResponse(
        id=model.id,
        canonical_name=model.canonical_name,
        display_name=model.display_name,
        capabilities=list(model.capabilities or []),
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        embedding_dimensions=model.embedding_dimensions,
        temperature=model.temperature,
        thinking_level=_thinking_level(model.thinking_level),
        thinking_strategy=_thinking_strategy(model.thinking_strategy),
        price_source=model.price_source,
        price_source_model_id=model.price_source_model_id,
        is_active=model.is_active,
        notes=model.notes,
        provider_model_count=provider_model_counts[model.id],
        spend_usd_30d=_spend_usd(spend),
        calls_30d=calls,
    )


def _provider_model_response(
    row: LlmProviderModel,
    model: LlmModel | None = None,
    *,
    usage: dict[str, tuple[int, Decimal]] | None = None,
) -> LlmProviderModelResponse:
    calls, spend = (usage or {}).get(row.id, (0, Decimal("0.000000")))
    effective_thinking_strategy = _thinking_strategy(
        row.thinking_strategy_override
        if row.thinking_strategy_override
        else (model.thinking_strategy if model is not None else None)
    )
    return LlmProviderModelResponse(
        id=row.id,
        provider_id=row.provider_id,
        model_id=row.model_id,
        api_model_id=row.api_model_id,
        input_cost_per_million=_money(row.input_cost_per_million),
        output_cost_per_million=_money(row.output_cost_per_million),
        fixed_cost_per_call_usd=_money(row.fixed_cost_per_call_usd),
        audio_cost_per_hour_usd=_money(row.audio_cost_per_hour_usd),
        audio_input_transform=row.audio_input_transform,
        image_input_format=row.image_input_format,
        image_input_max_edge_px=row.image_input_max_edge_px,
        max_tokens_override=row.max_tokens_override,
        supports_system_prompt=row.supports_system_prompt,
        supports_temperature=row.supports_temperature,
        thinking_strategy_override=(
            _thinking_strategy(row.thinking_strategy_override)
            if row.thinking_strategy_override
            else None
        ),
        effective_thinking_strategy=effective_thinking_strategy,
        extra_api_params=dict(row.extra_api_params or {}),
        price_source_override=row.price_source_override or "",
        price_source_model_id_override=row.price_source_model_id_override,
        price_last_synced_at=_iso(row.price_last_synced_at),
        is_enabled=row.is_enabled,
        spend_usd_30d=_spend_usd(spend),
        calls_30d=calls,
    )


def _capabilities(
    *,
    direct_usage: dict[str, tuple[int, Decimal]] | None = None,
    inherited_usage: dict[str, tuple[int, Decimal]] | None = None,
) -> list[LlmCapabilityEntry]:
    direct_usage = direct_usage or {}
    inherited_usage = inherited_usage or {}
    return [
        _capability_response(
            key=key,
            description=description,
            required=required,
            direct_usage=direct_usage,
            inherited_usage=inherited_usage,
        )
        for key, description, required in _CAPABILITIES
    ]


def _capability_response(
    *,
    key: str,
    description: str,
    required: tuple[str, ...],
    direct_usage: dict[str, tuple[int, Decimal]],
    inherited_usage: dict[str, tuple[int, Decimal]],
) -> LlmCapabilityEntry:
    direct_calls, direct_spend = direct_usage.get(key, (0, Decimal("0.000000")))
    inherited_calls, inherited_spend = inherited_usage.get(
        key, (0, Decimal("0.000000"))
    )
    return LlmCapabilityEntry(
        key=key,
        description=description,
        required_capabilities=list(required),
        spend_usd_30d=_spend_usd(direct_spend + inherited_spend),
        calls_30d=direct_calls + inherited_calls,
        direct_spend_usd_30d=_spend_usd(direct_spend),
        direct_calls_30d=direct_calls,
        inherited_spend_usd_30d=_spend_usd(inherited_spend),
        inherited_calls_30d=inherited_calls,
    )


def _assignment_response(
    row: LlmAssignment,
    *,
    provider_models_by_id: dict[str, LlmProviderModel] | None = None,
    models_by_id: dict[str, LlmModel] | None = None,
    direct_usage: dict[str, tuple[int, Decimal]] | None = None,
    inherited_usage: dict[str, tuple[int, Decimal]] | None = None,
    usage: dict[str, tuple[int, Decimal]] | None = None,
) -> LlmAssignmentResponse:
    if usage is not None:
        direct_usage = usage
    direct_usage = direct_usage or {}
    inherited_usage = inherited_usage or {}
    direct_calls, direct_spend = direct_usage.get(row.id, (0, Decimal("0.000000")))
    inherited_calls, inherited_spend = inherited_usage.get(
        row.id, (0, Decimal("0.000000"))
    )
    provider_model = (provider_models_by_id or {}).get(row.model_id)
    model = (
        (models_by_id or {}).get(provider_model.model_id)
        if provider_model is not None
        else None
    )
    effective_thinking_level = _thinking_level(
        row.thinking_level_override
        if row.thinking_level_override is not None
        else (model.thinking_level if model is not None else None)
    )
    effective_thinking_strategy = _thinking_strategy(
        provider_model.thinking_strategy_override
        if provider_model is not None and provider_model.thinking_strategy_override
        else (model.thinking_strategy if model is not None else None)
    )
    return LlmAssignmentResponse(
        id=row.id,
        capability=row.capability,
        description=_CAPABILITY_DESCRIPTIONS.get(row.capability, row.capability),
        priority=row.priority,
        provider_model_id=row.model_id,
        max_tokens=row.max_tokens,
        temperature=row.temperature,
        thinking_level_override=(
            _thinking_level(row.thinking_level_override)
            if row.thinking_level_override is not None
            else None
        ),
        effective_thinking_level=effective_thinking_level,
        effective_thinking_strategy=effective_thinking_strategy,
        extra_api_params=dict(row.extra_api_params or {}),
        required_capabilities=list(row.required_capabilities or []),
        is_enabled=row.enabled,
        last_used_at=None,
        spend_usd_30d=_spend_usd(direct_spend + inherited_spend),
        calls_30d=direct_calls + inherited_calls,
        direct_spend_usd_30d=_spend_usd(direct_spend),
        direct_calls_30d=direct_calls,
        inherited_spend_usd_30d=_spend_usd(inherited_spend),
        inherited_calls_30d=inherited_calls,
    )


def _assignment_response_context(
    session: Session, rows: list[LlmAssignment]
) -> tuple[dict[str, LlmProviderModel], dict[str, LlmModel]]:
    provider_model_ids = {row.model_id for row in rows}
    if not provider_model_ids:
        return {}, {}
    provider_models = list(
        session.scalars(
            select(LlmProviderModel).where(LlmProviderModel.id.in_(provider_model_ids))
        ).all()
    )
    model_ids = {row.model_id for row in provider_models}
    models = (
        list(session.scalars(select(LlmModel).where(LlmModel.id.in_(model_ids))).all())
        if model_ids
        else []
    )
    return {row.id: row for row in provider_models}, {row.id: row for row in models}


def _prompt_response(
    row: LlmPromptTemplate,
    revisions_count: int,
) -> LlmPromptTemplateResponse:
    preview = row.template.strip().replace("\n", " ")
    return LlmPromptTemplateResponse(
        id=row.id,
        capability=row.capability,
        name=row.name,
        version=row.version,
        is_active=row.is_active,
        is_customised=_hash_body(row.template) != row.default_hash,
        default_hash=row.default_hash,
        updated_at=_iso(row.updated_at) or "",
        revisions_count=revisions_count,
        preview=preview[:160],
    )


def _add_usage(
    rollups: dict[str, tuple[int, Decimal]], key: str, spend: Decimal
) -> None:
    calls, total_spend = rollups.get(key, (0, Decimal("0.000000")))
    rollups[key] = (calls + 1, total_spend + spend)


def _assignment_usage(
    session: Session, cutoff: datetime
) -> dict[str, tuple[int, Decimal]]:
    rows = session.execute(
        select(
            LlmUsage.assignment_id,
            func.count(LlmUsage.id),
            func.coalesce(func.sum(LlmUsage.cost_usd), Decimal("0.000000")),
        )
        .where(LlmUsage.created_at >= cutoff, LlmUsage.assignment_id.is_not(None))
        .group_by(LlmUsage.assignment_id)
    ).all()
    return {
        str(assignment_id): (int(count or 0), spend or Decimal("0.000000"))
        for assignment_id, count, spend in rows
        if assignment_id is not None
    }


def _recent_usage(session: Session, cutoff: datetime) -> list[LlmUsage]:
    return list(
        session.scalars(
            select(LlmUsage)
            .where(LlmUsage.created_at >= cutoff)
            .order_by(LlmUsage.created_at.desc(), LlmUsage.id.desc())
        ).all()
    )


def _capability_inherits_from(
    capability: str,
    ancestor: str,
    inheritance: dict[str, str],
) -> bool:
    seen: set[str] = set()
    current = capability
    for _ in range(16):
        parent = inheritance.get(current)
        if parent is None or parent in seen:
            return False
        if parent == ancestor:
            return True
        seen.add(parent)
        current = parent
    return False


def _ancestor_capabilities(capability: str, inheritance: dict[str, str]) -> list[str]:
    ancestors: list[str] = []
    seen: set[str] = set()
    current = capability
    for _ in range(16):
        parent = inheritance.get(current)
        if parent is None or parent in seen:
            return ancestors
        ancestors.append(parent)
        seen.add(parent)
        current = parent
    return ancestors


def _capability_has_chain(
    capability: str,
    *,
    assigned_capabilities: set[str],
    inheritance: dict[str, str],
) -> bool:
    seen: set[str] = set()
    current = capability
    for _ in range(16):
        if current in assigned_capabilities:
            return True
        if current in seen:
            return False
        seen.add(current)
        parent = inheritance.get(current)
        if parent is None:
            return False
        current = parent
    return False


def _missing_model_capabilities(
    *,
    assignment: LlmAssignmentResponse,
    provider_models_by_id: dict[str, LlmProviderModel],
    models_by_id: dict[str, LlmModel],
    required_capabilities: list[str],
) -> list[str]:
    provider_model = provider_models_by_id.get(assignment.provider_model_id)
    model = models_by_id.get(provider_model.model_id) if provider_model else None
    model_caps = set(model.capabilities if model else [])
    return [cap for cap in required_capabilities if cap not in model_caps]


def _load_graph(session: Session) -> LlmGraphPayload:
    # code-health: ignore[ccn nloc] API fields are generated schema contract.  # noqa: E501
    cutoff = _now() - timedelta(days=30)
    # justification: deployment-admin LLM dashboard reads deployment-global config
    # plus cross-workspace llm_usage aggregation across all workspaces, by design.
    with tenant_agnostic():
        providers = list(
            session.scalars(
                select(LlmProvider).order_by(LlmProvider.name, LlmProvider.id)
            ).all()
        )
        models = list(
            session.scalars(
                select(LlmModel).order_by(LlmModel.display_name, LlmModel.id)
            ).all()
        )
        provider_models = list(
            session.scalars(
                select(LlmProviderModel).order_by(
                    LlmProviderModel.api_model_id, LlmProviderModel.id
                )
            ).all()
        )
        assignments = list(
            session.scalars(
                select(LlmAssignment)
                .where(LlmAssignment.workspace_id.is_(None))
                .order_by(
                    LlmAssignment.capability,
                    LlmAssignment.priority,
                    LlmAssignment.id,
                )
            ).all()
        )
        inheritance = list(
            session.scalars(
                select(LlmCapabilityInheritance)
                .where(LlmCapabilityInheritance.workspace_id.is_(None))
                .order_by(LlmCapabilityInheritance.capability)
            ).all()
        )
        usage_rows = _recent_usage(session, cutoff)

    provider_counts: Counter[str] = Counter(row.provider_id for row in provider_models)
    model_counts: Counter[str] = Counter(row.model_id for row in provider_models)
    provider_models_by_id = {row.id: row for row in provider_models}
    models_by_id = {row.id: row for row in models}
    inheritance_by_capability = {
        row.capability: row.inherits_from for row in inheritance
    }

    enabled_assignment_caps = {row.capability for row in assignments if row.enabled}
    capability_keys = [entry.key for entry in _capabilities()]
    explicit_inheritance = set(inheritance_by_capability)
    effective_inheritance = dict(inheritance_by_capability)
    for capability in capability_keys:
        if (
            capability != DEFAULT_LLM_CAPABILITY
            and capability not in effective_inheritance
            and capability not in enabled_assignment_caps
        ):
            effective_inheritance[capability] = DEFAULT_LLM_CAPABILITY

    provider_model_usage: dict[str, tuple[int, Decimal]] = {}
    provider_usage: dict[str, tuple[int, Decimal]] = {}
    model_usage: dict[str, tuple[int, Decimal]] = {}
    assignment_direct_usage: dict[str, tuple[int, Decimal]] = {}
    assignment_inherited_usage: dict[str, tuple[int, Decimal]] = {}
    capability_direct_usage: dict[str, tuple[int, Decimal]] = {}
    capability_inherited_usage: dict[str, tuple[int, Decimal]] = {}
    provider_model_ids = set(provider_models_by_id)
    provider_model_ids_by_api_model_id = {
        row.api_model_id: row.id for row in provider_models
    }
    assignments_by_id = {row.id: row for row in assignments}
    capability_key_set = set(capability_keys)
    for usage_row in usage_rows:
        spend = usage_row.cost_usd
        resolved_provider_model_id = _llm_usage_provider_model_id(
            usage_row,
            provider_model_ids=provider_model_ids,
            provider_model_ids_by_api_model_id=provider_model_ids_by_api_model_id,
        )
        if resolved_provider_model_id is None:
            continue
        provider_model = provider_models_by_id.get(resolved_provider_model_id)
        if provider_model is None:
            continue
        _add_usage(provider_model_usage, provider_model.id, spend)
        _add_usage(provider_usage, provider_model.provider_id, spend)
        _add_usage(model_usage, provider_model.model_id, spend)
        if usage_row.capability in capability_key_set:
            _add_usage(capability_direct_usage, usage_row.capability, spend)
        for ancestor in _ancestor_capabilities(
            usage_row.capability, effective_inheritance
        ):
            if ancestor in capability_key_set:
                _add_usage(capability_inherited_usage, ancestor, spend)
        if usage_row.assignment_id is None:
            continue
        assignment = assignments_by_id.get(usage_row.assignment_id)
        if assignment is None:
            continue
        if usage_row.capability == assignment.capability:
            _add_usage(assignment_direct_usage, assignment.id, spend)
        elif _capability_inherits_from(
            usage_row.capability,
            assignment.capability,
            effective_inheritance,
        ):
            _add_usage(assignment_inherited_usage, assignment.id, spend)

    assignment_responses = [
        _assignment_response(
            row,
            provider_models_by_id=provider_models_by_id,
            models_by_id=models_by_id,
            direct_usage=assignment_direct_usage,
            inherited_usage=assignment_inherited_usage,
        )
        for row in assignments
    ]
    assignment_responses.sort(key=lambda row: (row.capability, row.priority, row.id))
    issues: list[LlmAssignmentIssue] = []
    for assignment_response in assignment_responses:
        missing = _missing_model_capabilities(
            assignment=assignment_response,
            provider_models_by_id=provider_models_by_id,
            models_by_id=models_by_id,
            required_capabilities=assignment_response.required_capabilities,
        )
        if missing:
            issues.append(
                LlmAssignmentIssue(
                    assignment_id=assignment_response.id,
                    capability=assignment_response.capability,
                    missing_capabilities=missing,
                )
            )

    inherited_caps = {
        capability
        for capability in capability_keys
        if capability not in enabled_assignment_caps
        and capability in effective_inheritance
    }
    assignments_by_capability: dict[str, list[LlmAssignmentResponse]] = {}
    for assignment_response in assignment_responses:
        assignments_by_capability.setdefault(assignment_response.capability, []).append(
            assignment_response
        )
    for capability in inherited_caps:
        parent = effective_inheritance[capability]
        required = _CAPABILITY_REQUIRED.get(capability, [])
        for assignment_response in assignments_by_capability.get(parent, []):
            missing = _missing_model_capabilities(
                assignment=assignment_response,
                provider_models_by_id=provider_models_by_id,
                models_by_id=models_by_id,
                required_capabilities=required,
            )
            if missing:
                issues.append(
                    LlmAssignmentIssue(
                        assignment_id=assignment_response.id,
                        capability=capability,
                        missing_capabilities=missing,
                    )
                )
    total_calls = sum(calls for calls, _spend in provider_model_usage.values())
    total_spend = sum(
        (spend for _calls, spend in provider_model_usage.values()),
        Decimal("0.000000"),
    )
    return LlmGraphPayload(
        providers=[
            _provider_response(row, provider_counts, usage=provider_usage)
            for row in providers
        ],
        models=[
            _model_response(row, model_counts, usage=model_usage) for row in models
        ],
        provider_models=[
            _provider_model_response(
                row, models_by_id.get(row.model_id), usage=provider_model_usage
            )
            for row in provider_models
        ],
        capabilities=_capabilities(
            direct_usage=capability_direct_usage,
            inherited_usage=capability_inherited_usage,
        ),
        inheritance=[
            LlmCapabilityInheritanceResponse(
                capability=capability,
                inherits_from=inherits_from,
                source="explicit"
                if capability in explicit_inheritance
                else "implicit_default",
            )
            for capability, inherits_from in sorted(effective_inheritance.items())
        ],
        assignments=assignment_responses,
        assignment_issues=issues,
        totals=LlmGraphTotals(
            spend_usd_30d=_spend_usd(total_spend),
            calls_30d=total_calls,
            provider_count=len(providers),
            model_count=len(models),
            capability_count=len(capability_keys),
            unassigned_capabilities=[
                capability
                for capability in capability_keys
                if not _capability_has_chain(
                    capability,
                    assigned_capabilities=enabled_assignment_caps,
                    inheritance=effective_inheritance,
                )
            ],
        ),
    )


def _not_found() -> NotFound:
    return NotFound(extra={"error": "not_found"})


def _conflict(error: str) -> Conflict:
    return Conflict(extra={"error": error})


def _direct_assignments_for_capability(
    session: Session, capability: str
) -> list[LlmAssignment]:
    return list(
        session.scalars(
            select(LlmAssignment)
            .where(
                LlmAssignment.workspace_id.is_(None),
                LlmAssignment.capability == capability,
            )
            .order_by(LlmAssignment.priority, LlmAssignment.id)
        ).all()
    )


def _unprocessable(
    error: str, *, message: str | None = None, **extra: object
) -> Validation:
    return Validation(detail=message, extra={"error": error, **extra})


def _commit_or_conflict(session: Session, error: str) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise _conflict(error) from exc


def _flush_or_conflict(session: Session, error: str) -> None:
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _conflict(error) from exc


def _openrouter_unavailable() -> UpstreamUnavailable:
    return UpstreamUnavailable(
        "OpenRouter metadata is temporarily unavailable",
        extra={"error": "openrouter_unavailable", "upstream": "openrouter"},
    )


def _publish_assignment_changed(
    ctx: DeploymentContext,
    request: Request,
) -> None:
    default_event_bus.publish(
        LlmAssignmentChanged(
            workspace_id=DEPLOYMENT_DEFAULT_CACHE_WORKSPACE_ID,
            actor_id=ctx.user_id,
            correlation_id=new_ulid(),
            occurred_at=_now(),
        )
    )
    admin_sse.publish_admin_event(
        kind="admin.llm.assignment_updated",
        ctx=ctx,
        request=request,
        payload={"workspace_id": None},
    )


def _publish_deployment_defaults_changed(
    ctx: DeploymentContext,
    request: Request,
) -> None:
    default_event_bus.publish(
        LlmAssignmentChanged(
            workspace_id=DEPLOYMENT_DEFAULT_CACHE_WORKSPACE_ID,
            actor_id=ctx.user_id,
            correlation_id=new_ulid(),
            occurred_at=_now(),
        )
    )
    admin_sse.publish_admin_event(
        kind="admin.llm.assignment_updated",
        ctx=ctx,
        request=request,
        payload={"workspace_id": None},
    )


def _provider_exists(session: Session, provider_id: str) -> bool:
    return session.get(LlmProvider, provider_id) is not None


def _model_exists(session: Session, model_id: str) -> bool:
    return session.get(LlmModel, model_id) is not None


def _provider_model(session: Session, provider_model_id: str) -> LlmProviderModel:
    row = session.get(LlmProviderModel, provider_model_id)
    if row is None:
        raise _not_found()
    return row


def _missing_capabilities(
    session: Session,
    *,
    provider_model_id: str,
    required_capabilities: list[str],
) -> list[str]:
    provider_model = _provider_model(session, provider_model_id)
    model = session.get(LlmModel, provider_model.model_id)
    model_capabilities = set(model.capabilities if model is not None else [])
    return [cap for cap in required_capabilities if cap not in model_capabilities]


def _raise_missing_capabilities(missing: list[str]) -> None:
    if missing:
        raise Validation(
            extra={
                "error": "assignment_missing_capability",
                "missing_capabilities": missing,
            },
        )


def _catalog_required_capabilities(capability: str) -> list[str]:
    required = _CAPABILITY_REQUIRED.get(capability)
    if required is None:
        raise _unprocessable("unknown_capability", capability=capability)
    return list(required)


def _validate_required_capabilities(
    capability: str, provided: list[str] | None
) -> list[str]:
    required = _catalog_required_capabilities(capability)
    if provided is not None and provided != required:
        raise _unprocessable(
            "required_capabilities_mismatch",
            capability=capability,
            required_capabilities=required,
        )
    return required


def _validate_provider_payload(
    session: Session,
    payload: ProviderPayload,
    *,
    provider_id: str | None = None,
) -> None:
    duplicate = session.scalar(
        select(LlmProvider.id).where(LlmProvider.name == payload.name).limit(1)
    )
    if duplicate is not None and duplicate != provider_id:
        raise _conflict("provider_name_exists")
    if payload.default_model is None:
        return
    provider_model = session.get(LlmProviderModel, payload.default_model)
    if provider_model is None:
        raise _unprocessable("default_model_not_found")
    if provider_id is None or provider_model.provider_id != provider_id:
        raise _unprocessable("default_model_provider_mismatch")


def _settings_from_request(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    raise RuntimeError("app.state.settings is not configured")


def _envelope_id_from_pointer(pointer: bytes) -> str:
    if len(pointer) < 2 or pointer[0] != _ROW_BACKED_ENVELOPE_VERSION:
        raise RuntimeError("LLM provider key encryption did not return a row pointer")
    try:
        envelope_id = pointer[1:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("LLM provider key envelope pointer is not UTF-8") from exc
    if not envelope_id.strip():
        raise RuntimeError("LLM provider key envelope pointer id is blank")
    return envelope_id


def _encrypt_provider_api_key(
    session: Session,
    *,
    provider: LlmProvider,
    api_key: SecretStr,
    settings: Settings,
) -> str:
    if provider.provider_type in {"fake", "local_embedding"}:
        raise _unprocessable("provider_key_unsupported_provider_type")
    if settings.root_key is None:
        raise ServiceUnavailable(
            "CREWDAY_ROOT_KEY is required to store LLM provider API keys",
            extra={
                "error": "root_key_required",
                "upstream": "secret_envelope",
            },
        )
    envelope = Aes256GcmEnvelope(
        settings.root_key,
        repository=SqlAlchemySecretEnvelopeRepository(session),
    )
    pointer = envelope.encrypt(
        api_key.get_secret_value().encode("utf-8"),
        purpose=_LLM_PROVIDER_API_KEY_PURPOSE,
        owner=EnvelopeOwner(kind="llm_provider", id=provider.id),
    )
    return _envelope_id_from_pointer(pointer)


def _decrypt_provider_api_key(
    session: Session,
    *,
    provider: LlmProvider,
    settings: Settings,
) -> SecretStr | None:
    if provider.api_key_envelope_ref is None:
        return None
    if settings.root_key is None:
        raise ServiceUnavailable(
            "CREWDAY_ROOT_KEY is required to read LLM provider API keys",
            extra={
                "error": "root_key_required",
                "upstream": "secret_envelope",
            },
        )
    envelope = Aes256GcmEnvelope(
        settings.root_key,
        repository=SqlAlchemySecretEnvelopeRepository(session),
    )
    try:
        plaintext = envelope.decrypt(
            bytes((_ROW_BACKED_ENVELOPE_VERSION,))
            + provider.api_key_envelope_ref.encode("utf-8"),
            purpose=_LLM_PROVIDER_API_KEY_PURPOSE,
            expected_owner=EnvelopeOwner(kind="llm_provider", id=provider.id),
        )
    except EnvelopeDecryptError as exc:
        raise _unprocessable("provider_client_unavailable") from exc
    try:
        decoded = plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _unprocessable("provider_client_unavailable") from exc
    if not decoded.strip():
        raise _unprocessable("provider_client_unavailable")
    return SecretStr(decoded)


def _validate_model_payload(
    session: Session,
    payload: ModelPayload,
    *,
    model_id: str | None = None,
) -> None:
    duplicate = session.scalar(
        select(LlmModel.id)
        .where(LlmModel.canonical_name == payload.canonical_name)
        .limit(1)
    )
    if duplicate is not None and duplicate != model_id:
        raise _conflict("model_canonical_name_exists")


def _validate_provider_model_payload(
    session: Session,
    payload: ProviderModelPayload,
    *,
    provider_model_id: str | None = None,
) -> None:
    provider = session.get(LlmProvider, payload.provider_id)
    if provider is None:
        raise _unprocessable("provider_not_found")
    model = session.get(LlmModel, payload.model_id)
    if model is None:
        raise _unprocessable("model_not_found")
    if provider.provider_type == "local_embedding" and "embeddings" not in set(
        model.capabilities or []
    ):
        raise _unprocessable("local_embedding_model_requires_embeddings")
    duplicate = session.scalar(
        select(LlmProviderModel.id)
        .where(
            LlmProviderModel.provider_id == payload.provider_id,
            LlmProviderModel.model_id == payload.model_id,
        )
        .limit(1)
    )
    if duplicate is not None and duplicate != provider_model_id:
        raise _conflict("provider_model_exists")


def _provider_model_price_lookup(
    row: LlmProviderModel, model: LlmModel
) -> tuple[Literal["openrouter"], str] | None:
    override = row.price_source_override or ""
    if override == "none":
        return None
    if override != "openrouter" and model.price_source != "openrouter":
        return None
    lookup_id = (
        row.price_source_model_id_override
        or model.price_source_model_id
        or (row.api_model_id if override == "openrouter" else model.canonical_name)
    )
    return "openrouter", lookup_id


def _sync_pricing_error(exc: Exception) -> None:
    if isinstance(exc, ValueError):
        raise _unprocessable("invalid_openrouter_model_id") from exc
    if isinstance(exc, LlmProviderError):
        raise NotFound(extra={"error": "openrouter_model_not_found"}) from exc
    if isinstance(exc, (LlmRateLimited, LlmTransportError)):
        raise _openrouter_unavailable() from exc
    raise exc


def _fetch_openrouter_pricing(lookup_id: str) -> OpenRouterModelMetadata:
    try:
        return fetch_openrouter_model_metadata(lookup_id)
    except (ValueError, LlmProviderError, LlmRateLimited, LlmTransportError) as exc:
        _sync_pricing_error(exc)
        raise AssertionError("unreachable") from exc


def _apply_provider_model_pricing(
    row: LlmProviderModel,
    *,
    source: Literal["openrouter"],
    lookup_id: str,
    metadata: OpenRouterModelMetadata,
    now: datetime,
    dry_run: bool = False,
) -> LlmSyncPricingDelta:
    input_before = _money(row.input_cost_per_million)
    output_before = _money(row.output_cost_per_million)
    fixed_before = _money(row.fixed_cost_per_call_usd)
    audio_before = _money(row.audio_cost_per_hour_usd)
    input_after = _money(metadata.input_cost_per_million)
    output_after = _money(metadata.output_cost_per_million)
    fixed_after = _money(metadata.fixed_cost_per_call_usd)
    audio_after = _money(metadata.audio_cost_per_hour_usd)
    changed = (
        input_before != input_after
        or output_before != output_after
        or fixed_before != fixed_after
        or audio_before != audio_after
    )
    if not dry_run:
        row.input_cost_per_million = metadata.input_cost_per_million
        row.output_cost_per_million = metadata.output_cost_per_million
        row.fixed_cost_per_call_usd = metadata.fixed_cost_per_call_usd or Decimal("0")
        row.audio_cost_per_hour_usd = metadata.audio_cost_per_hour_usd
        row.price_last_synced_at = now
    return LlmSyncPricingDelta(
        provider_model_id=row.id,
        api_model_id=row.api_model_id,
        source=source,
        lookup_id=lookup_id,
        input_before=input_before,
        input_after=input_after,
        output_before=output_before,
        output_after=output_after,
        fixed_before=fixed_before,
        fixed_after=fixed_after,
        audio_before=audio_before,
        audio_after=audio_after,
        price_last_synced_at=_iso(now),
        status="updated" if changed else "unchanged",
    )


def _sync_provider_model_pricing(
    row: LlmProviderModel,
    model: LlmModel,
    *,
    now: datetime,
    dry_run: bool = False,
) -> LlmSyncPricingDelta:
    lookup = _provider_model_price_lookup(row, model)
    if lookup is None:
        return LlmSyncPricingDelta(
            provider_model_id=row.id,
            api_model_id=row.api_model_id,
            input_before=_money(row.input_cost_per_million),
            input_after=_money(row.input_cost_per_million),
            output_before=_money(row.output_cost_per_million),
            output_after=_money(row.output_cost_per_million),
            fixed_before=_money(row.fixed_cost_per_call_usd),
            fixed_after=_money(row.fixed_cost_per_call_usd),
            audio_before=_money(row.audio_cost_per_hour_usd),
            audio_after=_money(row.audio_cost_per_hour_usd),
            status="skipped_not_syncable",
        )
    source, lookup_id = lookup
    metadata = _fetch_openrouter_pricing(lookup_id)
    return _apply_provider_model_pricing(
        row,
        source=source,
        lookup_id=lookup_id,
        metadata=metadata,
        now=now,
        dry_run=dry_run,
    )


def _sync_pricing_error_delta(
    row: LlmProviderModel,
    model: LlmModel,
) -> LlmSyncPricingDelta:
    lookup = _provider_model_price_lookup(row, model)
    return LlmSyncPricingDelta(
        provider_model_id=row.id,
        api_model_id=row.api_model_id,
        source=lookup[0] if lookup is not None else None,
        lookup_id=lookup[1] if lookup is not None else None,
        input_before=_money(row.input_cost_per_million),
        input_after=_money(row.input_cost_per_million),
        output_before=_money(row.output_cost_per_million),
        output_after=_money(row.output_cost_per_million),
        fixed_before=_money(row.fixed_cost_per_call_usd),
        fixed_after=_money(row.fixed_cost_per_call_usd),
        audio_before=_money(row.audio_cost_per_hour_usd),
        audio_after=_money(row.audio_cost_per_hour_usd),
        status="error",
    )


def _sync_matching_provider_model_prices_from_metadata(
    session: Session, metadata: OpenRouterModelMetadata
) -> list[LlmSyncPricingDelta]:
    rows = list(
        session.scalars(
            select(LlmProviderModel).order_by(
                LlmProviderModel.api_model_id, LlmProviderModel.id
            )
        ).all()
    )
    if not rows:
        return []
    models_by_id = {
        model.id: model
        for model in session.scalars(
            select(LlmModel).where(LlmModel.id.in_({row.model_id for row in rows}))
        ).all()
    }
    now = _now()
    deltas: list[LlmSyncPricingDelta] = []
    for row in rows:
        model = models_by_id.get(row.model_id)
        if model is None:
            continue
        lookup = _provider_model_price_lookup(row, model)
        if lookup is None:
            continue
        source, lookup_id = lookup
        try:
            normalized_lookup_id = normalize_openrouter_model_id(lookup_id)
        except ValueError:
            continue
        if normalized_lookup_id != metadata.model_id:
            continue
        deltas.append(
            _apply_provider_model_pricing(
                row,
                source=source,
                lookup_id=lookup_id,
                metadata=metadata,
                now=now,
            )
        )
    return deltas


def _openrouter_metadata_preview(
    session: Session, payload: OpenRouterModelPreviewRequest
) -> tuple[OpenRouterModelPreviewResponse, bool]:
    try:
        metadata = fetch_openrouter_model_metadata(payload.model_id_or_url)
    except ValueError as exc:
        raise _unprocessable("invalid_openrouter_model_id") from exc
    except LlmProviderError as exc:
        raise NotFound(extra={"error": "openrouter_model_not_found"}) from exc
    except (LlmRateLimited, LlmTransportError) as exc:
        raise _openrouter_unavailable() from exc

    existing_model_id = session.scalar(
        select(LlmModel.id)
        .where(
            or_(
                LlmModel.canonical_name == metadata.model_id,
                LlmModel.price_source_model_id == metadata.model_id,
            )
        )
        .limit(1)
    )
    providers = list(
        session.scalars(
            select(LlmProvider)
            .where(LlmProvider.provider_type == "openrouter")
            .order_by(LlmProvider.name, LlmProvider.id)
        ).all()
    )
    existing_provider_models = _existing_openrouter_provider_models(
        session,
        providers=providers,
        metadata=metadata,
        existing_model_id=existing_model_id,
    )
    try:
        model_payload = _openrouter_model_payload(metadata)
        provider_model_previews = [
            OpenRouterProviderModelPreview(
                provider_id=provider.id,
                provider_name=provider.name,
                existing_provider_model_id=existing_provider_models.get(provider.id),
                payload=_openrouter_provider_model_payload(
                    metadata,
                    provider_id=provider.id,
                    model_id=existing_model_id or "",
                ),
            )
            for provider in providers
        ]
    except PydanticValidationError as exc:
        raise UpstreamUnavailable(
            "OpenRouter metadata is temporarily unavailable",
            extra={"error": "openrouter_unavailable", "upstream": "openrouter"},
        ) from exc
    pricing_changed = bool(
        _sync_matching_provider_model_prices_from_metadata(session, metadata)
    )
    if pricing_changed:
        _commit_or_conflict(session, "provider_model_constraint_violation")

    return (
        OpenRouterModelPreviewResponse(
            openrouter_model_id=metadata.model_id,
            existing_model_id=existing_model_id,
            model_payload=model_payload,
            provider_model_previews=provider_model_previews,
        ),
        pricing_changed,
    )


def _existing_openrouter_provider_models(
    session: Session,
    *,
    providers: list[LlmProvider],
    metadata: OpenRouterModelMetadata,
    existing_model_id: str | None,
) -> dict[str, str]:
    if not providers:
        return {}
    provider_ids = [provider.id for provider in providers]
    model_match = LlmProviderModel.api_model_id == metadata.model_id
    if existing_model_id is not None:
        model_match = or_(model_match, LlmProviderModel.model_id == existing_model_id)
    rows = list(
        session.scalars(
            select(LlmProviderModel)
            .where(LlmProviderModel.provider_id.in_(provider_ids), model_match)
            .order_by(LlmProviderModel.provider_id, LlmProviderModel.id)
        ).all()
    )
    result: dict[str, str] = {}
    for row in rows:
        result.setdefault(row.provider_id, row.id)
    return result


def _openrouter_model_payload(metadata: OpenRouterModelMetadata) -> ModelPayload:
    return ModelPayload(
        canonical_name=metadata.model_id,
        display_name=metadata.display_name,
        capabilities=metadata.capabilities,
        context_window=metadata.context_window,
        max_output_tokens=metadata.max_output_tokens,
        embedding_dimensions=None,
        temperature=None,
        thinking_level=metadata.thinking_level,
        thinking_strategy=metadata.thinking_strategy,
        price_source="openrouter",
        price_source_model_id=metadata.model_id,
        is_active=True,
        notes=None,
    )


def _openrouter_provider_model_payload(
    metadata: OpenRouterModelMetadata, *, provider_id: str, model_id: str
) -> ProviderModelPayload:
    return ProviderModelPayload(
        provider_id=provider_id,
        model_id=model_id,
        api_model_id=metadata.model_id,
        input_cost_per_million=_money(metadata.input_cost_per_million),
        output_cost_per_million=_money(metadata.output_cost_per_million),
        fixed_cost_per_call_usd=_money(metadata.fixed_cost_per_call_usd),
        audio_cost_per_hour_usd=_money(metadata.audio_cost_per_hour_usd),
        max_tokens_override=None,
        supports_system_prompt=metadata.supports_system_prompt,
        supports_temperature=metadata.supports_temperature,
        thinking_strategy_override=None,
        extra_api_params={},
        price_source_override="openrouter",
        price_source_model_id_override=metadata.model_id,
        is_enabled=True,
    )


def _validate_assignment_priority(
    session: Session,
    *,
    capability: str,
    priority: int,
    assignment_id: str | None = None,
) -> None:
    duplicate = session.scalar(
        select(LlmAssignment.id)
        .where(
            LlmAssignment.workspace_id.is_(None),
            LlmAssignment.capability == capability,
            LlmAssignment.priority == priority,
        )
        .limit(1)
    )
    if duplicate is not None and duplicate != assignment_id:
        raise _conflict("assignment_priority_exists")


def _assignment(session: Session, assignment_id: str) -> LlmAssignment:
    row = session.get(LlmAssignment, assignment_id)
    if row is None or row.workspace_id is not None:
        raise _not_found()
    return row


def _capability_exists(capability: str) -> bool:
    return capability in _CAPABILITY_REQUIRED


def _explicit_inheritance(
    session: Session, capability: str
) -> LlmCapabilityInheritance:
    row = session.scalar(
        select(LlmCapabilityInheritance).where(
            LlmCapabilityInheritance.workspace_id.is_(None),
            LlmCapabilityInheritance.capability == capability,
        )
    )
    if row is None:
        raise _not_found()
    return row


def _validate_inheritance_edge(
    session: Session,
    *,
    capability: str,
    inherits_from: str,
) -> None:
    if not _capability_exists(capability):
        raise _unprocessable("unknown_capability", capability=capability)
    if not _capability_exists(inherits_from):
        raise _unprocessable("unknown_capability", capability=inherits_from)
    if capability == DEFAULT_LLM_CAPABILITY:
        raise _unprocessable(
            "default_capability_inheritance_forbidden", capability=capability
        )
    if capability == inherits_from:
        raise _unprocessable("capability_inheritance_self_loop", capability=capability)

    edges = {
        child: parent
        for child, parent in session.execute(
            select(
                LlmCapabilityInheritance.capability,
                LlmCapabilityInheritance.inherits_from,
            ).where(LlmCapabilityInheritance.workspace_id.is_(None))
        ).all()
    }
    edges[capability] = inherits_from
    seen: set[str] = set()
    current = capability
    while current in edges:
        if current in seen:
            raise _unprocessable(
                "capability_inheritance_cycle",
                capability=capability,
                inherits_from=inherits_from,
            )
        seen.add(current)
        current = edges[current]


def _inheritance_response(
    row: LlmCapabilityInheritance,
) -> LlmCapabilityInheritanceResponse:
    return LlmCapabilityInheritanceResponse(
        capability=row.capability,
        inherits_from=row.inherits_from,
        source="explicit",
    )


def _status(row: LlmUsage) -> Literal["ok", "error", "redacted_block"]:
    if row.status == "ok":
        return "ok"
    if row.status == "refused":
        return "redacted_block"
    return "error"


def _llm_usage_provider_model_id(
    row: LlmUsage,
    *,
    provider_model_ids: set[str],
    provider_model_ids_by_api_model_id: dict[str, str],
) -> str | None:
    if row.provider_model_id in provider_model_ids:
        return row.provider_model_id
    return provider_model_ids_by_api_model_id.get(row.provider_model_id)


def _load_playground_target(
    session: Session,
    provider_model_id: str,
) -> tuple[LlmProviderModel, LlmProvider, LlmModel]:
    provider_model = session.get(LlmProviderModel, provider_model_id)
    if provider_model is None:
        raise _not_found()
    provider = session.get(LlmProvider, provider_model.provider_id)
    if provider is None:
        raise _unprocessable("provider_not_found")
    model = session.get(LlmModel, provider_model.model_id)
    if model is None:
        raise _unprocessable("model_not_found")
    if not provider.is_enabled:
        raise _unprocessable("provider_disabled")
    if not provider_model.is_enabled:
        raise _unprocessable("provider_model_disabled")
    if not model.is_active:
        raise _unprocessable("model_inactive")
    return provider_model, provider, model


def _validate_playground_assignment(
    session: Session,
    *,
    payload: LlmProviderModelPlaygroundRequest,
    provider_model_id: str,
) -> LlmAssignment | None:
    if payload.mode == "direct":
        return None
    assert payload.assignment_id is not None
    assignment = session.get(LlmAssignment, payload.assignment_id)
    if assignment is None or assignment.workspace_id is not None:
        raise _unprocessable("assignment_not_found")
    if assignment.model_id != provider_model_id:
        raise _unprocessable("assignment_provider_model_mismatch")
    if not assignment.enabled:
        raise _unprocessable("assignment_disabled")
    return assignment


def _playground_effective_prompt(
    payload: LlmProviderModelPlaygroundRequest,
    *,
    capabilities: set[str],
    image_ref: str | None,
    audio_ref: PlaygroundAudioRef | None,
) -> str:
    if payload.prompt:
        return payload.prompt
    if "chat" not in capabilities:
        if image_ref is not None and "vision" in capabilities:
            return _PLAYGROUND_DEFAULT_VISION_PROMPT
        if audio_ref is not None and "audio_input" in capabilities:
            return _PLAYGROUND_DEFAULT_AUDIO_PROMPT
    raise _unprocessable("prompt_required")


def _playground_messages(
    payload: LlmProviderModelPlaygroundRequest,
    provider_model: LlmProviderModel,
    model: LlmModel,
    image_url: str | None = None,
    audio_ref: PlaygroundAudioRef | None = None,
) -> list[ChatMessage]:
    capabilities = set(model.capabilities or [])
    image_ref = image_url if image_url is not None else payload.image_url
    if "chat" not in capabilities and image_ref is None and audio_ref is None:
        if "vision" in capabilities:
            raise _unprocessable("playground_image_required")
        if "audio_input" in capabilities:
            raise _unprocessable("playground_audio_required")
        raise _unprocessable("playground_requires_chat_model")
    if payload.system_prompt is not None and not provider_model.supports_system_prompt:
        raise _unprocessable("system_prompt_not_supported")
    if image_ref is not None and "vision" not in capabilities:
        raise _unprocessable("image_requires_vision_model")
    if audio_ref is not None and "audio_input" not in capabilities:
        raise _unprocessable("audio_requires_audio_model")
    prompt = _playground_effective_prompt(
        payload,
        capabilities=capabilities,
        image_ref=image_ref,
        audio_ref=audio_ref,
    )
    messages: list[ChatMessage] = []
    if payload.system_prompt is not None:
        messages.append({"role": "system", "content": payload.system_prompt})
    if image_ref is None and audio_ref is None:
        messages.append({"role": "user", "content": prompt})
    else:
        content: list[ChatTextBlock | ChatImageUrlBlock | ChatInputAudioBlock] = [
            {"type": "text", "text": prompt}
        ]
        if image_ref is not None:
            content.append({"type": "image_url", "image_url": {"url": image_ref}})
        if audio_ref is not None:
            content.append({"type": "input_audio", "input_audio": audio_ref})
        messages.append(
            {
                "role": "user",
                "content": content,
            }
        )
    return messages


def _playground_uses_transcription_endpoint(
    model: LlmModel, audio_ref: PlaygroundAudioRef | None
) -> bool:
    capabilities = set(model.capabilities or [])
    return (
        audio_ref is not None
        and "audio_input" in capabilities
        and "chat" not in capabilities
    )


def _playground_max_tokens(
    payload: LlmProviderModelPlaygroundRequest,
    provider_model: LlmProviderModel,
    model: LlmModel,
    assignment: LlmAssignment | None,
) -> int:
    explicit = payload.max_tokens is not None
    value = payload.max_tokens
    if value is None and assignment is not None and assignment.max_tokens is not None:
        value = assignment.max_tokens
    if value is None and provider_model.max_tokens_override is not None:
        value = provider_model.max_tokens_override
    if value is None and model.max_output_tokens is not None:
        value = min(model.max_output_tokens, _PLAYGROUND_MAX_TOKENS_LIMIT)

    max_tokens = value if value is not None else 1024
    if max_tokens < 1:
        raise _unprocessable(
            "max_tokens_invalid",
            message="Max tokens must be at least 1.",
        )
    if max_tokens > _PLAYGROUND_MAX_TOKENS_LIMIT:
        source = "the submitted value" if explicit else "the selected defaults"
        raise _unprocessable(
            "max_tokens_exceeds_playground_limit",
            message=(
                f"Max tokens from {source} is {max_tokens}, which exceeds the "
                f"playground limit of {_PLAYGROUND_MAX_TOKENS_LIMIT}."
            ),
            max_tokens_limit=_PLAYGROUND_MAX_TOKENS_LIMIT,
        )
    if model.max_output_tokens is not None and max_tokens > model.max_output_tokens:
        raise _unprocessable(
            "max_tokens_exceeds_model_limit",
            message=(
                f"Max tokens is {max_tokens}, which exceeds this model's known "
                f"output-token limit of {model.max_output_tokens}."
            ),
            max_tokens_limit=model.max_output_tokens,
        )
    return max_tokens


def _playground_temperature(
    payload: LlmProviderModelPlaygroundRequest,
    provider_model: LlmProviderModel,
    model: LlmModel,
    assignment: LlmAssignment | None,
) -> float:
    if not provider_model.supports_temperature:
        if payload.temperature is not None:
            raise _unprocessable("temperature_not_supported")
        return 0.0
    value = (
        payload.temperature
        if payload.temperature is not None
        else (
            assignment.temperature
            if assignment is not None and assignment.temperature is not None
            else model.temperature
        )
    )
    return value if value is not None else 0.0


def _playground_llm(
    request: Request, session: Session, provider: LlmProvider
) -> LLMClient:
    if provider.provider_type == "fake":
        return FakeLLMClient()
    if provider.provider_type == "openai_compatible":
        if not provider.api_endpoint:
            raise _unprocessable("provider_endpoint_required")
        return OpenRouterClient(
            _decrypt_provider_api_key(
                session,
                provider=provider,
                settings=_settings_from_request(request),
            ),
            base_url=provider.api_endpoint,
            timeout=float(provider.timeout_s),
            api_key_required=False,
            provider_label=provider.name,
        )
    if provider.provider_type == "ollama":
        if not provider.api_endpoint:
            raise _unprocessable("provider_endpoint_required")
        return OllamaClient(
            _decrypt_provider_api_key(
                session,
                provider=provider,
                settings=_settings_from_request(request),
            ),
            base_url=provider.api_endpoint,
            timeout=float(provider.timeout_s),
            provider_label=provider.name,
        )
    if provider.provider_type != "openrouter":
        raise _unprocessable("provider_type_not_supported")
    llm = getattr(request.app.state, "llm", None)
    if llm is None:
        raise _unprocessable("provider_client_unavailable")
    configured = getattr(llm, "is_configured", None)
    try:
        is_configured = bool(configured()) if callable(configured) else True
    except RuntimeError as exc:
        raise _unprocessable("provider_client_unavailable") from exc
    if not is_configured:
        raise _unprocessable("provider_api_key_missing")
    return cast(LLMClient, llm)


def _playground_cost_usd(
    provider_model: LlmProviderModel,
    *,
    input_tokens: int,
    output_tokens: int,
    audio_seconds: float | None = None,
) -> Decimal:
    input_price = _decimal_or_zero(provider_model.input_cost_per_million)
    output_price = _decimal_or_zero(provider_model.output_cost_per_million)
    cost = (
        Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price
    ) / Decimal(1_000_000)
    cost += _decimal_or_zero(provider_model.fixed_cost_per_call_usd)
    if audio_seconds is not None:
        cost += (
            Decimal(str(audio_seconds))
            * _decimal_or_zero(provider_model.audio_cost_per_hour_usd)
            / Decimal("3600")
        )
    return cost.quantize(Decimal("0.000001"))


def _decimal_or_zero(value: Decimal | int | float | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _embedding_smoke_dimensions(model: LlmModel) -> int:
    if "embeddings" not in set(model.capabilities or []):
        raise _unprocessable("embedding_smoke_requires_embedding_model")
    if model.embedding_dimensions is None:
        raise _unprocessable("embedding_dimensions_unknown")
    return model.embedding_dimensions


def _embedding_smoke_client(
    provider: LlmProvider, model: LlmModel
) -> FastEmbedEmbeddingClient:
    if provider.provider_type != "local_embedding":
        raise _unprocessable("embedding_smoke_provider_type_not_supported")
    return FastEmbedEmbeddingClient(
        model_name=model.canonical_name,
        dimensions=_embedding_smoke_dimensions(model),
    )


def _safe_provider_error(exc: BaseException, *, provider_name: str) -> str:
    message = scrub_string(str(exc))
    if message.lower().startswith("openrouter "):
        label = scrub_string(provider_name.strip()) or "Provider"
        message = f"{label}{message[len('openrouter') :]}"
    return message[:500]


def build_admin_llm_router() -> APIRouter:
    # code-health: ignore[nloc] Router owns auth, deps, and route registration.  # noqa: E501
    router = APIRouter(prefix="/llm", tags=["admin", "llm"])

    @router.get(
        "/graph",
        response_model=LlmGraphPayload,
        operation_id="admin.llm.graph",
        openapi_extra=_llm_cli("graph-list", "Read the full LLM graph", mutates=False),
    )
    def graph(_ctx: _ReadCtx, session: _Db) -> LlmGraphPayload:
        return _load_graph(session)

    @router.get(
        "/providers",
        response_model=list[LlmProviderResponse],
        operation_id="admin.llm.providers.list",
        openapi_extra=_llm_cli("providers-list", "List LLM providers", mutates=False),
    )
    def list_providers(_ctx: _ReadCtx, session: _Db) -> list[LlmProviderResponse]:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            provider_models = list(session.scalars(select(LlmProviderModel)).all())
            providers = list(
                session.scalars(
                    select(LlmProvider).order_by(LlmProvider.name, LlmProvider.id)
                ).all()
            )
        counts: Counter[str] = Counter(row.provider_id for row in provider_models)
        return [_provider_response(row, counts) for row in providers]

    @router.post(
        "/providers",
        response_model=LlmProviderResponse,
        operation_id="admin.llm.providers.create",
        openapi_extra=_llm_cli("providers", "Create an LLM provider", mutates=True),
    )
    def create_provider(
        ctx: _WriteCtx, request: Request, session: _Db, payload: ProviderPayload
    ) -> LlmProviderResponse:
        now = _now()
        row = LlmProvider(
            id=new_ulid(),
            name=payload.name,
            provider_type=payload.provider_type,
            api_endpoint=payload.api_endpoint,
            api_key_envelope_ref=None,
            default_model=payload.default_model,
            timeout_s=payload.timeout_s,
            requests_per_minute=payload.requests_per_minute,
            is_enabled=payload.is_enabled,
            created_at=now,
            updated_at=now,
            updated_by_user_id=ctx.user_id,
        )
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider is deployment-global (no workspace_id column).
        with tenant_agnostic():
            _validate_provider_payload(session, payload)
            session.add(row)
            _commit_or_conflict(session, "provider_constraint_violation")
            _publish_deployment_defaults_changed(ctx, request)
            session.refresh(row)
        return _provider_response(row, Counter())

    @router.get(
        "/providers/{provider_id}",
        response_model=LlmProviderResponse,
        operation_id="admin.llm.providers.get",
        openapi_extra=_llm_cli("providers-show", "Show an LLM provider", mutates=False),
    )
    def get_provider(
        _ctx: _ReadCtx, session: _Db, provider_id: str
    ) -> LlmProviderResponse:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = session.get(LlmProvider, provider_id)
            if row is None:
                raise _not_found()
            count = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.provider_id == provider_id
                )
            )
        return _provider_response(row, Counter({provider_id: int(count or 0)}))

    @router.put(
        "/providers/{provider_id}",
        response_model=LlmProviderResponse,
        operation_id="admin.llm.providers.update",
        openapi_extra=_llm_cli(
            "providers-replace", "Update an LLM provider", mutates=True
        ),
    )
    def update_provider(
        ctx: _WriteCtx,
        request: Request,
        session: _Db,
        provider_id: str,
        payload: ProviderPayload,
    ) -> LlmProviderResponse:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = session.get(LlmProvider, provider_id)
            if row is None:
                raise _not_found()
            _validate_provider_payload(session, payload, provider_id=provider_id)
            row.name = payload.name
            row.provider_type = payload.provider_type
            row.api_endpoint = payload.api_endpoint
            row.default_model = payload.default_model
            row.timeout_s = payload.timeout_s
            row.requests_per_minute = payload.requests_per_minute
            row.is_enabled = payload.is_enabled
            row.updated_at = _now()
            row.updated_by_user_id = ctx.user_id
            _commit_or_conflict(session, "provider_constraint_violation")
            _publish_deployment_defaults_changed(ctx, request)
            session.refresh(row)
            count = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.provider_id == provider_id
                )
            )
        return _provider_response(row, Counter({provider_id: int(count or 0)}))

    @router.put(
        "/providers/{provider_id}/key",
        response_model=LlmProviderResponse,
        operation_id="admin.llm.providers.key.set",
        openapi_extra=_llm_secret_cli(
            "providers-key-replace", "Set an LLM provider API key"
        ),
    )
    def set_provider_key(
        ctx: _SessionWriteCtx,
        request: Request,
        session: _Db,
        provider_id: str,
        payload: LlmProviderKeyPayload,
    ) -> LlmProviderResponse:
        settings = _settings_from_request(request)
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = session.get(LlmProvider, provider_id)
            if row is None:
                raise _not_found()
            row.api_key_envelope_ref = _encrypt_provider_api_key(
                session,
                provider=row,
                api_key=payload.api_key,
                settings=settings,
            )
            row.updated_at = _now()
            row.updated_by_user_id = ctx.user_id
            _commit_or_conflict(session, "provider_key_constraint_violation")
            _publish_deployment_defaults_changed(ctx, request)
            session.refresh(row)
            count = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.provider_id == provider_id
                )
            )
        return _provider_response(row, Counter({provider_id: int(count or 0)}))

    @router.delete(
        "/providers/{provider_id}/key",
        response_model=LlmProviderResponse,
        operation_id="admin.llm.providers.key.clear",
        openapi_extra=_llm_secret_cli(
            "providers-key-delete", "Clear an LLM provider API key"
        ),
    )
    def clear_provider_key(
        ctx: _SessionWriteCtx,
        request: Request,
        session: _Db,
        provider_id: str,
    ) -> LlmProviderResponse:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = session.get(LlmProvider, provider_id)
            if row is None:
                raise _not_found()
            row.api_key_envelope_ref = None
            row.updated_at = _now()
            row.updated_by_user_id = ctx.user_id
            _commit_or_conflict(session, "provider_key_constraint_violation")
            _publish_deployment_defaults_changed(ctx, request)
            session.refresh(row)
            count = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.provider_id == provider_id
                )
            )
        return _provider_response(row, Counter({provider_id: int(count or 0)}))

    @router.delete(
        "/providers/{provider_id}",
        status_code=204,
        operation_id="admin.llm.providers.delete",
        openapi_extra=_llm_cli(
            "providers-delete", "Delete an LLM provider", mutates=True
        ),
    )
    def delete_provider(
        ctx: _WriteCtx, request: Request, session: _Db, provider_id: str
    ) -> None:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = session.get(LlmProvider, provider_id)
            if row is None:
                raise _not_found()
            references = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.provider_id == provider_id
                )
            )
            if references:
                raise _conflict("provider_in_use")
            session.delete(row)
            _commit_or_conflict(session, "provider_constraint_violation")
            _publish_deployment_defaults_changed(ctx, request)

    @router.get(
        "/models",
        response_model=list[LlmModelResponse],
        operation_id="admin.llm.models.list",
        openapi_extra=_llm_cli("models-list", "List LLM models", mutates=False),
    )
    def list_models(_ctx: _ReadCtx, session: _Db) -> list[LlmModelResponse]:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            provider_models = list(session.scalars(select(LlmProviderModel)).all())
            models = list(
                session.scalars(
                    select(LlmModel).order_by(LlmModel.display_name, LlmModel.id)
                ).all()
            )
        counts: Counter[str] = Counter(row.model_id for row in provider_models)
        return [_model_response(row, counts) for row in models]

    @router.post(
        "/models",
        response_model=LlmModelResponse,
        operation_id="admin.llm.models.create",
        openapi_extra=_llm_cli("models", "Create an LLM model", mutates=True),
    )
    def create_model(
        ctx: _WriteCtx, request: Request, session: _Db, payload: ModelPayload
    ) -> LlmModelResponse:
        now = _now()
        row = LlmModel(
            id=new_ulid(),
            canonical_name=payload.canonical_name,
            display_name=payload.display_name,
            capabilities=payload.capabilities,
            context_window=payload.context_window,
            max_output_tokens=payload.max_output_tokens,
            embedding_dimensions=payload.embedding_dimensions,
            temperature=payload.temperature,
            thinking_level=payload.thinking_level,
            thinking_strategy=payload.thinking_strategy,
            price_source=payload.price_source,
            price_source_model_id=payload.price_source_model_id,
            is_active=payload.is_active,
            notes=payload.notes,
            created_at=now,
            updated_at=now,
            updated_by_user_id=ctx.user_id,
        )
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            _validate_model_payload(session, payload)
            session.add(row)
            _commit_or_conflict(session, "model_constraint_violation")
            _publish_deployment_defaults_changed(ctx, request)
            session.refresh(row)
        return _model_response(row, Counter())

    @router.post(
        "/models/openrouter-preview",
        response_model=OpenRouterModelPreviewResponse,
        operation_id="admin.llm.models.openrouter_preview",
        openapi_extra=_llm_cli(
            "openrouter-preview", "Preview OpenRouter model metadata", mutates=True
        ),
    )
    def openrouter_model_preview(
        ctx: _WriteCtx,
        request: Request,
        session: _Db,
        payload: OpenRouterModelPreviewRequest,
    ) -> OpenRouterModelPreviewResponse:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            response, pricing_changed = _openrouter_metadata_preview(session, payload)
            if pricing_changed:
                _publish_deployment_defaults_changed(ctx, request)
            return response

    @router.get(
        "/models/{model_id}",
        response_model=LlmModelResponse,
        operation_id="admin.llm.models.get",
        openapi_extra=_llm_cli("models-show", "Show an LLM model", mutates=False),
    )
    def get_model(_ctx: _ReadCtx, session: _Db, model_id: str) -> LlmModelResponse:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = session.get(LlmModel, model_id)
            if row is None:
                raise _not_found()
            count = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.model_id == model_id
                )
            )
        return _model_response(row, Counter({model_id: int(count or 0)}))

    @router.put(
        "/models/{model_id}",
        response_model=LlmModelResponse,
        operation_id="admin.llm.models.update",
        openapi_extra=_llm_cli("models-replace", "Update an LLM model", mutates=True),
    )
    def update_model(
        ctx: _WriteCtx,
        request: Request,
        session: _Db,
        model_id: str,
        payload: ModelPayload,
    ) -> LlmModelResponse:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = session.get(LlmModel, model_id)
            if row is None:
                raise _not_found()
            _validate_model_payload(session, payload, model_id=model_id)
            row.canonical_name = payload.canonical_name
            row.display_name = payload.display_name
            row.capabilities = payload.capabilities
            row.context_window = payload.context_window
            row.max_output_tokens = payload.max_output_tokens
            row.embedding_dimensions = payload.embedding_dimensions
            row.temperature = payload.temperature
            row.thinking_level = payload.thinking_level
            row.thinking_strategy = payload.thinking_strategy
            row.price_source = payload.price_source
            row.price_source_model_id = payload.price_source_model_id
            row.is_active = payload.is_active
            row.notes = payload.notes
            row.updated_at = _now()
            row.updated_by_user_id = ctx.user_id
            _commit_or_conflict(session, "model_constraint_violation")
            _publish_deployment_defaults_changed(ctx, request)
            session.refresh(row)
            count = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.model_id == model_id
                )
            )
        return _model_response(row, Counter({model_id: int(count or 0)}))

    @router.delete(
        "/models/{model_id}",
        status_code=204,
        operation_id="admin.llm.models.delete",
        openapi_extra=_llm_cli("models-delete", "Delete an LLM model", mutates=True),
    )
    def delete_model(
        ctx: _WriteCtx, request: Request, session: _Db, model_id: str
    ) -> None:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = session.get(LlmModel, model_id)
            if row is None:
                raise _not_found()
            references = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.model_id == model_id
                )
            )
            if references:
                raise _conflict("model_in_use")
            session.delete(row)
            _commit_or_conflict(session, "model_constraint_violation")
            _publish_deployment_defaults_changed(ctx, request)

    @router.get(
        "/provider-models",
        response_model=list[LlmProviderModelResponse],
        operation_id="admin.llm.provider_models.list",
        openapi_extra=_llm_cli(
            "provider-models-list", "List provider models", mutates=False
        ),
    )
    def list_provider_models(
        _ctx: _ReadCtx,
        session: _Db,
        provider_id: str | None = Query(default=None),
        model_id: str | None = Query(default=None),
    ) -> list[LlmProviderModelResponse]:
        stmt = select(LlmProviderModel).order_by(
            LlmProviderModel.api_model_id, LlmProviderModel.id
        )
        if provider_id is not None:
            stmt = stmt.where(LlmProviderModel.provider_id == provider_id)
        if model_id is not None:
            stmt = stmt.where(LlmProviderModel.model_id == model_id)
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            rows = list(session.scalars(stmt).all())
            models_by_id = {
                row.id: row
                for row in session.scalars(
                    select(LlmModel).where(
                        LlmModel.id.in_({row.model_id for row in rows})
                    )
                ).all()
            }
        return [
            _provider_model_response(row, models_by_id.get(row.model_id))
            for row in rows
        ]

    @router.post(
        "/provider-models",
        response_model=LlmProviderModelResponse,
        operation_id="admin.llm.provider_models.create",
        openapi_extra=_llm_cli(
            "provider-models", "Create a provider model", mutates=True
        ),
    )
    def create_provider_model(
        ctx: _WriteCtx, request: Request, session: _Db, payload: ProviderModelPayload
    ) -> LlmProviderModelResponse:
        now = _now()
        row = LlmProviderModel(
            id=new_ulid(),
            provider_id=payload.provider_id,
            model_id=payload.model_id,
            api_model_id=payload.api_model_id,
            input_cost_per_million=_money_decimal(payload.input_cost_per_million),
            output_cost_per_million=_money_decimal(payload.output_cost_per_million),
            fixed_cost_per_call_usd=_money_decimal(payload.fixed_cost_per_call_usd),
            audio_cost_per_hour_usd=_money_decimal(payload.audio_cost_per_hour_usd),
            audio_input_transform=payload.audio_input_transform,
            image_input_format=payload.image_input_format,
            image_input_max_edge_px=payload.image_input_max_edge_px,
            max_tokens_override=payload.max_tokens_override,
            supports_system_prompt=payload.supports_system_prompt,
            supports_temperature=payload.supports_temperature,
            thinking_strategy_override=payload.thinking_strategy_override,
            extra_api_params=payload.extra_api_params,
            price_source_override=payload.price_source_override,
            price_source_model_id_override=payload.price_source_model_id_override,
            is_enabled=payload.is_enabled,
            created_at=now,
            updated_at=now,
        )
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            _validate_provider_model_payload(session, payload)
            session.add(row)
            _flush_or_conflict(session, "provider_model_constraint_violation")
            model = session.get(LlmModel, row.model_id)
            if model is None:
                raise _unprocessable("model_not_found")
            _sync_provider_model_pricing(row, model, now=now)
            _commit_or_conflict(session, "provider_model_constraint_violation")
            _publish_deployment_defaults_changed(ctx, request)
            session.refresh(row)
        return _provider_model_response(row, model)

    @router.get(
        "/provider-models/{provider_model_id}",
        response_model=LlmProviderModelResponse,
        operation_id="admin.llm.provider_models.get",
        openapi_extra=_llm_cli(
            "provider-models-show", "Show a provider model", mutates=False
        ),
    )
    def get_provider_model(
        _ctx: _ReadCtx, session: _Db, provider_model_id: str
    ) -> LlmProviderModelResponse:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = _provider_model(session, provider_model_id)
            model = session.get(LlmModel, row.model_id)
        return _provider_model_response(row, model)

    @router.post(
        "/provider-models/{provider_model_id}/sync-pricing",
        response_model=LlmProviderModelSyncPricingResponse,
        operation_id="admin.llm.provider_models.sync_pricing",
        openapi_extra=_llm_cli(
            "provider-models-sync-pricing",
            "Sync pricing for a provider model",
            mutates=True,
        ),
    )
    def sync_provider_model_pricing(
        ctx: _WriteCtx,
        request: Request,
        session: _Db,
        provider_model_id: str,
    ) -> LlmProviderModelSyncPricingResponse:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = _provider_model(session, provider_model_id)
            model = session.get(LlmModel, row.model_id)
            if model is None:
                raise _unprocessable("model_not_found")
            delta = _sync_provider_model_pricing(row, model, now=_now())
            _commit_or_conflict(session, "provider_model_constraint_violation")
            if delta.status == "updated":
                _publish_deployment_defaults_changed(ctx, request)
            session.refresh(row)
        return LlmProviderModelSyncPricingResponse(
            provider_model=_provider_model_response(row, model),
            pricing_sync_result=delta,
        )

    @router.post(
        "/provider-models/{provider_model_id}/embedding-smoke",
        response_model=LlmProviderModelEmbeddingSmokeResponse,
        operation_id="admin.llm.provider_models.embedding_smoke",
        openapi_extra=_llm_cli(
            "embedding-smoke", "Run a provider-model embedding smoke test", mutates=True
        ),
    )
    def provider_model_embedding_smoke(
        _ctx: _WriteCtx,
        request: Request,
        session: _Db,
        provider_model_id: str,
        payload: LlmProviderModelEmbeddingSmokeRequest,
    ) -> LlmProviderModelEmbeddingSmokeResponse:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            provider_model, provider, model = _load_playground_target(
                session, provider_model_id
            )
            client = _embedding_smoke_client(provider, model)
            dimensions = _embedding_smoke_dimensions(model)
        started = time.monotonic()
        try:
            vector = client.embed([payload.text])[0]
        except FastEmbedEmbeddingError as exc:
            return LlmProviderModelEmbeddingSmokeResponse(
                status="error",
                model_used=provider_model.api_model_id,
                provider_used=provider.name,
                provider_model_id=provider_model.id,
                latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                embedding_dimensions=dimensions,
                error_id=request_correlation_id(request),
                error_code="embedding_smoke_failed",
                error_message=scrub_string(str(exc)),
            )
        return LlmProviderModelEmbeddingSmokeResponse(
            status="ok",
            model_used=provider_model.api_model_id,
            provider_used=provider.name,
            provider_model_id=provider_model.id,
            latency_ms=max(0, round((time.monotonic() - started) * 1000)),
            embedding_dimensions=len(vector),
            vector_norm=round(math.sqrt(sum(value * value for value in vector)), 6),
        )

    @router.post(
        "/provider-models/{provider_model_id}/playground",
        response_model=LlmProviderModelPlaygroundResponse,
        operation_id="admin.llm.provider_models.playground",
        openapi_extra={
            **_llm_cli(
                "playground", "Run a provider-model playground prompt", mutates=True
            ),
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": LlmProviderModelPlaygroundRequest.model_json_schema()
                    },
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["prompt"],
                            "properties": {
                                "mode": {
                                    "type": "string",
                                    "enum": ["direct", "assignment"],
                                    "default": "direct",
                                },
                                "prompt": {"type": "string", "maxLength": 16000},
                                "system_prompt": {
                                    "type": "string",
                                    "maxLength": 8000,
                                    "nullable": True,
                                },
                                "max_tokens": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 32000,
                                    "nullable": True,
                                },
                                "temperature": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 2,
                                    "nullable": True,
                                },
                                "image_url": {
                                    "type": "string",
                                    "maxLength": 262144,
                                    "nullable": True,
                                },
                                "audio_url": {
                                    "type": "string",
                                    "maxLength": 262144,
                                    "nullable": True,
                                },
                                "assignment_id": {"type": "string", "nullable": True},
                                "thinking_level": {
                                    "type": "string",
                                    "enum": ["disabled", "low", "medium", "high"],
                                    "nullable": True,
                                },
                                "thinking_strategy": {
                                    "type": "string",
                                    "enum": [
                                        "none",
                                        "gemma_system_token",
                                        "glm_extra_body",
                                        "openrouter_extra_body",
                                    ],
                                    "nullable": True,
                                },
                                "image_file": {"type": "string", "format": "binary"},
                                "audio_file": {"type": "string", "format": "binary"},
                            },
                        }
                    },
                },
                "required": True,
            },
        },
    )
    async def provider_model_playground(
        _ctx: _WriteCtx,
        request: Request,
        session: _Db,
        provider_model_id: str,
    ) -> LlmProviderModelPlaygroundResponse:
        payload, image_url, upload_audio_ref = await _playground_request_payload(
            request
        )
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            provider_model, provider, model = _load_playground_target(
                session, provider_model_id
            )
            assignment = _validate_playground_assignment(
                session,
                payload=payload,
                provider_model_id=provider_model_id,
            )
            max_tokens = _playground_max_tokens(
                payload, provider_model, model, assignment
            )
            temperature = _playground_temperature(
                payload, provider_model, model, assignment
            )
            llm = _playground_llm(request, session, provider)
        has_image = image_url is not None or payload.image_url is not None
        if has_image and "vision" not in set(model.capabilities or []):
            raise _unprocessable("image_requires_vision_model")
        has_audio = upload_audio_ref is not None or payload.audio_url is not None
        if has_audio and "audio_input" not in set(model.capabilities or []):
            raise _unprocessable("audio_requires_audio_model")
        image_ref = await _playground_image_ref(payload, image_url)
        audio_ref = await _playground_audio_ref(payload, upload_audio_ref)
        use_transcription_endpoint = _playground_uses_transcription_endpoint(
            model, audio_ref
        )
        image_ref = await _playground_normalized_image_ref(image_ref, provider_model)
        if not use_transcription_endpoint:
            audio_ref = await _playground_normalized_audio_ref(
                audio_ref, provider_model
            )
        messages = _playground_messages(
            payload,
            provider_model,
            model,
            image_ref,
            audio_ref,
        )
        started = time.monotonic()
        try:
            if use_transcription_endpoint:
                transcribe = getattr(llm, "transcribe", None)
                if not callable(transcribe):
                    raise LlmProviderError("provider does not support transcription")
                response = transcribe(
                    model_id=provider_model.api_model_id,
                    audio=audio_ref,
                    temperature=temperature,
                    consents=ConsentSet.none(),
                )
            else:
                response = llm.chat(
                    model_id=provider_model.api_model_id,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    thinking_level=(
                        payload.thinking_level
                        or _thinking_level(
                            assignment.thinking_level_override
                            if assignment is not None
                            and assignment.thinking_level_override is not None
                            else model.thinking_level
                        )
                    ),
                    thinking_strategy=(
                        payload.thinking_strategy
                        or _thinking_strategy(
                            provider_model.thinking_strategy_override
                            or model.thinking_strategy
                        )
                    ),
                    consents=ConsentSet.none(),
                )
        except (LlmProviderError, LlmRateLimited, LlmTransportError) as exc:
            return LlmProviderModelPlaygroundResponse(
                status="error",
                model_used=provider_model.api_model_id,
                provider_used=provider.name,
                provider_model_id=provider_model.id,
                assignment_id=assignment.id if assignment is not None else None,
                latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                error_id=request_correlation_id(request),
                error_code="provider_rejected_request",
                error_message=_safe_provider_error(exc, provider_name=provider.name),
            )
        cost_usd = _playground_cost_usd(
            provider_model,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            audio_seconds=response.usage.seconds,
        )
        return LlmProviderModelPlaygroundResponse(
            status="ok",
            assistant_text=response.text,
            model_used=response.model_id or provider_model.api_model_id,
            provider_used=provider.name,
            provider_model_id=provider_model.id,
            assignment_id=assignment.id if assignment is not None else None,
            latency_ms=max(0, round((time.monotonic() - started) * 1000)),
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            finish_reason=response.finish_reason,
            stop_reason=response.finish_reason,
            cost_usd=cost_usd,
            cost_cents=int(cost_usd * Decimal(100)),
        )

    @router.put(
        "/provider-models/{provider_model_id}",
        response_model=LlmProviderModelResponse,
        operation_id="admin.llm.provider_models.update",
        openapi_extra=_llm_cli(
            "provider-models-replace", "Update a provider model", mutates=True
        ),
    )
    def update_provider_model(
        ctx: _WriteCtx,
        request: Request,
        session: _Db,
        provider_model_id: str,
        payload: ProviderModelPayload,
    ) -> LlmProviderModelResponse:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = _provider_model(session, provider_model_id)
            _validate_provider_model_payload(
                session, payload, provider_model_id=provider_model_id
            )
            old_model = session.get(LlmModel, row.model_id)
            if old_model is None:
                raise _unprocessable("model_not_found")
            old_lookup = _provider_model_price_lookup(row, old_model)
            old_price_source_override = row.price_source_override
            old_price_source_model_id_override = row.price_source_model_id_override
            row.provider_id = payload.provider_id
            row.model_id = payload.model_id
            row.api_model_id = payload.api_model_id
            row.input_cost_per_million = _money_decimal(payload.input_cost_per_million)
            row.output_cost_per_million = _money_decimal(
                payload.output_cost_per_million
            )
            row.fixed_cost_per_call_usd = _money_decimal(
                payload.fixed_cost_per_call_usd
            )
            row.audio_cost_per_hour_usd = _money_decimal(
                payload.audio_cost_per_hour_usd
            )
            row.audio_input_transform = payload.audio_input_transform
            row.image_input_format = payload.image_input_format
            row.image_input_max_edge_px = payload.image_input_max_edge_px
            row.max_tokens_override = payload.max_tokens_override
            row.supports_system_prompt = payload.supports_system_prompt
            row.supports_temperature = payload.supports_temperature
            row.thinking_strategy_override = payload.thinking_strategy_override
            row.extra_api_params = payload.extra_api_params
            row.price_source_override = payload.price_source_override
            row.price_source_model_id_override = payload.price_source_model_id_override
            row.is_enabled = payload.is_enabled
            row.updated_at = _now()
            model = session.get(LlmModel, row.model_id)
            if model is None:
                raise _unprocessable("model_not_found")
            new_lookup = _provider_model_price_lookup(row, model)
            should_sync_pricing = new_lookup is not None and (
                payload.price_source_override != old_price_source_override
                or payload.price_source_model_id_override
                != old_price_source_model_id_override
                or new_lookup != old_lookup
            )
            if should_sync_pricing:
                _sync_provider_model_pricing(row, model, now=row.updated_at)
            _commit_or_conflict(session, "provider_model_constraint_violation")
            _publish_deployment_defaults_changed(ctx, request)
            session.refresh(row)
        return _provider_model_response(row, model)

    @router.delete(
        "/provider-models/{provider_model_id}",
        status_code=204,
        operation_id="admin.llm.provider_models.delete",
        openapi_extra=_llm_cli(
            "provider-models-delete", "Delete a provider model", mutates=True
        ),
    )
    def delete_provider_model(
        ctx: _WriteCtx, request: Request, session: _Db, provider_model_id: str
    ) -> None:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = _provider_model(session, provider_model_id)
            references = session.scalar(
                select(func.count(LlmAssignment.id)).where(
                    LlmAssignment.model_id == provider_model_id
                )
            )
            if references:
                raise _conflict("provider_model_in_use")
            session.delete(row)
            _commit_or_conflict(session, "provider_model_constraint_violation")
            _publish_deployment_defaults_changed(ctx, request)

    @router.get(
        "/assignments",
        response_model=list[LlmAssignmentResponse],
        operation_id="admin.llm.assignments.list",
        openapi_extra=_llm_cli(
            "assignments-list", "List LLM assignments", mutates=False
        ),
    )
    def list_assignments(_ctx: _ReadCtx, session: _Db) -> list[LlmAssignmentResponse]:
        cutoff = _now() - timedelta(days=30)
        # justification: deployment-admin LLM dashboard reads deployment-global config
        # plus cross-workspace llm_usage aggregation across all workspaces, by design.
        with tenant_agnostic():
            rows = list(
                session.scalars(
                    select(LlmAssignment)
                    .where(LlmAssignment.workspace_id.is_(None))
                    .order_by(
                        LlmAssignment.capability,
                        LlmAssignment.priority,
                        LlmAssignment.id,
                    )
                ).all()
            )
            usage = _assignment_usage(session, cutoff)
            provider_models_by_id, models_by_id = _assignment_response_context(
                session, rows
            )
        return [
            _assignment_response(
                row,
                provider_models_by_id=provider_models_by_id,
                models_by_id=models_by_id,
                usage=usage,
            )
            for row in rows
        ]

    @router.get(
        "/inheritance",
        response_model=list[LlmCapabilityInheritanceResponse],
        operation_id="admin.llm.inheritance.list",
        openapi_extra=_llm_cli(
            "inheritance-list", "List LLM capability inheritance", mutates=False
        ),
    )
    def list_inheritance(
        _ctx: _ReadCtx, session: _Db
    ) -> list[LlmCapabilityInheritanceResponse]:
        # justification: deployment-level llm_capability_inheritance; workspace_id
        # is a legacy NULL column, not a live tenant boundary (deployment-admin).
        with tenant_agnostic():
            rows = list(
                session.scalars(
                    select(LlmCapabilityInheritance)
                    .where(LlmCapabilityInheritance.workspace_id.is_(None))
                    .order_by(LlmCapabilityInheritance.capability)
                ).all()
            )
        return [_inheritance_response(row) for row in rows]

    @router.post(
        "/inheritance",
        response_model=LlmCapabilityInheritanceResponse,
        operation_id="admin.llm.inheritance.create",
        openapi_extra=_llm_cli(
            "inheritance", "Create LLM capability inheritance", mutates=True
        ),
    )
    def create_inheritance(
        ctx: _WriteCtx,
        session: _Db,
        request: Request,
        payload: CapabilityInheritancePayload,
    ) -> LlmCapabilityInheritanceResponse:
        # justification: deployment-level llm_capability_inheritance; workspace_id
        # is a legacy NULL column, not a live tenant boundary (deployment-admin).
        with tenant_agnostic():
            _validate_inheritance_edge(
                session,
                capability=payload.capability,
                inherits_from=payload.inherits_from,
            )
            existing = session.scalar(
                select(LlmCapabilityInheritance.id).where(
                    LlmCapabilityInheritance.workspace_id.is_(None),
                    LlmCapabilityInheritance.capability == payload.capability,
                )
            )
            if existing is not None:
                raise _conflict("capability_inheritance_exists")
            direct_assignments = _direct_assignments_for_capability(
                session, payload.capability
            )
            if direct_assignments and not payload.clear_direct_assignments:
                raise _conflict("capability_direct_assignments_exist")
            for assignment in direct_assignments:
                session.delete(assignment)
            row = LlmCapabilityInheritance(
                id=new_ulid(),
                workspace_id=None,
                capability=payload.capability,
                inherits_from=payload.inherits_from,
                created_at=_now(),
            )
            session.add(row)
            _flush_or_conflict(session, "capability_inheritance_constraint_violation")
            _publish_assignment_changed(ctx, request)
            _commit_or_conflict(session, "capability_inheritance_constraint_violation")
            session.refresh(row)
        return _inheritance_response(row)

    @router.put(
        "/inheritance/{capability}",
        response_model=LlmCapabilityInheritanceResponse,
        operation_id="admin.llm.inheritance.update",
        openapi_extra=_llm_cli(
            "inheritance-replace", "Update LLM capability inheritance", mutates=True
        ),
    )
    def update_inheritance(
        ctx: _WriteCtx,
        session: _Db,
        request: Request,
        capability: str,
        payload: CapabilityInheritanceUpdatePayload,
    ) -> LlmCapabilityInheritanceResponse:
        # justification: deployment-level llm_capability_inheritance; workspace_id
        # is a legacy NULL column, not a live tenant boundary (deployment-admin).
        with tenant_agnostic():
            row = _explicit_inheritance(session, capability)
            _validate_inheritance_edge(
                session,
                capability=capability,
                inherits_from=payload.inherits_from,
            )
            row.inherits_from = payload.inherits_from
            _flush_or_conflict(session, "capability_inheritance_constraint_violation")
            _publish_assignment_changed(ctx, request)
            _commit_or_conflict(session, "capability_inheritance_constraint_violation")
            session.refresh(row)
        return _inheritance_response(row)

    @router.delete(
        "/inheritance/{capability}",
        status_code=204,
        operation_id="admin.llm.inheritance.delete",
        openapi_extra=_llm_cli(
            "inheritance-delete", "Delete LLM capability inheritance", mutates=True
        ),
    )
    def delete_inheritance(
        ctx: _WriteCtx,
        session: _Db,
        request: Request,
        capability: str,
    ) -> None:
        # justification: deployment-level llm_capability_inheritance; workspace_id
        # is a legacy NULL column, not a live tenant boundary (deployment-admin).
        with tenant_agnostic():
            row = _explicit_inheritance(session, capability)
            session.delete(row)
            _flush_or_conflict(session, "capability_inheritance_constraint_violation")
            _publish_assignment_changed(ctx, request)
            _commit_or_conflict(session, "capability_inheritance_constraint_violation")

    @router.post(
        "/assignments",
        response_model=LlmAssignmentResponse,
        operation_id="admin.llm.assignments.create",
        openapi_extra=_llm_cli("assignments", "Create an LLM assignment", mutates=True),
    )
    def create_assignment(
        ctx: _WriteCtx, session: _Db, request: Request, payload: AssignmentPayload
    ) -> LlmAssignmentResponse:
        now = _now()
        # justification: deployment-level llm_assignment config; workspace_id is a
        # legacy NULL column, not a live tenant boundary. Deployment-admin by design.
        with tenant_agnostic():
            provider_model = _provider_model(session, payload.provider_model_id)
            provider = session.get(LlmProvider, provider_model.provider_id)
            required_capabilities = _validate_required_capabilities(
                payload.capability, payload.required_capabilities
            )
            _validate_assignment_priority(
                session,
                capability=payload.capability,
                priority=payload.priority,
            )
            _raise_missing_capabilities(
                _missing_capabilities(
                    session,
                    provider_model_id=payload.provider_model_id,
                    required_capabilities=required_capabilities,
                )
            )
            row = LlmAssignment(
                id=new_ulid(),
                workspace_id=None,
                capability=payload.capability,
                model_id=payload.provider_model_id,
                provider=provider.name
                if provider is not None
                else provider_model.provider_id,
                priority=payload.priority,
                enabled=payload.is_enabled,
                max_tokens=payload.max_tokens,
                temperature=payload.temperature,
                thinking_level_override=payload.thinking_level_override,
                extra_api_params=payload.extra_api_params,
                required_capabilities=required_capabilities,
                created_at=now,
            )
            session.add(row)
            _flush_or_conflict(session, "assignment_constraint_violation")
            _publish_assignment_changed(ctx, request)
            _commit_or_conflict(session, "assignment_constraint_violation")
            session.refresh(row)
            provider_models_by_id, models_by_id = _assignment_response_context(
                session, [row]
            )
        return _assignment_response(
            row,
            provider_models_by_id=provider_models_by_id,
            models_by_id=models_by_id,
            usage={},
        )

    @router.patch(
        "/assignments/reorder",
        response_model=list[LlmAssignmentResponse],
        operation_id="admin.llm.assignments.reorder",
        openapi_extra=_llm_cli(
            "assignments-reorder-update", "Reorder LLM assignments", mutates=True
        ),
    )
    def reorder_assignments(
        ctx: _WriteCtx,
        session: _Db,
        request: Request,
        payload: list[AssignmentReorderItem],
    ) -> list[LlmAssignmentResponse]:
        changed = False
        # justification: deployment-level llm_assignment config; workspace_id is a
        # legacy NULL column, not a live tenant boundary. Deployment-admin by design.
        with tenant_agnostic():
            for group in payload:
                rows = list(
                    session.scalars(
                        select(LlmAssignment).where(
                            LlmAssignment.id.in_(group.ids_in_priority_order),
                            LlmAssignment.capability == group.capability,
                        )
                    ).all()
                )
                by_id = {row.id: row for row in rows}
                if set(by_id) != set(group.ids_in_priority_order):
                    raise _unprocessable("assignment_reorder_mismatch")
                if any(row.workspace_id is not None for row in rows):
                    raise _unprocessable("assignment_reorder_mismatch")
                all_group_ids = set(
                    session.scalars(
                        select(LlmAssignment.id).where(
                            LlmAssignment.workspace_id.is_(None),
                            LlmAssignment.capability == group.capability,
                        )
                    ).all()
                )
                if all_group_ids != set(group.ids_in_priority_order):
                    raise _unprocessable("assignment_reorder_mismatch")
                for priority, assignment_id in enumerate(group.ids_in_priority_order):
                    row = by_id[assignment_id]
                    row.priority = priority
                    changed = True
            _flush_or_conflict(session, "assignment_constraint_violation")
            if changed:
                _publish_assignment_changed(ctx, request)
            _commit_or_conflict(session, "assignment_constraint_violation")
            all_rows = list(
                session.scalars(
                    select(LlmAssignment)
                    .where(LlmAssignment.workspace_id.is_(None))
                    .order_by(
                        LlmAssignment.capability,
                        LlmAssignment.priority,
                        LlmAssignment.id,
                    )
                ).all()
            )
            provider_models_by_id, models_by_id = _assignment_response_context(
                session, all_rows
            )
        return [
            _assignment_response(
                row,
                provider_models_by_id=provider_models_by_id,
                models_by_id=models_by_id,
                usage={},
            )
            for row in all_rows
        ]

    @router.get(
        "/assignments/{assignment_id}",
        response_model=LlmAssignmentResponse,
        operation_id="admin.llm.assignments.get",
        openapi_extra=_llm_cli(
            "assignments-show", "Show an LLM assignment", mutates=False
        ),
    )
    def get_assignment(
        _ctx: _ReadCtx, session: _Db, assignment_id: str
    ) -> LlmAssignmentResponse:
        cutoff = _now() - timedelta(days=30)
        # justification: deployment-admin LLM dashboard reads deployment-global config
        # plus cross-workspace llm_usage aggregation across all workspaces, by design.
        with tenant_agnostic():
            row = _assignment(session, assignment_id)
            usage = _assignment_usage(session, cutoff)
            provider_models_by_id, models_by_id = _assignment_response_context(
                session, [row]
            )
        return _assignment_response(
            row,
            provider_models_by_id=provider_models_by_id,
            models_by_id=models_by_id,
            usage=usage,
        )

    @router.put(
        "/assignments/{assignment_id}",
        response_model=LlmAssignmentResponse,
        operation_id="admin.llm.assignments.update",
        openapi_extra=_llm_cli(
            "assignments-replace", "Update an LLM assignment", mutates=True
        ),
    )
    def update_assignment(
        ctx: _WriteCtx,
        session: _Db,
        request: Request,
        assignment_id: str,
        payload: AssignmentUpdatePayload,
    ) -> LlmAssignmentResponse:
        # code-health: ignore[nloc] Router owns auth, deps, and route registration.  # noqa: E501
        # justification: deployment-level llm_assignment config; workspace_id is a
        # legacy NULL column, not a live tenant boundary. Deployment-admin by design.
        with tenant_agnostic():
            row = _assignment(session, assignment_id)
            sent = payload.model_fields_set
            required_capabilities = _validate_required_capabilities(
                row.capability, payload.required_capabilities
            )
            if "priority" in sent:
                if payload.priority is None:
                    raise _unprocessable("priority_required")
                _validate_assignment_priority(
                    session,
                    capability=row.capability,
                    priority=payload.priority,
                    assignment_id=row.id,
                )
                row.priority = payload.priority
            if "provider_model_id" in sent:
                if payload.provider_model_id is None:
                    raise _unprocessable("provider_model_id_required")
                provider_model = _provider_model(session, payload.provider_model_id)
                provider = session.get(LlmProvider, provider_model.provider_id)
                _raise_missing_capabilities(
                    _missing_capabilities(
                        session,
                        provider_model_id=payload.provider_model_id,
                        required_capabilities=required_capabilities,
                    )
                )
                row.model_id = payload.provider_model_id
                row.provider = (
                    provider.name
                    if provider is not None
                    else provider_model.provider_id
                )
            elif "required_capabilities" in sent:
                _raise_missing_capabilities(
                    _missing_capabilities(
                        session,
                        provider_model_id=row.model_id,
                        required_capabilities=required_capabilities,
                    )
                )
            if "max_tokens" in sent:
                row.max_tokens = payload.max_tokens
            if "temperature" in sent:
                row.temperature = payload.temperature
            if "thinking_level_override" in sent:
                row.thinking_level_override = payload.thinking_level_override
            if "extra_api_params" in sent:
                row.extra_api_params = payload.extra_api_params or {}
            if "required_capabilities" in sent or "provider_model_id" in sent:
                row.required_capabilities = required_capabilities
            if "is_enabled" in sent:
                if payload.is_enabled is None:
                    raise _unprocessable("is_enabled_required")
                row.enabled = payload.is_enabled
            _flush_or_conflict(session, "assignment_constraint_violation")
            _publish_assignment_changed(ctx, request)
            _commit_or_conflict(session, "assignment_constraint_violation")
            session.refresh(row)
            provider_models_by_id, models_by_id = _assignment_response_context(
                session, [row]
            )
        return _assignment_response(
            row,
            provider_models_by_id=provider_models_by_id,
            models_by_id=models_by_id,
            usage={},
        )

    @router.delete(
        "/assignments/{assignment_id}",
        status_code=204,
        operation_id="admin.llm.assignments.delete",
        openapi_extra=_llm_cli(
            "assignments-delete", "Delete an LLM assignment", mutates=True
        ),
    )
    def delete_assignment(
        ctx: _WriteCtx,
        session: _Db,
        request: Request,
        assignment_id: str,
    ) -> None:
        # justification: deployment-level llm_assignment config; workspace_id is a
        # legacy NULL column, not a live tenant boundary. Deployment-admin by design.
        with tenant_agnostic():
            row = _assignment(session, assignment_id)
            session.delete(row)
            _flush_or_conflict(session, "assignment_constraint_violation")
            _publish_assignment_changed(ctx, request)
            _commit_or_conflict(session, "assignment_constraint_violation")

    @router.get(
        "/prompts",
        response_model=list[LlmPromptTemplateResponse],
        operation_id="admin.llm.prompts.list",
        openapi_extra=_llm_cli("prompts-list", "List LLM prompts", mutates=False),
    )
    def list_prompts(_ctx: _ReadCtx, session: _Db) -> list[LlmPromptTemplateResponse]:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_prompt_template is deployment-global (no workspace_id column).
        with tenant_agnostic():
            rows = list(
                session.scalars(
                    select(LlmPromptTemplate)
                    .where(LlmPromptTemplate.is_active.is_(True))
                    .order_by(LlmPromptTemplate.capability)
                ).all()
            )
            revision_count_rows = session.execute(
                select(
                    LlmPromptTemplateRevision.template_id,
                    func.count(LlmPromptTemplateRevision.id),
                ).group_by(LlmPromptTemplateRevision.template_id)
            ).all()
            revision_counts: dict[str, int] = {
                template_id: int(count or 0)
                for template_id, count in revision_count_rows
            }
        return [
            _prompt_response(row, int(revision_counts.get(row.id, 0))) for row in rows
        ]

    @router.get(
        "/prompts/{prompt_id}",
        response_model=LlmPromptTemplateDetail,
        operation_id="admin.llm.prompts.get",
        openapi_extra=_llm_cli("prompts-show", "Show an LLM prompt", mutates=False),
    )
    def get_prompt(
        _ctx: _ReadCtx, session: _Db, prompt_id: str
    ) -> LlmPromptTemplateDetail:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_prompt_template is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = session.get(LlmPromptTemplate, prompt_id)
            if row is None:
                # code-health: ignore[duplicate] Repeated DTO/event field lists keep external wire contracts explicit at each boundary.  # noqa: E501
                raise _not_found()
            # code-health: ignore[duplicate] Repeated DTO/event field lists keep external wire contracts explicit at each boundary.  # noqa: E501
            count = session.scalar(
                select(func.count(LlmPromptTemplateRevision.id)).where(
                    LlmPromptTemplateRevision.template_id == prompt_id
                )
            )
        base = _prompt_response(row, int(count or 0))
        return LlmPromptTemplateDetail(
            **base.model_dump(), template=row.template, notes=row.notes
        )

    @router.put(
        "/prompts/{prompt_id}",
        response_model=LlmPromptTemplateDetail,
        operation_id="admin.llm.prompts.update",
        openapi_extra=_llm_cli("prompts-replace", "Update an LLM prompt", mutates=True),
    )
    def update_prompt(
        ctx: _WriteCtx, session: _Db, prompt_id: str, payload: PromptUpdatePayload
    ) -> LlmPromptTemplateDetail:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_prompt_template is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = session.get(LlmPromptTemplate, prompt_id)
            if row is None:
                raise _not_found()
            revision = LlmPromptTemplateRevision(
                id=new_ulid(),
                template_id=row.id,
                version=row.version,
                body=row.template,
                notes=row.notes,
                created_at=_now(),
                created_by_user_id=ctx.user_id,
            )
            session.add(revision)
            row.template = payload.template
            row.notes = payload.notes
            row.version += 1
            row.updated_at = _now()
            _commit_or_conflict(session, "prompt_constraint_violation")
            session.refresh(row)
            count = session.scalar(
                select(func.count(LlmPromptTemplateRevision.id)).where(
                    LlmPromptTemplateRevision.template_id == prompt_id
                )
            )
        base = _prompt_response(row, int(count or 0))
        return LlmPromptTemplateDetail(
            **base.model_dump(), template=row.template, notes=row.notes
        )

    @router.get(
        "/prompts/{prompt_id}/revisions",
        response_model=list[LlmPromptRevisionResponse],
        operation_id="admin.llm.prompts.revisions",
        openapi_extra=_llm_cli(
            "prompts-revisions-list", "List LLM prompt revisions", mutates=False
        ),
    )
    def prompt_revisions(
        _ctx: _ReadCtx, session: _Db, prompt_id: str
    ) -> list[LlmPromptRevisionResponse]:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_prompt_template is deployment-global (no workspace_id column).
        with tenant_agnostic():
            if session.get(LlmPromptTemplate, prompt_id) is None:
                raise _not_found()
            rows = list(
                session.scalars(
                    select(LlmPromptTemplateRevision)
                    .where(LlmPromptTemplateRevision.template_id == prompt_id)
                    .order_by(LlmPromptTemplateRevision.version.desc())
                ).all()
            )
        return [
            LlmPromptRevisionResponse(
                id=row.id,
                template_id=row.template_id,
                version=row.version,
                body=row.body,
                notes=row.notes,
                created_at=_iso(row.created_at) or "",
                created_by_user_id=row.created_by_user_id,
            )
            for row in rows
        ]

    @router.post(
        "/prompts/{prompt_id}/reset-to-default",
        response_model=LlmPromptTemplateDetail,
        operation_id="admin.llm.prompts.reset",
        openapi_extra=_llm_cli(
            "reset-to-default", "Reset an LLM prompt to its default", mutates=True
        ),
    )
    def reset_prompt(
        ctx: _WriteCtx, session: _Db, prompt_id: str
    ) -> LlmPromptTemplateDetail:
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_prompt_template is deployment-global (no workspace_id column).
        with tenant_agnostic():
            row = session.get(LlmPromptTemplate, prompt_id)
            if row is None:
                raise _not_found()
            default = _current_prompt_default(row.capability)
            revision = LlmPromptTemplateRevision(
                id=new_ulid(),
                template_id=row.id,
                version=row.version,
                body=row.template,
                notes=row.notes,
                created_at=_now(),
                created_by_user_id=ctx.user_id,
            )
            session.add(revision)
            row.template = default
            row.default_hash = _hash_body(default)
            row.notes = None
            row.version += 1
            row.updated_at = _now()
            _commit_or_conflict(session, "prompt_constraint_violation")
            session.refresh(row)
            count = session.scalar(
                select(func.count(LlmPromptTemplateRevision.id)).where(
                    LlmPromptTemplateRevision.template_id == prompt_id
                )
            )
        base = _prompt_response(row, int(count or 0))
        return LlmPromptTemplateDetail(
            **base.model_dump(), template=row.template, notes=row.notes
        )

    @router.get(
        "/calls",
        response_model=list[LlmCallResponse],
        operation_id="admin.llm.calls.list",
        openapi_extra=_llm_cli("calls-list", "List LLM calls", mutates=False),
    )
    def list_calls(
        _ctx: _ReadCtx,
        session: _Db,
        capability: str | None = Query(default=None),
        provider_model_id: str | None = Query(default=None),
        assignment_id: str | None = Query(default=None),
        fallback_attempts_gt: int | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[LlmCallResponse]:
        # code-health: ignore[nloc] Router owns auth, deps, and route registration.  # noqa: E501
        provider_model_filter_values: tuple[str, ...] = ()
        if provider_model_id is not None:
            # justification: deployment admin manages install-wide LLM config by
            # design; llm_provider_model is deployment-global (no workspace_id column).
            with tenant_agnostic():
                provider_model = session.get(LlmProviderModel, provider_model_id)
            provider_model_filter_values = (
                (provider_model_id, provider_model.api_model_id)
                if provider_model is not None
                else (provider_model_id,)
            )
        stmt = (
            select(LlmUsage)
            .order_by(LlmUsage.created_at.desc(), LlmUsage.id.desc())
            .limit(limit)
        )
        if capability is not None:
            stmt = stmt.where(LlmUsage.capability == capability)
        if provider_model_filter_values:
            stmt = stmt.where(
                or_(
                    *(
                        LlmUsage.provider_model_id == value
                        for value in provider_model_filter_values
                    )
                )
            )
        if assignment_id is not None:
            stmt = stmt.where(LlmUsage.assignment_id == assignment_id)
        if fallback_attempts_gt is not None:
            stmt = stmt.where(LlmUsage.fallback_attempts > fallback_attempts_gt)
        # justification: deployment-admin cross-workspace llm_usage call log, aggregated
        # across every workspace for admin observability, by design.
        with tenant_agnostic():
            rows = list(session.scalars(stmt).all())
            provider_models = list(session.scalars(select(LlmProviderModel)).all())
        provider_model_ids = {row.id for row in provider_models}
        provider_model_ids_by_api_model_id = {
            row.api_model_id: row.id for row in provider_models
        }
        return [
            LlmCallResponse(
                at=_iso(row.created_at) or "",
                capability=row.capability,
                model_id=row.provider_model_id,
                input_tokens=row.tokens_in,
                output_tokens=row.tokens_out,
                cost_usd=row.cost_usd,
                cost_cents=row.cost_cents,
                latency_ms=row.latency_ms,
                status=_status(row),
                assignment_id=row.assignment_id,
                provider_model_id=_llm_usage_provider_model_id(
                    row,
                    provider_model_ids=provider_model_ids,
                    provider_model_ids_by_api_model_id=(
                        provider_model_ids_by_api_model_id
                    ),
                ),
                fallback_attempts=row.fallback_attempts,
            )
            for row in rows
        ]

    @router.post(
        "/sync-pricing",
        response_model=LlmSyncPricingResult,
        operation_id="admin.llm.sync_pricing",
        openapi_extra=_llm_cli("sync-pricing", "Sync LLM pricing", mutates=True),
    )
    def sync_pricing(
        ctx: _WriteCtx,
        request: Request,
        session: _Db,
        payload: LlmSyncPricingPayload | None = None,
    ) -> LlmSyncPricingResult:
        started_at = _now()
        payload = payload or LlmSyncPricingPayload()
        # justification: deployment admin manages install-wide LLM config by
        # design; llm_provider_model is deployment-global (no workspace_id column).
        with tenant_agnostic():
            stmt = select(LlmProviderModel).order_by(
                LlmProviderModel.api_model_id, LlmProviderModel.id
            )
            if payload.provider_model_ids is not None:
                requested_ids = set(payload.provider_model_ids)
                stmt = stmt.where(LlmProviderModel.id.in_(requested_ids))
            rows = list(session.scalars(stmt).all())
            if payload.provider_model_ids is not None and len(rows) != len(
                set(payload.provider_model_ids)
            ):
                raise _not_found()
            models_by_id = {
                model.id: model
                for model in session.scalars(
                    select(LlmModel).where(
                        LlmModel.id.in_({row.model_id for row in rows})
                    )
                ).all()
            }
            deltas: list[LlmSyncPricingDelta] = []
            for row in rows:
                model = models_by_id.get(row.model_id)
                if model is None:
                    deltas.append(
                        LlmSyncPricingDelta(
                            provider_model_id=row.id,
                            api_model_id=row.api_model_id,
                            input_before=_money(row.input_cost_per_million),
                            input_after=_money(row.input_cost_per_million),
                            output_before=_money(row.output_cost_per_million),
                            output_after=_money(row.output_cost_per_million),
                            fixed_before=_money(row.fixed_cost_per_call_usd),
                            fixed_after=_money(row.fixed_cost_per_call_usd),
                            audio_before=_money(row.audio_cost_per_hour_usd),
                            audio_after=_money(row.audio_cost_per_hour_usd),
                            status="error",
                        )
                    )
                    continue
                try:
                    deltas.append(
                        _sync_provider_model_pricing(
                            row,
                            model,
                            now=started_at,
                            dry_run=payload.dry_run,
                        )
                    )
                except (
                    ValueError,
                    LlmProviderError,
                    LlmRateLimited,
                    LlmTransportError,
                    Validation,
                    NotFound,
                    UpstreamUnavailable,
                ):
                    deltas.append(_sync_pricing_error_delta(row, model))
            if not payload.dry_run:
                _commit_or_conflict(session, "provider_model_constraint_violation")
            if not payload.dry_run and any(
                delta.status == "updated" for delta in deltas
            ):
                _publish_deployment_defaults_changed(ctx, request)
        return LlmSyncPricingResult(
            started_at=_iso(started_at) or "",
            deltas=deltas,
            updated=sum(1 for delta in deltas if delta.status == "updated"),
            skipped=sum(
                1
                for delta in deltas
                if delta.status in {"unchanged", "skipped_not_syncable"}
            ),
            errors=sum(1 for delta in deltas if delta.status == "error"),
        )

    return router
