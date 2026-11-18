"""Newline-delimited JSON-RPC for MCP 2025-06-18.

The module frames messages and classifies them. It does not spawn a process,
call a tool, or read a resource.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "personal-ai-assistant"
CLIENT_VERSION = "0.1.0"
MAX_LINE_BYTES = 1024 * 1024
JSONRPC = "2.0"

STARTUP_METHODS = frozenset({"initialize", "notifications/initialized"})
PUBLIC_METHODS = frozenset({"ping", "notifications/cancelled"})
DENIED_METHODS = frozenset({"tools/call", "resources/read"})
LIST_CHANGED = frozenset(
    {"notifications/tools/list_changed", "notifications/resources/list_changed"}
)

Kind = Literal["request", "notification", "response"]


class ProtocolError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The MCP message is not valid.")


@dataclass(frozen=True, slots=True)
class Envelope:
    kind: Kind
    method: str | None
    request_id: object
    result: object
    error: object
    params: object


def client_allowed(method: str, *, startup: bool) -> bool:
    """Return whether this process may write the method. Denied methods are never allowed."""
    if method in DENIED_METHODS or not method or "\x00" in method or "\n" in method:
        return False
    if startup and method in STARTUP_METHODS:
        return True
    return method in PUBLIC_METHODS


def encode_message(payload: dict) -> bytes:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if b"\n" in raw or len(raw) + 1 > MAX_LINE_BYTES:
        raise ProtocolError("oversize")
    return raw + b"\n"


def initialize_request(request_id: str) -> dict:
    return {
        "jsonrpc": JSONRPC,
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        },
    }


def initialized_notification() -> dict:
    return {"jsonrpc": JSONRPC, "method": "notifications/initialized"}


def cancellation_notification(request_id: str) -> dict:
    return {
        "jsonrpc": JSONRPC,
        "method": "notifications/cancelled",
        "params": {"requestId": request_id},
    }


def ping_request(request_id: str) -> dict:
    return {"jsonrpc": JSONRPC, "id": request_id, "method": "ping"}


def tools_list_request(request_id: str) -> dict:
    return {"jsonrpc": JSONRPC, "id": request_id, "method": "tools/list"}


def resources_list_request(request_id: str) -> dict:
    return {"jsonrpc": JSONRPC, "id": request_id, "method": "resources/list"}


def tools_call_request(request_id: str, name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": JSONRPC,
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def resources_read_request(request_id: str, uri: str) -> dict:
    return {
        "jsonrpc": JSONRPC,
        "id": request_id,
        "method": "resources/read",
        "params": {"uri": uri},
    }


def _object_pairs(items: list[tuple[str, object]]) -> dict:
    found: dict = {}
    for key, value in items:
        if key in found:
            raise ValueError("duplicate")
        found[key] = value
    return found


def _reject_constant(_value: str) -> None:
    raise ValueError("constant")


def empty_result(request_id: object) -> dict:
    return {"jsonrpc": JSONRPC, "id": request_id, "result": {}}


def method_not_found(request_id: object) -> dict:
    return {
        "jsonrpc": JSONRPC,
        "id": request_id,
        "error": {"code": -32601, "message": "Method not found"},
    }


def decode_line(raw: bytes) -> Envelope:
    if len(raw) > MAX_LINE_BYTES:
        raise ProtocolError("oversize")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("encoding") from None
    if "\n" in text or "\x00" in text:
        raise ProtocolError("framing")
    try:
        value = json.loads(text, object_pairs_hook=_object_pairs, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError):
        raise ProtocolError("malformed") from None
    if not isinstance(value, dict):
        raise ProtocolError("not_object")
    return parse_envelope(value)


def parse_envelope(message: dict) -> Envelope:
    if message.get("jsonrpc") != JSONRPC:
        raise ProtocolError("envelope")
    if "method" in message:
        method = message["method"]
        if not isinstance(method, str) or not method or "\x00" in method or "\n" in method:
            raise ProtocolError("envelope")
        if "id" in message:
            return Envelope("request", method, message["id"], None, None, message.get("params"))
        if "result" in message or "error" in message:
            raise ProtocolError("envelope")
        return Envelope("notification", method, None, None, None, message.get("params"))
    if "id" not in message or ("result" in message) == ("error" in message):
        raise ProtocolError("envelope")
    return Envelope("response", None, message["id"], message.get("result"), message.get("error"), None)


def accepted_initialize(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    return result.get("protocolVersion") == PROTOCOL_VERSION


def cancellation_target(params: object) -> str | None:
    if not isinstance(params, dict):
        return None
    request_id = params.get("requestId")
    if not isinstance(request_id, str) or not request_id:
        return None
    return request_id
