import os

from fastapi import APIRouter

from site_api import __version__
from site_api.models import StatusResponse, VersionResponse

router = APIRouter()


@router.get("/healthz", response_model=StatusResponse)
def healthz() -> StatusResponse:
    return StatusResponse(status="ok")


@router.get("/readyz", response_model=StatusResponse)
def readyz() -> StatusResponse:
    return StatusResponse(status="ok")


@router.get("/version", response_model=VersionResponse)
def version() -> VersionResponse:
    return VersionResponse(
        site_api=os.environ.get("SITE_API_VERSION", __version__),
        site_web=os.environ.get("SITE_WEB_VERSION", __version__),
    )
