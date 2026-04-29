"""Bare-host runtime information for the SPA shell."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.capabilities import Capabilities

__all__ = ["RuntimeInfoResponse", "RuntimeInfoRuntime", "router"]


class RuntimeInfoRuntime(BaseModel):
    """Runtime-mode flags exposed to the SPA."""

    demo_mode: bool


class RuntimeInfoResponse(BaseModel):
    """Body of ``GET /api/v1/runtime/info``."""

    runtime: RuntimeInfoRuntime


router = APIRouter(tags=["runtime"])


@router.get(
    "/runtime/info",
    response_model=RuntimeInfoResponse,
    operation_id="runtime.info",
    summary="Get runtime deployment flags",
)
def runtime_info(request: Request) -> RuntimeInfoResponse:
    capabilities: Capabilities | None = getattr(request.app.state, "capabilities", None)
    return RuntimeInfoResponse(
        runtime=RuntimeInfoRuntime(
            demo_mode=bool(capabilities and capabilities.has("runtime.demo_mode")),
        )
    )
