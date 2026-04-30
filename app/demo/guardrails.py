"""Demo-mode guardrail declarations.

The production config module is ``app/config.py`` (not a package), so
the central demo declaration lives with the rest of the demo runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_501_NOT_IMPLEMENTED

if TYPE_CHECKING:
    from app.domain.llm.router import ModelPick

__all__ = [
    "DEMO_DISABLED_INTEGRATIONS",
    "DEMO_FREE_MODEL_ID",
    "DEMO_LIVE_LLM_CAPABILITIES",
    "DEMO_NOT_AVAILABLE_MESSAGE",
    "DEMO_WORKSPACE_CAP_CENTS_30D",
    "DemoIntegration",
    "demo_disabled_response",
    "demo_free_model_pick",
    "disabled_integration_for_path",
    "llm_capability_allowed_in_demo",
]


DEMO_FREE_MODEL_ID: Final[str] = "google/gemma-3-27b-it:free"
DEMO_WORKSPACE_CAP_CENTS_30D: Final[int] = 10
DEMO_NOT_AVAILABLE_MESSAGE: Final[str] = "Not available in demo"
DEMO_BUDGET_EXCEEDED_MESSAGE: Final[str] = (
    "Demo agents are rate-limited - load a fresh scenario to reset."
)

DEMO_LIVE_LLM_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "chat.manager",
        "chat.employee",
        "chat.compact",
        "chat.detect_language",
        "chat.translate",
        "tasks.nl_intake",
    }
)


@dataclass(frozen=True, slots=True)
class DemoIntegration:
    """One disabled integration declaration for demo mode."""

    key: str
    behaviour: str


DEMO_DISABLED_INTEGRATIONS: Final[Mapping[str, DemoIntegration]] = {
    "smtp": DemoIntegration("smtp", "null_mailer"),
    "webhooks": DemoIntegration("webhooks", "suppressed_demo"),
    "ical_polling": DemoIntegration("ical_polling", "http_501"),
    "passkeys": DemoIntegration("passkeys", "http_501"),
    "magic_links": DemoIntegration("magic_links", "http_501"),
    "api_tokens": DemoIntegration("api_tokens", "http_501"),
    "payout_manifest": DemoIntegration("payout_manifest", "http_501"),
    "ocr": DemoIntegration("ocr", "disabled_capability"),
    "voice_transcribe": DemoIntegration("voice_transcribe", "disabled_capability"),
    "daily_digest": DemoIntegration("daily_digest", "disabled_capability"),
    "anomaly_detection": DemoIntegration("anomaly_detection", "disabled_capability"),
}

_DISABLED_ROUTE_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("/auth/passkey", "passkeys"),
    ("/signup/passkey", "passkeys"),
    ("/auth/magic", "magic_links"),
    ("/auth/tokens", "api_tokens"),
    ("/me/tokens", "api_tokens"),
    ("/payout_manifest", "payout_manifest"),
    ("/ical-feeds", "ical_polling"),
)


def llm_capability_allowed_in_demo(capability: str) -> bool:
    """Return whether ``capability`` may call a live model in demo mode."""
    return capability in DEMO_LIVE_LLM_CAPABILITIES


def demo_free_model_pick(*, capability: str) -> ModelPick:
    """Return the synthetic free-tier model assignment for demo mode."""
    from app.domain.llm.router import ModelPick

    if not llm_capability_allowed_in_demo(capability):
        raise ValueError(f"LLM capability {capability!r} is disabled in demo")
    return ModelPick(
        provider_model_id=f"demo:{DEMO_FREE_MODEL_ID}",
        api_model_id=DEMO_FREE_MODEL_ID,
        max_tokens=None,
        temperature=None,
        extra_api_params={},
        required_capabilities=(),
        assignment_id=f"demo:{capability}",
    )


def disabled_integration_for_path(path: str, method: str) -> DemoIntegration | None:
    """Return the demo-disabled integration matched by an HTTP request."""
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    for marker, key in _DISABLED_ROUTE_MARKERS:
        if marker in path:
            return DEMO_DISABLED_INTEGRATIONS[key]
    return None


def demo_disabled_response(request: Request, integration: DemoIntegration) -> Response:
    """HTTP 501 response for route-level demo stubs."""
    return JSONResponse(
        {
            "error": "not_implemented_in_demo",
            "integration": integration.key,
            "message": f"{DEMO_NOT_AVAILABLE_MESSAGE}.",
            "path": request.url.path,
        },
        status_code=HTTP_501_NOT_IMPLEMENTED,
    )
