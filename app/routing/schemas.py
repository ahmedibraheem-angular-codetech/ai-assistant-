from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RouterModelOutput(BaseModel):
    """JSON returned by the local routing model. Extra keys are ignored."""

    model_config = ConfigDict(extra="ignore")

    decision: Literal["agent", "default", "clarify"]
    agent_id: str | None = None
    reason: str = Field(default="", max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("agent_id", mode="before")
    @classmethod
    def empty_agent_id(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            collapsed = value.strip()
            if collapsed.casefold() in {"", "null", "none"}:
                return None
            return collapsed
        return value

    @field_validator("reason")
    @classmethod
    def short_reason(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("agent_id")
    @classmethod
    def canonical_uuid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(UUID(value))
        except (ValueError, AttributeError, TypeError):
            return value


class RoutingDecision(BaseModel):
    """Decision after the model output has been checked against registered agents."""

    model_config = ConfigDict(extra="forbid")

    ran: bool
    decision: Literal["agent", "default", "clarify"]
    agent_id: str | None = None
    agent_name: str | None = None
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    clarification: str | None = None
