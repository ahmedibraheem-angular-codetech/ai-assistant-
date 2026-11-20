from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.permissions.engine import MODES


class PermissionModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str

    @field_validator("mode")
    @classmethod
    def known_mode(cls, value: str) -> str:
        if value not in MODES:
            raise ValueError("The permission mode is not recognized.")
        return value


class ToolEnabledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class CapabilityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class PathGrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=36, max_length=36)
    relative_path: str = Field(min_length=1, max_length=400)
    kind: str
    action: str

    @field_validator("kind")
    @classmethod
    def known_kind(cls, value: str) -> str:
        if value not in {"file", "folder"}:
            raise ValueError("The path kind is not recognized.")
        return value

    @field_validator("action")
    @classmethod
    def known_action(cls, value: str) -> str:
        if value not in {"read", "create", "write", "modify", "delete"}:
            raise ValueError("The path action is not recognized.")
        return value


class PathGrantsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grants: list[PathGrantInput] = Field(default_factory=list, max_length=50)


class PermissionEvaluateRequest(BaseModel):
    """A user explanation request. It cannot change policy or prove authorization."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = None
    tool_name: str | None = Field(default=None, max_length=64)
    resource_type: str = Field(min_length=1, max_length=32)
    resource_id: str | None = Field(default=None, max_length=200)
    action: str = Field(min_length=1, max_length=32)
    relative_path: str | None = Field(default=None, max_length=400)


class StandingApprovalCreate(BaseModel):
    """A lasting yes for reads by one agent. Changes and commands still ask."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=64)
    tool_name: str = Field(min_length=1, max_length=64)
    workspace_id: str | None = Field(default=None, max_length=64)
    path_prefix: str | None = Field(default=None, max_length=400)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)
