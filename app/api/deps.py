from fastapi import Depends
from sqlalchemy.orm import Session

from app.agents.execution import AgentExecutionService
from app.agents.repository import AgentRepository
from app.agents.service import AgentService
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService
from app.core.database import get_db
from app.files.grants import AgentWorkspaceGrantRepository, AgentWorkspaceGrantService
from app.files.repository import WorkspaceRepository
from app.files.service import WorkspaceService
from app.memories.action_repository import PendingMemoryActionRepository
from app.memories.action_service import MemoryActionService
from app.memories.repository import MemoryRepository
from app.memories.service import MemoryService
from app.approvals.service import ApprovalService
from app.permissions.admin import PermissionAdmin
from app.tools.executor import ToolExecutor
from app.providers.ollama import OllamaProvider
from app.routing.service import RoutingService
from app.tools.grants import AgentToolGrantRepository
from app.tools.registry import get_tool_registry
from app.tools.service import ToolService


def get_workspace_service(session: Session = Depends(get_db)) -> WorkspaceService:
    return WorkspaceService(WorkspaceRepository(session))


def get_agent_service(session: Session = Depends(get_db)) -> AgentService:
    return AgentService(AgentRepository(session))


def get_workspace_grant_service(
    session: Session = Depends(get_db),
    agents: AgentService = Depends(get_agent_service),
) -> AgentWorkspaceGrantService:
    return AgentWorkspaceGrantService(
        AgentWorkspaceGrantRepository(session),
        WorkspaceRepository(session),
        agents,
    )


def get_tool_service(
    session: Session = Depends(get_db),
    agents: AgentService = Depends(get_agent_service),
) -> ToolService:
    return ToolService(
        get_tool_registry(),
        AgentToolGrantRepository(session),
        agents,
        workspace_grants=AgentWorkspaceGrantRepository(session),
    )


def get_permission_admin(
    session: Session = Depends(get_db),
    agents: AgentService = Depends(get_agent_service),
) -> PermissionAdmin:
    return PermissionAdmin(
        session,
        get_tool_registry(),
        agents,
        WorkspaceRepository(session),
    )


def get_approval_service(
    session: Session = Depends(get_db),
) -> ApprovalService:
    return ApprovalService(session, get_tool_registry(), ToolExecutor())


def get_agent_execution_service(
    agents: AgentService = Depends(get_agent_service),
    tools: ToolService = Depends(get_tool_service),
) -> AgentExecutionService:
    return AgentExecutionService(agents, OllamaProvider(), tools)


def get_memory_service(session: Session = Depends(get_db)) -> MemoryService:
    return MemoryService(MemoryRepository(session))


def get_memory_action_service(
    session: Session = Depends(get_db),
    memories: MemoryService = Depends(get_memory_service),
) -> MemoryActionService:
    return MemoryActionService(PendingMemoryActionRepository(session), memories)


def get_routing_service(agents: AgentService = Depends(get_agent_service)) -> RoutingService:
    return RoutingService(agents, OllamaProvider())


def get_conversation_service(
    session: Session = Depends(get_db),
    agents: AgentService = Depends(get_agent_service),
    execution: AgentExecutionService = Depends(get_agent_execution_service),
    memories: MemoryService = Depends(get_memory_service),
    memory_actions: MemoryActionService = Depends(get_memory_action_service),
    routing: RoutingService = Depends(get_routing_service),
) -> ConversationService:
    return ConversationService(
        ConversationRepository(session),
        agents,
        execution,
        memories,
        memory_actions,
        routing,
    )
