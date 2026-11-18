"""Local permission administration.

These routes are for the user on this machine. They are not tools, and a
model payload cannot call them. A non-local client is rejected.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.agents.exceptions import AgentNotFoundError, InvalidAgentIdError
from app.api.deps import get_permission_admin
from app.files.exceptions import InvalidWorkspaceIdError, WorkspaceNotFoundError
from app.permissions.admin import PermissionAdmin
from app.permissions.schemas import (
    CapabilityUpdate,
    PathGrantsUpdate,
    PermissionEvaluateRequest,
    PermissionModeUpdate,
    ToolEnabledUpdate,
)
from app.tools.exceptions import ToolNotFoundError

router = APIRouter()

_LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


def require_local_admin(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in _LOCAL:
        raise HTTPException(status_code=403, detail="Permission changes are only available on localhost.")


@router.get("/permissions")
def read_permissions(
    _local: None = Depends(require_local_admin),
    admin: PermissionAdmin = Depends(get_permission_admin),
):
    return admin.configuration()


@router.put("/permissions/mode")
def update_mode(
    payload: PermissionModeUpdate,
    _local: None = Depends(require_local_admin),
    admin: PermissionAdmin = Depends(get_permission_admin),
):
    return admin.set_mode(payload.mode)


@router.get("/permissions/tools")
def list_manifests(
    _local: None = Depends(require_local_admin),
    admin: PermissionAdmin = Depends(get_permission_admin),
):
    tools = admin.manifests()
    return {"tools": tools, "count": len(tools)}


@router.put("/permissions/tools/{name}")
def update_tool(
    name: str,
    payload: ToolEnabledUpdate,
    _local: None = Depends(require_local_admin),
    admin: PermissionAdmin = Depends(get_permission_admin),
):
    try:
        return admin.set_tool_enabled(name, payload.enabled)
    except ToolNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/permissions/capabilities/{name}")
def update_capability(
    name: str,
    payload: CapabilityUpdate,
    _local: None = Depends(require_local_admin),
    admin: PermissionAdmin = Depends(get_permission_admin),
):
    try:
        return admin.set_capability(name, payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="That capability is not recognized.") from exc


@router.get("/permissions/agents/{agent_id}")
def effective_permissions(
    agent_id: str,
    _local: None = Depends(require_local_admin),
    admin: PermissionAdmin = Depends(get_permission_admin),
):
    try:
        return admin.effective(agent_id)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidAgentIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/permissions/agents/{agent_id}/paths")
def replace_paths(
    agent_id: str,
    payload: PathGrantsUpdate,
    _local: None = Depends(require_local_admin),
    admin: PermissionAdmin = Depends(get_permission_admin),
):
    try:
        return admin.replace_paths(agent_id, [item.model_dump() for item in payload.grants])
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidAgentIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (InvalidWorkspaceIdError, WorkspaceNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/permissions/evaluate")
def evaluate_permission(
    payload: PermissionEvaluateRequest,
    _local: None = Depends(require_local_admin),
    admin: PermissionAdmin = Depends(get_permission_admin),
):
    return admin.explain(
        agent_id=payload.agent_id,
        tool_name=payload.tool_name,
        action=payload.action,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        relative_path=payload.relative_path,
    )
