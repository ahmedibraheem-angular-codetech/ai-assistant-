"""The only path that turns a consumed MCP approval into one MCP frame.

The gateway never receives an ORM session and never commits. ApprovalService
owns the approval transaction. Lock order is the per-server gateway lock,
then the loader permit, then the session locks. The permit, the session
locks, and the gateway lock are released before this module waits for a
response. Responses stay matched by JSON-RPC id. The send lock still
serializes the bytes of one frame.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

from app.approvals.fingerprint import operation_fingerprint, redacted_arguments
from app.approvals.review import MCP_CALL_TOOL, MCP_READ_TOOL, format_expiry
from app.mcp.arguments import ArgumentError, canonical_json, sha256_text, validate_mcp_arguments, validate_schema
from app.mcp.loader import ConfigError, WritePermit, current_snapshot
from app.mcp.payload import PayloadError, open_payload, payload_aad, seal_payload
from app.mcp.protocol import resources_read_request, tools_call_request
from app.mcp.session import (
    DISPATCH_GUARD,
    SessionError,
    begin_execution_frame,
    begin_list_frame,
    close_session,
    observe_discovery,
    open_session,
    operation_deadline,
    published_session,
    wait_mcp,
)

NOT_SENT = "The MCP operation was not sent."
UNKNOWN_RESULT = "The MCP operation may have reached the server. It will not be retried."
REMOTE_RESULT = "The MCP server reported an error."

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_before_write = None
_before_permit = None


class GatewayError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(NOT_SENT)


@dataclass(frozen=True)
class SealPackage:
    session: object
    discovery_at_response: int
    arguments: dict
    fingerprint: str
    nonce: bytes
    ciphertext: bytes
    action: str
    resource_type: str
    description: str


@dataclass(frozen=True)
class SealedDispatch:
    approval_id: str
    agent_id: str | None
    tool_name: str
    fingerprint: str
    arguments: dict
    nonce: bytes
    ciphertext: bytes


@dataclass(frozen=True)
class DispatchOutcome:
    classification: str
    public_result: str


def set_before_write(callback) -> None:
    """Test hook. It runs while the write permit is held and before the frame."""
    global _before_write
    _before_write = callback


def set_before_permit(callback) -> None:
    """Test hook. It runs after the execution claim and before the permit."""
    global _before_permit
    _before_permit = callback


def discover_tools(agent_id: str | None, server_id: str) -> dict:
    _require_discovery(agent_id, "list_mcp_tools", {"server_id": server_id})
    session, result, discovery = _list(server_id, "tools/list")
    server = _server(session)
    tools = []
    for name, schema, digest in _indexed_tools(result, server.tool_names):
        tools.append({"name": name, "input_schema_sha256": digest, "input_schema": schema})
    return _observed(session, discovery, tools=tools)


def discover_resources(agent_id: str | None, server_id: str) -> dict:
    _require_discovery(agent_id, "list_mcp_resources", {"server_id": server_id})
    session, result, discovery = _list(server_id, "resources/list")
    server = _server(session)
    resources = []
    listed = result.get("resources") if isinstance(result, dict) else None
    if isinstance(listed, list):
        for item in listed:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri")
            if not isinstance(uri, str) or uri not in server.resource_uris:
                continue
            name = item.get("name") if isinstance(item.get("name"), str) and len(item["name"]) <= 80 else ""
            resources.append({"uri": uri, "name": name, "authorized": True})
    return _observed(session, discovery, resources=resources)


def seal_call(
    *,
    approval_id: str,
    agent_id: str | None,
    expires_at,
    server_id: str,
    remote_tool_name: str,
    tool_arguments: dict,
) -> SealPackage:
    _require_execution(agent_id, MCP_CALL_TOOL, {"server_id": server_id, "remote_tool_name": remote_tool_name})
    if not isinstance(remote_tool_name, str) or not remote_tool_name or len(remote_tool_name) > 64:
        raise GatewayError("invalid")
    if not isinstance(tool_arguments, dict):
        raise GatewayError("invalid")
    lock = _server_lock(server_id)
    with lock:
        session = _open(server_id)
        server = _server(session)
        if remote_tool_name not in server.tool_names or agent_id not in server.agent_ids:
            raise GatewayError("denied")
        pending, broken = _begin_list(session, "tools/list")
    if broken or session._stream_broken:
        close_session(session)
        raise GatewayError("unavailable")
    if pending is None or wait_mcp(session, pending, seconds=operation_deadline("list")) != "completed":
        raise GatewayError("unavailable")
    if pending.discovery_at_response is None:
        raise GatewayError("unavailable")
    selected = None
    schema_hash = ""
    schema = None
    for name, item_schema, digest in _indexed_tools(pending.result, server.tool_names):
        if name == remote_tool_name:
            selected = name
            schema = item_schema
            schema_hash = digest
    if selected is None or schema is None:
        raise GatewayError("denied")
    try:
        canonical, ordered = validate_mcp_arguments(schema, tool_arguments)
    except ArgumentError as exc:
        raise GatewayError("invalid") from exc
    identity = _identity(session, server, pending.discovery_at_response)
    discovery = pending.discovery_at_response
    shown, _hidden = redacted_arguments({"tool_arguments": tool_arguments})
    sealed = {
        **identity,
        "remote_tool_name": remote_tool_name,
        "input_schema_sha256": schema_hash,
        "arguments_sha256": sha256_text(canonical),
        "tool_arguments": shown["tool_arguments"],
    }
    package = _encrypt(
        session=session,
        discovery=discovery,
        sealed=sealed,
        approval_id=approval_id,
        agent_id=agent_id,
        tool_name=MCP_CALL_TOOL,
        expires_at=expires_at,
        plaintext={
            "kind": "call",
            "remote_tool_name": remote_tool_name,
            "arguments_sha256": sealed["arguments_sha256"],
            "arguments": ordered,
        },
        description=f"Call {remote_tool_name} on {server_id}.",
    )
    return package


def seal_read(
    *,
    approval_id: str,
    agent_id: str | None,
    expires_at,
    server_id: str,
    resource_uri: str,
) -> SealPackage:
    _require_execution(agent_id, MCP_READ_TOOL, {"server_id": server_id, "resource_uri": resource_uri})
    if not isinstance(resource_uri, str) or not resource_uri or len(resource_uri) > 400 or "\x00" in resource_uri:
        raise GatewayError("invalid")
    lock = _server_lock(server_id)
    with lock:
        session = _open(server_id)
        server = _server(session)
        if resource_uri not in server.resource_uris or agent_id not in server.agent_ids:
            raise GatewayError("denied")
        try:
            discovery = observe_discovery(session, guard=DISPATCH_GUARD)
        except SessionError as exc:
            raise GatewayError("unavailable") from exc
        identity = _identity(session, server, discovery)
    sealed = {**identity, "resource_uri": resource_uri}
    return _encrypt(
        session=session,
        discovery=discovery,
        sealed=sealed,
        approval_id=approval_id,
        agent_id=agent_id,
        tool_name=MCP_READ_TOOL,
        expires_at=expires_at,
        plaintext={"kind": "read", "resource_uri": resource_uri},
        description=f"Read a resource on {server_id}.",
    )


def dispatch(sealed: SealedDispatch) -> DispatchOutcome:
    """Write at most one frame. The caller has already committed EXECUTING."""
    try:
        plaintext = _decrypt(sealed)
    except PayloadError:
        return DispatchOutcome("not_dispatched", NOT_SENT)
    if plaintext is None:
        return DispatchOutcome("not_dispatched", NOT_SENT)
    arguments = sealed.arguments
    server_id = arguments.get("server_id")
    if not isinstance(server_id, str):
        return DispatchOutcome("not_dispatched", NOT_SENT)
    try:
        from app.permissions.service import evaluate_mcp_access
        from app.tools.registry import get_tool_registry

        access = evaluate_mcp_access(
            agent_id=sealed.agent_id,
            tool_name=sealed.tool_name,
            arguments=_execution_arguments(sealed.tool_name, arguments),
            registry=get_tool_registry(),
        )
    except Exception:
        return DispatchOutcome("not_dispatched", NOT_SENT)
    if not access.permitted or access.kind != "approval":
        return DispatchOutcome("not_dispatched", NOT_SENT)
    frame = _frame(sealed.tool_name, plaintext)
    epoch = arguments.get("epoch")
    revision = arguments.get("config_revision")
    if frame is None or isinstance(epoch, bool) or not isinstance(epoch, int) or not isinstance(revision, str):
        return DispatchOutcome("not_dispatched", NOT_SENT)
    session = None
    pending = None
    outcome = None
    with _server_lock(server_id):
        early = _before_permit
        if early is not None:
            early()
        try:
            permit = WritePermit.acquire(revision, epoch)
        except (ConfigError, TypeError, ValueError):
            return DispatchOutcome("not_dispatched", NOT_SENT)
        try:
            session = published_session(server_id)
            if (
                session is None
                or session.revision != arguments.get("config_revision")
                or session.epoch != arguments.get("epoch")
            ):
                outcome = DispatchOutcome("not_dispatched", NOT_SENT)
            else:
                hook = _before_write
                if hook is not None:
                    hook(session)
                try:
                    pending = begin_execution_frame(
                        session,
                        frame,
                        guard=DISPATCH_GUARD,
                        process_generation=int(arguments["process_generation"]),
                        discovery_generation=int(arguments["discovery_generation"]),
                    )
                except SessionError as exc:
                    if exc.code == "unknown" and session is not None:
                        outcome = DispatchOutcome("unknown", UNKNOWN_RESULT)
                    else:
                        outcome = DispatchOutcome("not_dispatched", NOT_SENT)
        finally:
            permit.release()
    if outcome is not None:
        if session is not None and (outcome.classification == "unknown" or session._stream_broken):
            close_session(session)
        return outcome
    assert pending is not None and session is not None
    kind = wait_mcp(session, pending, seconds=operation_deadline("call"))
    if kind == "unknown":
        close_session(session)
        return DispatchOutcome("unknown", UNKNOWN_RESULT)
    if kind == "remote_failed":
        return DispatchOutcome("remote_failed", REMOTE_RESULT)
    return DispatchOutcome("completed", _public_result(pending.result))


def _decrypt(sealed: SealedDispatch) -> dict | None:
    aad = payload_aad(
        approval_id=sealed.approval_id,
        fingerprint=sealed.fingerprint,
        tool_name=sealed.tool_name,
        agent_id=sealed.agent_id,
    )
    raw = open_payload(sealed.nonce, sealed.ciphertext, aad)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    if body.get("approval_id") != sealed.approval_id or body.get("fingerprint") != sealed.fingerprint:
        return None
    arguments = sealed.arguments
    if sealed.tool_name == MCP_CALL_TOOL:
        if body.get("kind") != "call" or body.get("remote_tool_name") != arguments.get("remote_tool_name"):
            return None
        if body.get("arguments_sha256") != arguments.get("arguments_sha256"):
            return None
        try:
            text = canonical_json(body.get("arguments"))
        except ArgumentError:
            return None
        if sha256_text(text) != arguments.get("arguments_sha256"):
            return None
        body["arguments"] = json.loads(text)
        return body
    if sealed.tool_name == MCP_READ_TOOL:
        if body.get("kind") != "read" or body.get("resource_uri") != arguments.get("resource_uri"):
            return None
        return body
    return None


def _frame(tool_name: str, plaintext: dict) -> dict | None:
    if tool_name == MCP_CALL_TOOL:
        return tools_call_request("pending", plaintext["remote_tool_name"], plaintext["arguments"])
    if tool_name == MCP_READ_TOOL:
        return resources_read_request("pending", plaintext["resource_uri"])
    return None


def _execution_arguments(tool_name: str, arguments: dict) -> dict:
    if tool_name == MCP_CALL_TOOL:
        return {"server_id": arguments.get("server_id"), "remote_tool_name": arguments.get("remote_tool_name")}
    return {"server_id": arguments.get("server_id"), "resource_uri": arguments.get("resource_uri")}


def _require_discovery(agent_id: str | None, tool_name: str, arguments: dict) -> None:
    from app.permissions.service import evaluate_mcp_access
    from app.tools.registry import get_tool_registry

    access = evaluate_mcp_access(
        agent_id=agent_id,
        tool_name=tool_name,
        arguments=arguments,
        registry=get_tool_registry(),
    )
    if not access.permitted or access.kind != "discovery":
        raise GatewayError("denied")


def _require_execution(agent_id: str | None, tool_name: str, arguments: dict) -> None:
    from app.permissions.service import evaluate_mcp_access
    from app.tools.registry import get_tool_registry

    access = evaluate_mcp_access(
        agent_id=agent_id,
        tool_name=tool_name,
        arguments=arguments,
        registry=get_tool_registry(),
    )
    if not access.permitted or access.kind != "approval":
        raise GatewayError("denied")


def _list(server_id: str, method: str) -> tuple[object, object, int]:
    with _server_lock(server_id):
        session = _open(server_id)
        pending, broken = _begin_list(session, method)
    if broken or session._stream_broken:
        close_session(session)
        raise GatewayError("unavailable")
    if pending is None or wait_mcp(session, pending, seconds=operation_deadline("list")) != "completed":
        raise GatewayError("unavailable")
    if pending.discovery_at_response is None:
        raise GatewayError("unavailable")
    return session, pending.result, pending.discovery_at_response


def _begin_list(session, method: str) -> tuple[object | None, bool]:
    try:
        return begin_list_frame(session, method, guard=DISPATCH_GUARD), False
    except SessionError as exc:
        return None, exc.code == "unknown"


def _open(server_id: str):
    try:
        return open_session(server_id)
    except SessionError as exc:
        raise GatewayError("unavailable") from exc


def _server(session):
    snapshot = current_snapshot()
    if snapshot is None or snapshot.revision != session.revision or snapshot.epoch != session.epoch:
        raise GatewayError("unavailable")
    for server in snapshot.servers:
        if server.id == session.server_id and server.enabled:
            return server
    raise GatewayError("unavailable")


def _identity(session, server, discovery: int) -> dict:
    if (
        session.executable_device is None
        or session.executable_inode is None
        or not session.executable_sha256
        or not session.env_sha256
    ):
        raise GatewayError("unavailable")
    return {
        "server_id": server.id,
        "server_display_name": server.display_name,
        "config_revision": session.revision,
        "epoch": session.epoch,
        "executable_device": session.executable_device,
        "executable_inode": session.executable_inode,
        "executable_sha256": session.executable_sha256,
        "env_sha256": session.env_sha256,
        "process_generation": session.generation,
        "discovery_generation": discovery,
    }


def _observed(session, discovery: int, **payload) -> dict:
    server = _server(session)
    return {
        "server_id": server.id,
        "config_revision": session.revision,
        "epoch": session.epoch,
        "process_generation": session.generation,
        "discovery_generation": discovery,
        **payload,
    }


def _indexed_tools(result: object, allowed: tuple[str, ...]) -> list[tuple[str, dict, str]]:
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        raise GatewayError("invalid")
    found: list[tuple[str, dict, str]] = []
    seen: set[str] = set()
    for item in result["tools"]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        schema = item.get("inputSchema")
        if not isinstance(name, str) or name not in allowed or name in seen or not isinstance(schema, dict):
            continue
        try:
            text = validate_schema(schema)
        except ArgumentError:
            continue
        seen.add(name)
        found.append((name, schema, sha256_text(text)))
    return found


def _encrypt(
    *,
    session,
    discovery: int,
    sealed: dict,
    approval_id: str,
    agent_id: str | None,
    tool_name: str,
    expires_at,
    plaintext: dict,
    description: str,
) -> SealPackage:
    body = dict(sealed)
    body["approval_id"] = approval_id
    body["expires_at"] = format_expiry(expires_at)
    fingerprint = operation_fingerprint(
        agent_id=agent_id,
        tool_name=tool_name,
        action="execute",
        resource_type="none",
        resource_id=None,
        arguments=body,
    )
    encoded = dict(plaintext)
    encoded["approval_id"] = approval_id
    encoded["fingerprint"] = fingerprint
    try:
        raw = json.dumps(encoded, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
        nonce, ciphertext = seal_payload(
            raw,
            payload_aad(
                approval_id=approval_id,
                fingerprint=fingerprint,
                tool_name=tool_name,
                agent_id=agent_id,
            ),
        )
    except (PayloadError, TypeError, ValueError) as exc:
        raise GatewayError("unavailable") from exc
    return SealPackage(
        session=session,
        discovery_at_response=discovery,
        arguments=body,
        fingerprint=fingerprint,
        nonce=nonce,
        ciphertext=ciphertext,
        action="execute",
        resource_type="none",
        description=" ".join(description.split())[:300],
    )


def _public_result(result: object) -> str:
    if not isinstance(result, dict):
        return json.dumps({"status": "ok"})
    shown, _hidden = redacted_arguments({"result": result})
    return json.dumps({"status": "ok", "result": shown.get("result")}, default=str)[:400]


def _server_lock(server_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(server_id)
        if lock is None:
            lock = threading.Lock()
            _locks[server_id] = lock
        return lock
