import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _collapsed_name(value: str) -> str:
    collapsed = " ".join(value.split())
    if not collapsed:
        raise ValueError("must not be blank")
    return collapsed


def _stripped(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


def _model_name(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    if any(character.isspace() for character in stripped):
        raise ValueError("must not contain whitespace")
    return stripped


class AgentCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "Research Agent",
                    "description": "Gathers sources and summarizes findings.",
                    "role": "Research and information analysis.",
                    "system_prompt": (
                        "You are a research agent. Analyze information carefully and cite uncertainty."
                    ),
                    "model": "qwen3.5:9b",
                    "enabled": True,
                }
            ]
        },
    )

    name: str = Field(
        min_length=1,
        max_length=100,
        description="Unique agent name. Comparison ignores case and extra whitespace.",
    )
    description: str = Field(min_length=1, max_length=2_000)
    role: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field(min_length=1, max_length=20_000)
    model: str = Field(
        min_length=1,
        max_length=200,
        description="LLM model assigned to this agent. Availability comes from the provider, not from the agent.",
    )
    enabled: bool = True
    allowed_tools: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def collapse_name(cls, value: str) -> str:
        return _collapsed_name(value)

    @field_validator("description", "role", "system_prompt")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return _stripped(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        return _model_name(value)

    @field_validator("allowed_tools")
    @classmethod
    def validate_allowed_tools(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str) or _TOOL_NAME.fullmatch(item) is None:
                raise ValueError("Tool names must be lowercase identifiers.")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=2_000)
    role: str | None = Field(default=None, min_length=1, max_length=200)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=20_000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def collapse_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _collapsed_name(value)

    @field_validator("description", "role", "system_prompt")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _stripped(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _model_name(value)

    @model_validator(mode="after")
    def require_one_field(self) -> "AgentUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    role: str
    system_prompt: str
    model: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class AgentListResponse(BaseModel):
    agents: list[AgentRead]
    count: int


class AvailableModel(BaseModel):
    name: str
    size: int | None = None
    modified_at: str | None = None


class AgentModelAvailability(BaseModel):
    agent_id: str
    assigned_model: str
    assigned_model_available: bool | None
    enabled: bool
    runnable: bool
    provider: str
    source: str
    availability: str
    invocation: str
    models: list[AvailableModel]
    provider_error: str | None = None
