from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MemoryCategory(str, Enum):
    personal = "personal"
    professional = "professional"
    preferences = "preferences"
    goals = "goals"
    projects = "projects"
    custom = "custom"


def _collapsed(value: str) -> str:
    collapsed = " ".join(value.split())
    if not collapsed:
        raise ValueError("must not be blank")
    return collapsed


def _normalize_tags(value: list[str]) -> list[str]:
    if len(value) > 20:
        raise ValueError("at most 20 tags are allowed")
    tags: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = _collapsed(item)
        if len(tag) > 40:
            raise ValueError("tags must be at most 40 characters")
        folded = tag.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        tags.append(tag)
    return tags


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4_000)
    category: MemoryCategory
    description: str | None = Field(default=None, max_length=500)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    priority: int = Field(default=0, ge=0, le=100)

    @field_validator("key", "content")
    @classmethod
    def collapse_required(cls, value: str) -> str:
        return _collapsed(value)

    @field_validator("description")
    @classmethod
    def collapse_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = " ".join(value.split())
        return collapsed or None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        return _normalize_tags(value)


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str | None = Field(default=None, min_length=1, max_length=100)
    content: str | None = Field(default=None, min_length=1, max_length=4_000)
    category: MemoryCategory | None = None
    description: str | None = Field(default=None, max_length=500)
    tags: list[str] | None = None
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=100)

    @field_validator("key", "content")
    @classmethod
    def collapse_required(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _collapsed(value)

    @field_validator("description")
    @classmethod
    def collapse_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = " ".join(value.split())
        return collapsed or None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _normalize_tags(value)

    @model_validator(mode="after")
    def require_one_field(self) -> "MemoryUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        for field_name in self.model_fields_set:
            if field_name != "description" and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class MemoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    key: str
    content: str
    category: MemoryCategory
    description: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    enabled: bool
    priority: int


class MemoryListResponse(BaseModel):
    memories: list[MemoryRead]
    count: int
    limit: int
    offset: int


class MemoryProposalPreview(BaseModel):
    key: str
    content: str
    category: MemoryCategory
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    priority: int = 0


class MemoryActionProposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4_000)
    conversation_id: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @field_validator("message")
    @classmethod
    def collapse_message(cls, value: str) -> str:
        collapsed = " ".join(value.split())
        if not collapsed:
            raise ValueError("must not be blank")
        return collapsed

    @field_validator("conversation_id")
    @classmethod
    def conversation_must_be_uuid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from uuid import UUID

        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("conversation_id must be a UUID") from exc


class MemoryActionDecision(BaseModel):
    """Empty body. Extra fields are rejected so a client cannot change the proposal."""

    model_config = ConfigDict(extra="forbid")


class MemoryActionView(BaseModel):
    action_id: str | None = None
    intent: str
    proposed_operation: str | None = None
    status: str | None = None
    confirmation_required: bool
    expires_at: datetime | None = None
    clarification: str | None = None
    proposed_memory: MemoryProposalPreview | None = None
    matching_memories: list[MemoryRead] = Field(default_factory=list)
    count: int = 0
    limit: int = 20
    offset: int = 0
    answer: str


class MemoryActionResult(BaseModel):
    action_id: str
    status: str
    intent: str
    operation: str
    memories: list[MemoryRead]
    answer: str
