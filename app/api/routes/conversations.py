from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import SQLAlchemyError

from app.agents.exceptions import AgentDisabledError, AgentNotFoundError, InvalidAgentIdError
from app.api.deps import get_conversation_service
from app.memories.exceptions import (
    DuplicateMemoryKeyError,
    InvalidMemoryActionIdError,
    MemoryActionNotFoundError,
    MemoryActionStateError,
    MemoryNotFoundError,
    MemoryPersistenceError,
)
from app.conversations.exceptions import (
    AgentConversationConflictError,
    ConversationArchivedError,
    ConversationNotFoundError,
    ConversationPersistenceError,
    InvalidConversationIdError,
    ModelNotConfiguredError,
)
from app.conversations.schemas import (
    ConversationListResponse,
    ConversationRead,
    ConversationUpdate,
    MessageListResponse,
)
from app.conversations.service import ConversationService
from app.providers.ollama import OllamaError

router = APIRouter()


@contextmanager
def conversation_http_errors():
    try:
        yield
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidConversationIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InvalidAgentIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentConversationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConversationArchivedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ModelNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MemoryActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidMemoryActionIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MemoryActionStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DuplicateMemoryKeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemoryPersistenceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ConversationPersistenceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OllamaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=500, detail="The conversation could not be saved.") from exc


@router.get("/conversations", response_model=ConversationListResponse)
def list_conversations(
    include_archived: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: ConversationService = Depends(get_conversation_service),
):
    with conversation_http_errors():
        return service.list_conversations(include_archived=include_archived, limit=limit, offset=offset)


@router.get("/conversations/{conversation_id}", response_model=ConversationRead)
def get_conversation(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
):
    with conversation_http_errors():
        return service.get_conversation(conversation_id)


@router.get("/conversations/{conversation_id}/messages", response_model=MessageListResponse)
def list_messages(
    conversation_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: ConversationService = Depends(get_conversation_service),
):
    with conversation_http_errors():
        return service.list_messages(conversation_id, limit=limit, offset=offset)


@router.patch("/conversations/{conversation_id}", response_model=ConversationRead)
def update_conversation(
    conversation_id: str,
    payload: ConversationUpdate,
    service: ConversationService = Depends(get_conversation_service),
):
    with conversation_http_errors():
        return service.update_conversation(conversation_id, payload)


@router.delete("/conversations/{conversation_id}", status_code=204, response_model=None)
def delete_conversation(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
):
    with conversation_http_errors():
        service.delete_conversation(conversation_id)
    return Response(status_code=204)
