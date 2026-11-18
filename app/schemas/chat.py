from uuid import UUID

from pydantic import BaseModel, Field

from app.memories.schemas import MemoryRead


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    system_prompt: str | None = Field(default=None, max_length=10_000)
    agent_id: UUID | None = Field(
        default=None,
        description="Registered agent to run. When omitted, chat uses OLLAMA_MODEL.",
    )
    conversation_id: UUID | None = Field(
        default=None,
        description="Existing conversation to continue. When omitted, a new conversation is created.",
    )


class ChatResponse(BaseModel):
    model: str
    answer: str
    conversation_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    memory_intent: str | None = None
    memory_action_id: str | None = None
    confirmation_required: bool | None = None
    memories: list[MemoryRead] | None = None
    routing_decision: str | None = None
    selected_agent_id: str | None = None
    selected_agent_name: str | None = None
    routing_reason: str | None = None
    routing_confidence: float | None = None
