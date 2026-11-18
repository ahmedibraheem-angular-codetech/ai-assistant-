import json
import re
from dataclasses import dataclass

from app.tools.exceptions import ToolValidationError

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_INVALID = "The tool request was not valid."


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict


@dataclass(frozen=True, slots=True)
class ParsedToolRequest:
    calls: tuple[ToolCall, ...]
    error: str | None
    signature: str


def parse_tool_request(content: str) -> ParsedToolRequest | None:
    """Read a structured tool request.

    Plain text is a final answer. Agent ids, permissions, and confirmation
    flags in the model JSON are ignored.
    """
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if "tool_calls" in payload:
        raw_calls = payload.get("tool_calls")
    elif "name" in payload:
        raw_calls = [payload]
    else:
        return None
    signature = json.dumps(payload, sort_keys=True, default=str)
    if not isinstance(raw_calls, list) or not raw_calls:
        return ParsedToolRequest((), _INVALID, signature)
    calls: list[ToolCall] = []
    for item in raw_calls:
        if not isinstance(item, dict):
            return ParsedToolRequest((), _INVALID, signature)
        name = item.get("name")
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            return ParsedToolRequest((), _INVALID, signature)
        arguments = item.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return ParsedToolRequest((), _INVALID, signature)
        calls.append(ToolCall(name=name, arguments=arguments))
    return ParsedToolRequest(tuple(calls), None, signature)


def argument_error(exc: ToolValidationError) -> str:
    message = str(exc).strip()
    return message or _INVALID
