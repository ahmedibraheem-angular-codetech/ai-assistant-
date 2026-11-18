from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from app.agents.exceptions import (
    AgentDisabledError,
    AgentNotFoundError,
    DuplicateAgentNameError,
    InvalidAgentDataError,
    InvalidAgentIdError,
)
from app.agents.repository import AgentRepository
from app.agents.schemas import (
    AgentCreate,
    AgentListResponse,
    AgentModelAvailability,
    AgentRead,
    AgentUpdate,
    AvailableModel,
)

_PROVIDER_EXPLANATION = (
    "Model availability is provider-level. Each agent stores only its assigned model "
    "name; installed models belong to the LLM provider (Ollama), not to the agent. "
    "Any model listed here can be assigned to any agent. The same catalog is exposed "
    "by GET /api/v1/models."
)

_PROVIDER_UNAVAILABLE_EXPLANATION = (
    "Model availability is provider-level. Each agent stores only its assigned model "
    "name; installed models belong to the LLM provider (Ollama), not to the agent. "
    "The provider could not be reached, so the installed model list is unavailable. "
    "The assigned model was not changed."
)


def normalize_agent_name(name: str) -> str:
    collapsed = " ".join(name.split())
    if not collapsed:
        raise InvalidAgentDataError("Agent name must not be blank.")
    if len(collapsed) > 100:
        raise InvalidAgentDataError("Agent name must be at most 100 characters.")
    return collapsed


def parse_agent_id(agent_id: str) -> str:
    try:
        return str(UUID(agent_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidAgentIdError() from exc


class AgentService:
    def __init__(self, repository: AgentRepository) -> None:
        self.repository = repository

    def create(self, payload: AgentCreate) -> AgentRead:
        name = normalize_agent_name(payload.name)
        normalized = name.casefold()
        if self.repository.get_by_normalized_name(normalized) is not None:
            raise DuplicateAgentNameError(name)
        now = datetime.now(timezone.utc)
        try:
            agent = self.repository.insert(
                agent_id=str(uuid4()),
                name=name,
                name_normalized=normalized,
                description=payload.description,
                role=payload.role,
                system_prompt=payload.system_prompt,
                model=payload.model,
                enabled=payload.enabled,
                created_at=now,
                updated_at=now,
            )
        except IntegrityError as exc:
            raise DuplicateAgentNameError(name) from exc
        return AgentRead.model_validate(agent)

    def list_agents(self, enabled: bool | None = None) -> AgentListResponse:
        agents = [AgentRead.model_validate(agent) for agent in self.repository.list_agents(enabled)]
        return AgentListResponse(agents=agents, count=len(agents))

    def get(self, agent_id: str) -> AgentRead:
        parsed = parse_agent_id(agent_id)
        agent = self.repository.get(parsed)
        if agent is None:
            raise AgentNotFoundError(parsed)
        return AgentRead.model_validate(agent)

    def update(self, agent_id: str, payload: AgentUpdate) -> AgentRead:
        current = self.get(agent_id)
        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, object] = {}

        if "name" in data:
            name = normalize_agent_name(str(data["name"]))
            normalized = name.casefold()
            other = self.repository.get_by_normalized_name(normalized)
            if other is not None and other.id != current.id:
                raise DuplicateAgentNameError(name)
            changes["name"] = name
            changes["name_normalized"] = normalized

        for field in ("description", "role", "system_prompt", "model"):
            if field in data:
                changes[field] = data[field]

        if "enabled" in data:
            enabled = data["enabled"]
            if not isinstance(enabled, bool):
                raise InvalidAgentDataError("enabled must be a boolean.")
            changes["enabled"] = enabled

        if not changes:
            raise InvalidAgentDataError("At least one field must be provided.")

        try:
            updated = self.repository.apply_update(current.id, changes, datetime.now(timezone.utc))
        except IntegrityError as exc:
            raise DuplicateAgentNameError(str(changes.get("name", current.name))) from exc
        if updated is None:
            raise AgentNotFoundError(current.id)
        if changes.get("enabled") is False:
            from app.approvals.service import cancel_pending
            from app.tools.runtime import stop_file_workers

            cancel_pending(self.repository.session, agent_id=updated.id)
            self.repository.session.commit()
            stop_file_workers(agent_id=updated.id)
        return AgentRead.model_validate(updated)

    def delete(self, agent_id: str) -> None:
        parsed = parse_agent_id(agent_id)
        if not self.repository.delete(parsed):
            raise AgentNotFoundError(parsed)

    def resolve_for_invocation(self, agent_id: str) -> AgentRead:
        """Return an agent that execution is allowed to run.

        Disabled agents stay in the registry and can still be read or edited.
        They cannot be selected for invocation. Agents have no permission field
        and cannot change this flag themselves.
        """
        agent = self.get(agent_id)
        if not agent.enabled:
            raise AgentDisabledError(agent.id, agent.name)
        return agent


def build_model_availability(
    agent: AgentRead,
    models: list[dict],
    provider_error: str | None,
) -> AgentModelAvailability:
    available_models: list[AvailableModel] = []
    names: set[str] = set()
    for item in models:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        name = str(name)
        names.add(name)
        size = item.get("size")
        modified = item.get("modified_at")
        available_models.append(
            AvailableModel(
                name=name,
                size=size if isinstance(size, int) and not isinstance(size, bool) else None,
                modified_at=None if modified is None else str(modified),
            )
        )

    if provider_error:
        assigned_available = None
        availability = _PROVIDER_UNAVAILABLE_EXPLANATION
    else:
        assigned_available = agent.model in names
        availability = _PROVIDER_EXPLANATION

    if agent.enabled:
        invocation = "This agent is enabled and may be selected for invocation."
    else:
        invocation = "This agent is disabled and will not be invoked."

    return AgentModelAvailability(
        agent_id=agent.id,
        assigned_model=agent.model,
        assigned_model_available=assigned_available,
        enabled=agent.enabled,
        runnable=agent.enabled,
        provider="ollama",
        source="provider",
        availability=availability,
        invocation=invocation,
        models=available_models,
        provider_error=provider_error,
    )
