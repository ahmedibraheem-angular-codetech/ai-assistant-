class AgentRegistryError(Exception):
    """Base error for agent registry operations."""


class AgentNotFoundError(AgentRegistryError):
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent '{agent_id}' was not found.")


class DuplicateAgentNameError(AgentRegistryError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"An agent named '{name}' already exists.")


class InvalidAgentIdError(AgentRegistryError):
    def __init__(self) -> None:
        super().__init__("Agent id must be a UUID.")


class InvalidAgentDataError(AgentRegistryError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class AgentDisabledError(AgentRegistryError):
    """Raised when a caller tries to invoke an agent the operator disabled."""

    def __init__(self, agent_id: str, name: str) -> None:
        self.agent_id = agent_id
        self.name = name
        super().__init__(f"Agent '{name}' is disabled and cannot be invoked.")
