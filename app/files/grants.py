from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agents.service import AgentService
from app.files.exceptions import InvalidWorkspaceIdError, WorkspaceNotFoundError
from app.files.models import AgentWorkspaceGrantRecord
from app.files.repository import WorkspaceRepository
from app.files.schemas import AgentWorkspacesResponse
from app.permissions.audit import add_audit
from app.tools.runtime import stop_file_workers


class AgentWorkspaceGrantRepository:
    """The only workspace-grant code that touches SQLAlchemy."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def ids_for(self, agent_id: str) -> list[str]:
        statement = (
            select(AgentWorkspaceGrantRecord.workspace_id)
            .where(AgentWorkspaceGrantRecord.agent_id == agent_id)
            .order_by(
                AgentWorkspaceGrantRecord.position.asc(),
                AgentWorkspaceGrantRecord.workspace_id.asc(),
            )
        )
        return list(self.session.scalars(statement).all())

    def allows(self, agent_id: str, workspace_id: str) -> bool:
        statement = select(AgentWorkspaceGrantRecord.workspace_id).where(
            AgentWorkspaceGrantRecord.agent_id == agent_id,
            AgentWorkspaceGrantRecord.workspace_id == workspace_id,
        )
        return self.session.scalar(statement) is not None

    def replace(self, agent_id: str, workspace_ids: list[str]) -> None:
        self.session.execute(
            delete(AgentWorkspaceGrantRecord).where(AgentWorkspaceGrantRecord.agent_id == agent_id)
        )
        for position, workspace_id in enumerate(workspace_ids):
            self.session.add(
                AgentWorkspaceGrantRecord(
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    position=position,
                )
            )
        self.session.commit()


class AgentWorkspaceGrantService:
    def __init__(
        self,
        grants: AgentWorkspaceGrantRepository,
        workspaces: WorkspaceRepository,
        agents: AgentService,
    ) -> None:
        self.grants = grants
        self.workspaces = workspaces
        self.agents = agents

    def list_grants(self, agent_id: str) -> AgentWorkspacesResponse:
        agent = self.agents.get(agent_id)
        self.grants.session.expire_all()
        return AgentWorkspacesResponse(agent_id=agent.id, workspace_ids=self.grants.ids_for(agent.id))

    def replace(self, agent_id: str, workspace_ids: list[str]) -> AgentWorkspacesResponse:
        agent = self.agents.get(agent_id)
        self.grants.session.expire_all()
        previous = set(self.grants.ids_for(agent.id))
        parsed = self._existing_ids(workspace_ids)
        from app.approvals.service import cancel_pending

        for workspace_id in previous.difference(parsed):
            cancel_pending(self.grants.session, agent_id=agent.id, workspace_id=workspace_id)
        add_audit(
            self.grants.session,
            event="workspace_grants_replaced",
            detail=" ".join(parsed),
            agent_id=agent.id,
            resource_type="workspace",
        )
        self.grants.replace(agent.id, parsed)
        for workspace_id in previous.difference(parsed):
            stop_file_workers(agent_id=agent.id, workspace_id=workspace_id)
        return AgentWorkspacesResponse(agent_id=agent.id, workspace_ids=parsed)

    def _existing_ids(self, workspace_ids: list[str]) -> list[str]:
        parsed: list[str] = []
        for raw in workspace_ids:
            try:
                workspace_id = str(UUID(str(raw)))
            except (ValueError, AttributeError, TypeError) as exc:
                raise InvalidWorkspaceIdError() from exc
            if self.workspaces.get(workspace_id) is None:
                raise WorkspaceNotFoundError()
            if workspace_id not in parsed:
                parsed.append(workspace_id)
        return parsed
