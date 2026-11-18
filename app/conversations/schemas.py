from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    agent_id: str | None
    created_at: datetime
    updated_at: datetime
    is_archived: bool
    message_count: int


class ConversationListResponse(BaseModel):
    conversations: list[ConversationRead]
    count: int
    limit: int
    offset: int


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    is_archived: bool | None = None

    @field_validator("title")
    @classmethod
    def collapse_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = " ".join(value.split())
        if not collapsed:
            raise ValueError("must not be blank")
        return collapsed

    @model_validator(mode="after")
    def require_one_field(self) -> "ConversationUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    role: str
    content: str
    agent_id: str | None
    model: str | None
    created_at: datetime
    sequence_number: int


class MessageListResponse(BaseModel):
    messages: list[MessageRead]
    count: int
    limit: int
    offset: int
