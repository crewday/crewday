from pydantic import BaseModel, ConfigDict


class StatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str


class VersionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    site_api: str
    site_web: str
