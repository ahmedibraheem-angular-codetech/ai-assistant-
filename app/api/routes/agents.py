from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.agents.exceptions import (
    AgentDisabledError,
    AgentNotFoundError,
    DuplicateAgentNameError,
    InvalidAgentDataError,
    InvalidAgentIdError,
)
from app.agents.schemas import AgentCreate, AgentListResponse, AgentModelAvailability, AgentRead, AgentUpdate
from app.agents.service import AgentService, build_model_availability
from app.api.deps import get_agent_service, get_tool_service
from app.providers.ollama import OllamaError, OllamaProvider
from app.tools.exceptions import UnknownToolNameError
from app.tools.service import ToolService

router = APIRouter()


@contextmanager
def agent_http_errors():
    try:
        yield
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DuplicateAgentNameError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidAgentIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InvalidAgentDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/agents", response_model=AgentListResponse)
def list_agents(
    enabled: bool | None = Query(default=None),
    service: AgentService = Depends(get_agent_service),
):
    with agent_http_errors():
        return service.list_agents(enabled=enabled)


@router.post("/agents", response_model=AgentRead, status_code=201)
def create_agent(
    payload: AgentCreate,
    service: AgentService = Depends(get_agent_service),
    tools: ToolService = Depends(get_tool_service),
):
    with agent_http_errors():
        try:
            if payload.allowed_tools:
                tools.ensure_known(payload.allowed_tools)
            agent = service.create(payload)
            if payload.allowed_tools:
                tools.replace_grants(agent.id, payload.allowed_tools)
            return agent
        except UnknownToolNameError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/agents/{agent_id}", response_model=AgentRead)
def get_agent(agent_id: str, service: AgentService = Depends(get_agent_service)):
    with agent_http_errors():
        return service.get(agent_id)


@router.patch("/agents/{agent_id}", response_model=AgentRead)
def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    service: AgentService = Depends(get_agent_service),
):
    with agent_http_errors():
        return service.update(agent_id, payload)


@router.delete("/agents/{agent_id}", status_code=204, response_model=None)
def delete_agent(agent_id: str, service: AgentService = Depends(get_agent_service)):
    with agent_http_errors():
        service.delete(agent_id)
    return Response(status_code=204)


@router.get("/agents/{agent_id}/models", response_model=AgentModelAvailability)
async def list_agent_models(agent_id: str, service: AgentService = Depends(get_agent_service)):
    with agent_http_errors():
        agent = service.get(agent_id)

    provider_error = None
    models: list[dict] = []
    try:
        payload = await OllamaProvider().list_models()
        models = list(payload.get("models", []))
    except OllamaError as exc:
        provider_error = str(exc)
    return build_model_availability(agent, models, provider_error)
