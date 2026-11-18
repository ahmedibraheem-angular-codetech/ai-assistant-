from dataclasses import dataclass

from app.agents.service import AgentService
from app.memories.context import compose_system_prompt
from app.providers.ollama import OllamaProvider
from app.tools.service import ToolService


@dataclass(frozen=True, slots=True)
class AgentExecutionContext:
    agent_id: str
    agent_name: str
    model: str
    system_prompt: str
    message: str


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    agent_id: str
    agent_name: str
    model: str
    answer: str


class AgentExecutionService:
    """Run one registered agent through the local Ollama provider."""

    def __init__(
        self,
        agents: AgentService,
        provider: OllamaProvider,
        tools: ToolService | None = None,
    ) -> None:
        self.agents = agents
        self.provider = provider
        self.tools = tools

    async def execute(
        self,
        *,
        agent_id: str,
        message: str,
        history: list[dict] | None = None,
        memory_context: str | None = None,
    ) -> AgentExecutionResult:
        agent = self.agents.resolve_for_invocation(agent_id)
        # The stored agent prompt stays the prefix. Memory is reference data only.
        prompt = compose_system_prompt(agent.system_prompt, memory_context)
        context = AgentExecutionContext(
            agent_id=agent.id,
            agent_name=agent.name,
            model=agent.model,
            system_prompt=prompt,
            message=message,
        )
        answer = await self.complete(
            model=context.model,
            message=context.message,
            system_prompt=context.system_prompt,
            history=history,
            agent_id=context.agent_id,
        )
        return AgentExecutionResult(
            agent_id=context.agent_id,
            agent_name=context.agent_name,
            model=context.model,
            answer=answer,
        )

    async def complete(
        self,
        *,
        model: str,
        message: str,
        system_prompt: str | None,
        history: list[dict] | None,
        agent_id: str | None,
    ) -> str:
        """Answer one turn. Tools run only when this caller has grants."""
        if self.tools is None:
            return await self.provider.chat(
                model=model,
                message=message,
                system_prompt=system_prompt,
                history=history,
            )
        return await self.tools.complete(
            provider=self.provider,
            model=model,
            message=message,
            system_prompt=system_prompt,
            history=history,
            agent_id=agent_id,
        )
