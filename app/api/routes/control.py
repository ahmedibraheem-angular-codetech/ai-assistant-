"""Assistant controls for the local user: pause/resume and agent budgets.

Admin routes. Tools and model payloads cannot call them.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agents.exceptions import AgentNotFoundError, InvalidAgentIdError
from app.agents.service import AgentService
from app.api.deps import get_agent_service
from app.api.routes.permissions import require_local_admin
from app.core import controls
from app.core.database import get_db

router = APIRouter()


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=200)


class BudgetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_tool_calls: int | None = Field(default=None, ge=1, le=100_000)
    daily_chat_turns: int | None = Field(default=None, ge=1, le=100_000)


@router.get("/control")
def read_control(_local: None = Depends(require_local_admin)):
    return controls.pause_state()


@router.post("/control/pause")
def pause(
    payload: PauseRequest | None = None,
    _local: None = Depends(require_local_admin),
    session: Session = Depends(get_db),
):
    return controls.set_paused(session, True, None if payload is None else payload.reason)


@router.post("/control/resume")
def resume(_local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    return controls.set_paused(session, False)


def _agent_id(agents: AgentService, agent_id: str) -> str:
    try:
        return agents.get(agent_id).id
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidAgentIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/control/agents/{agent_id}/budget")
def read_budget(
    agent_id: str,
    _local: None = Depends(require_local_admin),
    agents: AgentService = Depends(get_agent_service),
):
    return controls.usage_for(_agent_id(agents, agent_id))


@router.put("/control/agents/{agent_id}/budget")
def update_budget(
    agent_id: str,
    payload: BudgetUpdate,
    _local: None = Depends(require_local_admin),
    agents: AgentService = Depends(get_agent_service),
    session: Session = Depends(get_db),
):
    canonical = _agent_id(agents, agent_id)
    return controls.set_budget(session, canonical, controls.Budget(**payload.model_dump()))


@router.delete("/control/agents/{agent_id}/budget")
def clear_budget(
    agent_id: str,
    _local: None = Depends(require_local_admin),
    agents: AgentService = Depends(get_agent_service),
    session: Session = Depends(get_db),
):
    return controls.set_budget(session, _agent_id(agents, agent_id), None)


class TeamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_ids: list[str] = Field(default_factory=list, max_length=16)


@router.get("/agents/{agent_id}/team")
def read_team(
    agent_id: str,
    _local: None = Depends(require_local_admin),
    agents: AgentService = Depends(get_agent_service),
    session: Session = Depends(get_db),
):
    from app.agents.team import team_of

    manager = _agent_id(agents, agent_id)
    return {"agent_id": manager, "member_ids": team_of(session, manager)}


@router.put("/agents/{agent_id}/team")
def update_team(
    agent_id: str,
    payload: TeamUpdate,
    _local: None = Depends(require_local_admin),
    agents: AgentService = Depends(get_agent_service),
    session: Session = Depends(get_db),
):
    """Members the agent may delegate to. They keep their own permissions."""
    from app.agents.team import TeamError, replace_team

    manager = _agent_id(agents, agent_id)
    members = [_agent_id(agents, member) for member in payload.member_ids]
    try:
        return {"agent_id": manager, "member_ids": replace_team(session, manager, members)}
    except TeamError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ------------------------------------------------------------- hiring studio


class HireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=40)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_ids: list[str] = Field(default_factory=list, max_length=20)
    extra_instructions: str | None = Field(default=None, max_length=4_000)
    apply_budget: bool = True


@router.get("/agent-templates")
def list_templates(_local: None = Depends(require_local_admin)):
    from app.agents.templates import TEMPLATES

    items = [template.public() for template in TEMPLATES.values()]
    return {"templates": items, "count": len(items)}


@router.post("/agents/hire", status_code=201)
def hire_agent(
    payload: HireRequest,
    _local: None = Depends(require_local_admin),
    agents: AgentService = Depends(get_agent_service),
    session: Session = Depends(get_db),
):
    """Create an agent from a template, grant only the template's tools."""
    from app.agents.exceptions import DuplicateAgentNameError
    from app.agents.schemas import AgentCreate
    from app.agents.templates import TEMPLATES
    from app.core.config import get_settings
    from app.files.exceptions import InvalidWorkspaceIdError, WorkspaceNotFoundError
    from app.files.grants import AgentWorkspaceGrantRepository, AgentWorkspaceGrantService
    from app.files.repository import WorkspaceRepository
    from app.tools.grants import AgentToolGrantRepository
    from app.tools.registry import get_tool_registry
    from app.tools.service import ToolService

    template = TEMPLATES.get(payload.template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="That template does not exist.")
    model = (payload.model or get_settings().ollama_model or "").strip()
    if not model:
        raise HTTPException(status_code=422, detail="Choose a model or set OLLAMA_MODEL first.")
    prompt = template.system_prompt
    if payload.extra_instructions and payload.extra_instructions.strip():
        prompt = f"{prompt}\n\nExtra instructions from the user:\n{payload.extra_instructions.strip()}"
    registry = get_tool_registry()
    granted = [name for name in template.tools if registry.get(name) is not None]
    try:
        agent = agents.create(
            AgentCreate(
                name=payload.name or template.name,
                description=template.description,
                role=template.role,
                system_prompt=prompt,
                model=model,
                enabled=True,
            )
        )
    except DuplicateAgentNameError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    tools = ToolService(
        registry,
        AgentToolGrantRepository(session),
        agents,
        workspace_grants=AgentWorkspaceGrantRepository(session),
    )
    tools.replace_grants(agent.id, granted)
    workspaces: list[str] = []
    if payload.workspace_ids:
        grants = AgentWorkspaceGrantService(
            AgentWorkspaceGrantRepository(session), WorkspaceRepository(session), agents
        )
        try:
            workspaces = list(grants.replace(agent.id, payload.workspace_ids).workspace_ids)
        except (InvalidWorkspaceIdError, WorkspaceNotFoundError) as exc:
            agents.delete(agent.id)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    budget = None
    if payload.apply_budget and (template.daily_tool_calls or template.daily_chat_turns):
        budget = controls.set_budget(
            session,
            agent.id,
            controls.Budget(
                daily_tool_calls=template.daily_tool_calls,
                daily_chat_turns=template.daily_chat_turns,
            ),
        )
    return {
        "agent": agent.model_dump(mode="json"),
        "template_id": template.id,
        "granted_tools": granted,
        "workspace_ids": workspaces,
        "budget": budget,
        "standing_approvals": [],
        "next_steps": list(template.notes),
    }
