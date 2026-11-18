from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db

from app.agents.exceptions import AgentDisabledError, AgentNotFoundError, InvalidAgentIdError
from app.files.exceptions import FileAccessError
from app.api.deps import get_tool_service
from app.tools.exceptions import (
    ToolApprovalRequired,
    ToolDisabledError,
    ToolExecutionDisabledError,
    ToolNotFoundError,
    ToolPermissionError,
    ToolValidationError,
    UnknownToolNameError,
)
from app.tools.schemas import (
    AgentToolsResponse,
    AgentToolsUpdate,
    ToolListResponse,
    ToolRead,
    ToolTestRequest,
    ToolTestResponse,
)
from app.tools.service import ToolService

router = APIRouter()


@contextmanager
def tool_http_errors():
    try:
        yield
    except ToolNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnknownToolNameError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ToolPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ToolApprovalRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ToolDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ToolExecutionDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ToolValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidAgentIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/tools", response_model=ToolListResponse)
def list_tools(service: ToolService = Depends(get_tool_service)):
    tools = service.list_tools()
    return ToolListResponse(tools=tools, count=len(tools))


@router.get("/tools/{name}", response_model=ToolRead)
def get_tool(name: str, service: ToolService = Depends(get_tool_service)):
    with tool_http_errors():
        return service.get_tool(name)


@router.post("/tools/{name}/test", response_model=ToolTestResponse, response_model_exclude_none=True)
async def test_tool(
    name: str,
    payload: ToolTestRequest,
    session: Session = Depends(get_db),
    service: ToolService = Depends(get_tool_service),
):
    try:
        with tool_http_errors():
            outcome = await service.test(name, payload.arguments, payload.agent_id)
    except HTTPException:
        session.commit()
        raise
    session.commit()
    return ToolTestResponse(
        tool=outcome.tool,
        status=outcome.status,
        output=outcome.output,
        error=outcome.error,
    )


@router.get("/agents/{agent_id}/tools", response_model=AgentToolsResponse)
def list_agent_tools(agent_id: str, service: ToolService = Depends(get_tool_service)):
    with tool_http_errors():
        return service.describe_agent(agent_id)


@router.put("/agents/{agent_id}/tools", response_model=AgentToolsResponse)
def replace_agent_tools(
    agent_id: str,
    payload: AgentToolsUpdate,
    service: ToolService = Depends(get_tool_service),
):
    with tool_http_errors():
        return service.replace_grants(agent_id, payload.allowed_tools)
