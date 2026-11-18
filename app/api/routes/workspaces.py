from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Response

from app.agents.exceptions import AgentDisabledError, AgentNotFoundError, InvalidAgentIdError
from app.api.deps import get_workspace_grant_service, get_workspace_service
from app.files.exceptions import (
    DuplicateWorkspaceNameError,
    DuplicateWorkspaceRootError,
    InvalidWorkspaceIdError,
    WorkspaceApprovalError,
    WorkspaceError,
    WorkspaceLimitError,
    WorkspaceNotFoundError,
    WorkspacePathError,
    WorkspacePermissionError,
)
from app.files.schemas import (
    AgentWorkspacesResponse,
    AgentWorkspacesUpdate,
    WorkspaceCreate,
    WorkspaceListResponse,
    WorkspaceRead,
    WorkspaceUpdate,
)
from app.files.grants import AgentWorkspaceGrantService
from app.files.service import WorkspaceService

router = APIRouter()


@contextmanager
def workspace_http_errors():
    try:
        yield
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidWorkspaceIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WorkspaceApprovalError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WorkspacePathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WorkspacePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DuplicateWorkspaceNameError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DuplicateWorkspaceRootError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkspaceLimitError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkspaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidAgentIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/workspaces", response_model=WorkspaceListResponse)
def list_workspaces(service: WorkspaceService = Depends(get_workspace_service)):
    with workspace_http_errors():
        return service.list_workspaces()


@router.post("/workspaces", response_model=WorkspaceRead, status_code=201)
def create_workspace(payload: WorkspaceCreate, service: WorkspaceService = Depends(get_workspace_service)):
    with workspace_http_errors():
        return service.create(payload)


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceRead)
def get_workspace(workspace_id: str, service: WorkspaceService = Depends(get_workspace_service)):
    with workspace_http_errors():
        return service.get(workspace_id)


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceRead)
def update_workspace(
    workspace_id: str,
    payload: WorkspaceUpdate,
    service: WorkspaceService = Depends(get_workspace_service),
):
    with workspace_http_errors():
        return service.update(workspace_id, payload)


@router.get("/agents/{agent_id}/workspaces", response_model=AgentWorkspacesResponse)
def list_agent_workspaces(
    agent_id: str,
    service: AgentWorkspaceGrantService = Depends(get_workspace_grant_service),
):
    with workspace_http_errors():
        return service.list_grants(agent_id)


@router.put("/agents/{agent_id}/workspaces", response_model=AgentWorkspacesResponse)
def replace_agent_workspaces(
    agent_id: str,
    payload: AgentWorkspacesUpdate,
    service: AgentWorkspaceGrantService = Depends(get_workspace_grant_service),
):
    with workspace_http_errors():
        return service.replace(agent_id, payload.workspace_ids)


@router.delete("/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str, service: WorkspaceService = Depends(get_workspace_service)):
    with workspace_http_errors():
        service.delete(workspace_id)
    return Response(status_code=204)
