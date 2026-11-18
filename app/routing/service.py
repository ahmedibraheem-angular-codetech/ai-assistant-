"""Turn a routing-model response into a registered agent or the default assistant."""

import re

from app.agents.exceptions import AgentDisabledError, AgentNotFoundError, InvalidAgentIdError
from app.agents.schemas import AgentRead
from app.agents.service import AgentService
from app.core.config import Settings
from app.providers.ollama import OllamaProvider
from app.routing.exceptions import InvalidRoutingResponseError
from app.routing.router import AgentRouter
from app.routing.schemas import RoutingDecision

_GREETINGS = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "hiya",
        "good morning",
        "good afternoon",
        "good evening",
        "thanks",
        "thank you",
        "thank you very much",
    }
)
_TOKEN = re.compile(r"[a-z0-9]+")
_INVALID = "The routing response was invalid, so the default assistant was used."
_UNAVAILABLE = "The selected agent is not available, so the default assistant was used."
_LOW_CONFIDENCE = "The routing confidence was below the threshold, so the default assistant was used."
_SINGLE_KEYWORD = "One keyword is not enough to choose a specialist, so the default assistant was used."
_GREETING = "This is a greeting, so the default assistant will answer."


class RoutingService:
    def __init__(self, agents: AgentService, provider: OllamaProvider) -> None:
        self.agents = agents
        self.router = AgentRouter(provider)

    def can_route(self, settings: Settings) -> bool:
        if not settings.auto_agent_routing_enabled:
            return False
        if not _routing_model(settings):
            return False
        return self.agents.list_agents(enabled=True).count > 0

    async def choose(
        self,
        *,
        message: str,
        history: list[dict[str, str]] | None,
        settings: Settings,
    ) -> RoutingDecision:
        if not self.can_route(settings):
            return _skipped()
        agents = self.agents.list_agents(enabled=True).agents
        if _is_greeting(message):
            return RoutingDecision(
                ran=True,
                decision="default",
                reason=_GREETING,
                confidence=1.0,
            )
        try:
            parsed = await self.router.complete(
                model=_routing_model(settings),
                message=message,
                agents=agents,
                history=history,
                timeout_seconds=settings.agent_routing_timeout_seconds,
            )
        except InvalidRoutingResponseError:
            return _default(_INVALID, ran=True)
        return self._accept(parsed.decision, parsed.agent_id, parsed.reason, parsed.confidence, agents, message, settings)

    def _accept(
        self,
        decision: str,
        agent_token: str | None,
        reason: str,
        confidence: float,
        agents: list[AgentRead],
        message: str,
        settings: Settings,
    ) -> RoutingDecision:
        short_reason = _short(reason) or "The default assistant will answer."
        if decision == "agent":
            if len(_TOKEN.findall(message)) == 1:
                return _default(_SINGLE_KEYWORD, ran=True)
            if confidence < settings.agent_routing_confidence_threshold:
                return _default(_LOW_CONFIDENCE, ran=True, confidence=confidence)
            agent = _match_agent(agent_token, agents)
            if agent is None:
                return _default(_UNAVAILABLE, ran=True, confidence=confidence)
            try:
                confirmed = self.agents.resolve_for_invocation(agent.id)
            except (AgentNotFoundError, AgentDisabledError, InvalidAgentIdError):
                return _default(_UNAVAILABLE, ran=True, confidence=confidence)
            return RoutingDecision(
                ran=True,
                decision="agent",
                agent_id=confirmed.id,
                agent_name=confirmed.name,
                reason=short_reason,
                confidence=confidence,
            )
        if decision == "clarify" and confidence >= settings.agent_routing_confidence_threshold:
            question = short_reason or "Which kind of help do you want?"
            return RoutingDecision(
                ran=True,
                decision="clarify",
                reason=question,
                confidence=confidence,
                clarification=(
                    f"{question} Tell me whether you want development, research, "
                    "business, personal organization, or a general answer."
                ),
            )
        if decision == "clarify":
            return _default(_LOW_CONFIDENCE, ran=True, confidence=confidence)
        return RoutingDecision(ran=True, decision="default", reason=short_reason, confidence=confidence)


def _match_agent(token: str | None, agents: list[AgentRead]) -> AgentRead | None:
    if not token:
        return None
    folded = token.casefold()
    for agent in agents:
        if agent.id == token or agent.name.casefold() == folded:
            return agent
    return None


def _routing_model(settings: Settings) -> str:
    return (settings.agent_routing_model or settings.ollama_model).strip()


def _is_greeting(message: str) -> bool:
    return message.casefold().strip(" !.?,") in _GREETINGS


def _short(reason: str) -> str:
    return " ".join(reason.split())[:180]


def _skipped() -> RoutingDecision:
    return RoutingDecision(ran=False, decision="default", reason="", confidence=0.0)


def _default(reason: str, *, ran: bool, confidence: float = 0.0) -> RoutingDecision:
    return RoutingDecision(ran=ran, decision="default", reason=reason, confidence=confidence)
