from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.schemas import _collapsed_name


class WorkspaceCreate(BaseModel):
    """Explicit approval of one directory. The scope is read-only and cannot be widened."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=1024)
    confirm_read_only: bool

    @field_validator("name")
    @classmethod
    def collapse_name(cls, value: str) -> str:
        return _collapsed_name(value)

    @field_validator("path")
    @classmethod
    def strip_path(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class WorkspaceUpdate(BaseModel):
    """Rename or disable a registration. The approved directory cannot be changed here."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def collapse_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _collapsed_name(value)

    @model_validator(mode="after")
    def require_one_field(self) -> "WorkspaceUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class WorkspaceRead(BaseModel):
    id: str
    name: str
    root_path: str
    access_scope: str
    enabled: bool
    approval: str
    created_at: datetime
    updated_at: datetime


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceRead]
    count: int


class AgentWorkspacesUpdate(BaseModel):
    """Replace the workspaces one agent may read. This does not grant writes."""

    model_config = ConfigDict(extra="forbid")

    workspace_ids: list[str] = Field(default_factory=list, max_length=50)


class AgentWorkspacesResponse(BaseModel):
    agent_id: str
    workspace_ids: list[str]
