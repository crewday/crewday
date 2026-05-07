from pydantic import BaseModel, ConfigDict


class StatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str


class VersionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: str
