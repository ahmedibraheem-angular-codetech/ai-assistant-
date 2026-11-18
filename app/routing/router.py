"""Ask the local Ollama model which registered agent should answer."""

import json
import re

from pydantic import ValidationError

from app.agents.schemas import AgentRead
from app.providers.ollama import OllamaError, OllamaProvider
from app.routing.exceptions import InvalidRoutingResponseError
from app.routing.schemas import RouterModelOutput

ROUTING_SYSTEM_PROMPT = """You are the local agent router.
Choose who should answer the user's latest message.
Return only one JSON object and no other text:
{"decision":"agent | default | clarify","agent_id":"UUID or null","reason":"Short explanation","confidence":0.0}

Rules:
- decision is agent, default, or clarify.
- agent_id must be one of the ids listed below, or null.
- Never invent an agent id or name.
- Greetings and general questions use default.
- Development, code, debugging, and implementation questions use the developer agent when one is listed.
- Research and fact-finding requests use the research agent when one is listed.
- Business planning and commercial strategy use the business agent when one is listed.
- Personal organization and personal assistance use the personal agent when one is listed.
- Do not select an agent because of one ambiguous keyword.
- If the request could reasonably belong to more than one specialist, use clarify.
- confidence is a number from 0 to 1.
- reason is one short sentence. Do not include private reasoning.
"""

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class AgentRouter:
    def __init__(self, provider: OllamaProvider) -> None:
        self.provider = provider

    async def complete(
        self,
        *,
        model: str,
        message: str,
        agents: list[AgentRead],
        history: list[dict[str, str]] | None,
        timeout_seconds: float,
    ) -> RouterModelOutput:
        prompt = _user_prompt(message, history)
        previous_timeout = getattr(self.provider, "timeout", None)
        if previous_timeout is not None:
            self.provider.timeout = timeout_seconds
        try:
            raw = await self.provider.chat(
                model=model,
                message=prompt,
                system_prompt=ROUTING_SYSTEM_PROMPT + "\nEnabled agents:\n" + _catalog(agents),
            )
        except OllamaError as exc:
            raise InvalidRoutingResponseError() from exc
        finally:
            if previous_timeout is not None:
                self.provider.timeout = previous_timeout
        try:
            return RouterModelOutput.model_validate(_extract_json(raw))
        except (InvalidRoutingResponseError, ValidationError, ValueError) as exc:
            raise InvalidRoutingResponseError() from exc


def _catalog(agents: list[AgentRead]) -> str:
    lines: list[str] = []
    for agent in agents:
        lines.append(
            f"- id: {agent.id}; name: {agent.name}; role: {agent.role}; description: {agent.description}"
        )
    return "\n".join(lines)


def _user_prompt(message: str, history: list[dict[str, str]] | None) -> str:
    parts = [f"Latest message:\n{message.strip()}"]
    if history:
        lines = []
        for item in history[-6:]:
            content = " ".join(str(item.get("content", "")).split())[:300]
            if not content:
                continue
            lines.append(f"{item.get('role', 'user')}: {content}")
        if lines:
            parts.append("Recent conversation:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _extract_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = _JSON_FENCE.sub("", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise InvalidRoutingResponseError() from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise InvalidRoutingResponseError() from exc
    if not isinstance(parsed, dict):
        raise InvalidRoutingResponseError()
    return parsed
