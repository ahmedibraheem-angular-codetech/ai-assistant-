from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import get_memory_action_service, get_memory_service
from app.memories.action_service import ActionExecution, ActionProposal, MemoryActionService
from app.memories.exceptions import (
    DuplicateMemoryKeyError,
    InvalidMemoryActionIdError,
    InvalidMemoryIdError,
    MemoryActionNotFoundError,
    MemoryActionStateError,
    MemoryNotFoundError,
    MemoryPersistenceError,
)
from app.memories.schemas import (
    MemoryActionDecision,
    MemoryActionProposeRequest,
    MemoryActionResult,
    MemoryActionView,
    MemoryCategory,
    MemoryCreate,
    MemoryListResponse,
    MemoryProposalPreview,
    MemoryRead,
    MemoryUpdate,
)
from app.memories.service import MemoryService

router = APIRouter(prefix="/memories", tags=["Memories"])


@contextmanager
def memory_http_errors():
    try:
        yield
    except MemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidMemoryIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DuplicateMemoryKeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemoryActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidMemoryActionIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MemoryActionStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemoryPersistenceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=500, detail="The memory could not be saved.") from exc


@router.get("", response_model=MemoryListResponse)
def list_memories(
    category: MemoryCategory | None = None,
    enabled: bool | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    memories: MemoryService = Depends(get_memory_service),
) -> MemoryListResponse:
    with memory_http_errors():
        items, count = memories.list_memories(
            category=category.value if category else None,
            enabled=enabled,
            limit=limit,
            offset=offset,
        )
    return MemoryListResponse(
        memories=[MemoryRead.model_validate(item) for item in items],
        count=count,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=MemoryRead, status_code=201)
def create_memory(
    payload: MemoryCreate,
    memories: MemoryService = Depends(get_memory_service),
) -> MemoryRead:
    with memory_http_errors():
        memory = memories.create(payload)
    return MemoryRead.model_validate(memory)


@router.get("/search", response_model=MemoryListResponse)
def search_memories(
    q: str = Query(min_length=1, max_length=500),
    category: MemoryCategory | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    memories: MemoryService = Depends(get_memory_service),
) -> MemoryListResponse:
    with memory_http_errors():
        items, count = memories.search(
            query=q,
            category=category.value if category else None,
            limit=limit,
            offset=offset,
        )
    return MemoryListResponse(
        memories=[MemoryRead.model_validate(item) for item in items],
        count=count,
        limit=limit,
        offset=offset,
    )


@router.post("/actions/propose", response_model=MemoryActionView)
def propose_memory_action(
    payload: MemoryActionProposeRequest,
    actions: MemoryActionService = Depends(get_memory_action_service),
) -> MemoryActionView:
    with memory_http_errors():
        proposal = actions.propose(
            payload.message,
            conversation_id=payload.conversation_id,
            limit=payload.limit,
            offset=payload.offset,
        )
    return _action_view(proposal)


@router.get("/actions/{action_id}", response_model=MemoryActionView)
def get_memory_action(
    action_id: str,
    actions: MemoryActionService = Depends(get_memory_action_service),
) -> MemoryActionView:
    with memory_http_errors():
        proposal = actions.get_action(action_id)
    return _action_view(proposal)


@router.post("/actions/{action_id}/confirm", response_model=MemoryActionResult)
def confirm_memory_action(
    action_id: str,
    payload: MemoryActionDecision,
    actions: MemoryActionService = Depends(get_memory_action_service),
) -> MemoryActionResult:
    del payload
    with memory_http_errors():
        result = actions.confirm(action_id)
    return _action_result(result)


@router.post("/actions/{action_id}/cancel", response_model=MemoryActionResult)
def cancel_memory_action(
    action_id: str,
    payload: MemoryActionDecision,
    actions: MemoryActionService = Depends(get_memory_action_service),
) -> MemoryActionResult:
    del payload
    with memory_http_errors():
        result = actions.cancel(action_id)
    return _action_result(result)


@router.get("/{memory_id}", response_model=MemoryRead)
def get_memory(memory_id: str, memories: MemoryService = Depends(get_memory_service)) -> MemoryRead:
    with memory_http_errors():
        memory = memories.get(memory_id)
    return MemoryRead.model_validate(memory)


@router.patch("/{memory_id}", response_model=MemoryRead)
def update_memory(
    memory_id: str,
    payload: MemoryUpdate,
    memories: MemoryService = Depends(get_memory_service),
) -> MemoryRead:
    with memory_http_errors():
        memory = memories.update(memory_id, payload)
    return MemoryRead.model_validate(memory)


@router.delete("/{memory_id}", status_code=204)
def delete_memory(memory_id: str, memories: MemoryService = Depends(get_memory_service)) -> Response:
    with memory_http_errors():
        memories.delete(memory_id)
    return Response(status_code=204)


@router.patch("/{memory_id}/enable", response_model=MemoryRead)
def enable_memory(memory_id: str, memories: MemoryService = Depends(get_memory_service)) -> MemoryRead:
    with memory_http_errors():
        memory = memories.set_enabled(memory_id, True)
    return MemoryRead.model_validate(memory)


@router.patch("/{memory_id}/disable", response_model=MemoryRead)
def disable_memory(memory_id: str, memories: MemoryService = Depends(get_memory_service)) -> MemoryRead:
    with memory_http_errors():
        memory = memories.set_enabled(memory_id, False)
    return MemoryRead.model_validate(memory)


def _action_view(proposal: ActionProposal) -> MemoryActionView:
    preview = None
    if proposal.proposed_memory is not None:
        preview = MemoryProposalPreview.model_validate(proposal.proposed_memory)
    return MemoryActionView(
        action_id=proposal.action_id,
        intent=proposal.intent,
        proposed_operation=proposal.operation,
        status=proposal.status,
        confirmation_required=proposal.confirmation_required,
        expires_at=proposal.expires_at,
        clarification=proposal.clarification,
        proposed_memory=preview,
        matching_memories=[MemoryRead.model_validate(item) for item in proposal.matching_memories],
        count=proposal.count,
        limit=proposal.limit,
        offset=proposal.offset,
        answer=proposal.answer,
    )


def _action_result(result: ActionExecution) -> MemoryActionResult:
    return MemoryActionResult(
        action_id=result.action_id,
        status=result.status,
        intent=result.intent,
        operation=result.operation,
        memories=[MemoryRead.model_validate(item) for item in result.memories],
        answer=result.answer,
    )
