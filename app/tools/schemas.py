import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def normalize_tool_names(value: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or _TOOL_NAME.fullmatch(item) is None:
            raise ValueError("Tool names must be lowercase identifiers.")
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


class ToolRead(BaseModel):
    name: str
    description: str
    category: str
    enabled: bool
    requires_confirmation: bool
    timeout_seconds: float
    input_schema: dict[str, Any]


class ToolListResponse(BaseModel):
    tools: list[ToolRead]
    count: int


class AgentToolsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_tools: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("allowed_tools")
    @classmethod
    def validate_names(cls, value: list[str]) -> list[str]:
        return normalize_tool_names(value)


class AgentToolsResponse(BaseModel):
    agent_id: str
    allowed_tools: list[str]
    tools: list[ToolRead]


class ToolTestRequest(BaseModel):
    """A direct tool test. Confirmation cannot be supplied by the caller."""

    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None


class ToolTestResponse(BaseModel):
    tool: str
    status: str
    output: dict[str, Any] | None = None
    error: str | None = None
