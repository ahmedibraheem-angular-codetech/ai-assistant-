from fastapi import APIRouter, Depends

from app.api.deps import get_conversation_service
from app.api.routes.conversations import conversation_http_errors
from app.conversations.service import ConversationService
from app.memories.schemas import MemoryRead
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter()


@router.post("/chat", response_model=ChatResponse, response_model_exclude_none=True)
async def chat(
    request: ChatRequest,
    conversations: ConversationService = Depends(get_conversation_service),
):
    with conversation_http_errors():
        result = await conversations.send(
            message=request.message,
            system_prompt=request.system_prompt,
            agent_id=str(request.agent_id) if request.agent_id is not None else None,
            conversation_id=str(request.conversation_id) if request.conversation_id is not None else None,
        )
    memories = None
    if result.listed_memories is not None:
        memories = [MemoryRead.model_validate(item) for item in result.listed_memories]
    return ChatResponse(
        model=result.model,
        answer=result.answer,
        conversation_id=result.conversation_id,
        agent_id=result.agent_id,
        agent_name=result.agent_name,
        memory_intent=result.memory_intent,
        memory_action_id=result.memory_action_id,
        confirmation_required=result.confirmation_required,
        memories=memories,
        routing_decision=result.routing_decision,
        selected_agent_id=result.selected_agent_id,
        selected_agent_name=result.selected_agent_name,
        routing_reason=result.routing_reason,
        routing_confidence=result.routing_confidence,
    )
