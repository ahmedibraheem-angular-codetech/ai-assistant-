"""Load current grants and ask the permission engine.

Every call opens a new session and closes it. A previous decision is not reused.
The tool executor is not involved.
"""

from dataclasses import dataclass
from uuid import UUID

from app.agents.repository import AgentRepository
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.files.access import relative_parts
from app.files.exceptions import FileAccessError
from app.files.grants import AgentWorkspaceGrantRepository
from app.permissions.engine import (
    ALLOW,
    DENY,
    REQUIRES_APPROVAL,
    PermissionDecision,
    PermissionFacts,
    decide,
)
from app.permissions.manifests import manifest_for_tool
from app.permissions.models import AgentPathGrantRecord
from app.permissions.repository import PermissionRepository, parse_agent_id
from app.tools.grants import AgentToolGrantRepository
from app.tools.registry import ToolRegistry

_FILE_RESOURCES = frozenset({"workspace", "file", "folder"})


def evaluate_request(
    *,
    agent_id: str | None,
    tool_name: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    relative_path: str | None,
    registry: ToolRegistry,
) -> PermissionDecision:
    session = get_session_factory()()
    try:
        session.expire_all()
        facts = _facts(
            session,
            agent_id=agent_id,
            tool_name=tool_name,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            relative_path=relative_path,
            registry=registry,
        )
        return decide(facts)
    finally:
        session.close()


@dataclass(frozen=True, slots=True)
class McpAccess:
    permitted: bool
    kind: str
    reason: str
    code: str
    mode: str


def evaluate_mcp_access(*, agent_id: str | None, tool_name: str, arguments: dict, registry: ToolRegistry) -> McpAccess:
    """Permission for one MCP parent tool. This does not call decide().

    Discovery is permission only. call_mcp_tool and read_mcp_resource never
    return an allow decision. restricted_auto denies both even when an
    approval row exists. The default assistant has no agent id and is denied.
    """
    session = get_session_factory()()
    try:
        session.expire_all()
        mode = PermissionRepository(session).mode()
        found, enabled, canonical = _agent(session, agent_id)
        tool = registry.get(tool_name)
        stored = PermissionRepository(session).tool_enabled(tool_name)
        tool_enabled = bool(tool is not None and tool.is_enabled() and stored is not False)
        granted = _granted(session, canonical, tool_name, False)
        if agent_id is None:
            return McpAccess(False, "deny", "The default assistant cannot use MCP.", "denied", mode)
        if not found or not enabled or canonical is None:
            return McpAccess(False, "deny", "That agent cannot use this tool.", "denied", mode)
        if not tool_enabled:
            return McpAccess(False, "deny", "That tool is disabled.", "disabled", mode)
        if not granted:
            return McpAccess(False, "deny", "That tool is not granted.", "denied", mode)
        execution = tool_name in {"call_mcp_tool", "read_mcp_resource"}
        discovery = tool_name in {"list_mcp_tools", "list_mcp_resources"}
        if not execution and not discovery:
            return McpAccess(False, "deny", "That tool is not granted.", "denied", mode)
        if execution and mode == "restricted_auto":
            return McpAccess(False, "deny", "Restricted mode does not run MCP operations.", "denied", mode)
        server = _mcp_server(arguments.get("server_id"))
        if server is None or canonical not in server.agent_ids:
            return McpAccess(False, "deny", "That MCP server is not authorized.", "denied", mode)
        if tool_name == "call_mcp_tool":
            name = arguments.get("remote_tool_name")
            if not isinstance(name, str) or name not in server.tool_names:
                return McpAccess(False, "deny", "That MCP tool is not authorized.", "denied", mode)
            return McpAccess(True, "approval", "MCP execution needs approval.", "approval", mode)
        if tool_name == "read_mcp_resource":
            uri = arguments.get("resource_uri")
            if not isinstance(uri, str) or uri not in server.resource_uris:
                return McpAccess(False, "deny", "That MCP resource is not authorized.", "denied", mode)
            return McpAccess(True, "approval", "MCP execution needs approval.", "approval", mode)
        return McpAccess(True, "discovery", "MCP discovery is permitted.", "discovery", mode)
    finally:
        session.close()


def _mcp_server(server_id: object):
    from app.mcp.loader import current_snapshot

    if not isinstance(server_id, str) or not server_id:
        return None
    snapshot = current_snapshot()
    if snapshot is None:
        return None
    for server in snapshot.servers:
        if server.id == server_id and server.enabled:
            return server
    return None


def request_for_tool(registry: ToolRegistry, tool_name: str, arguments: dict) -> dict:
    tool = registry.get(tool_name)
    action = "execute"
    resource_type = "none"
    resource_id = None
    relative_path = None
    if tool is not None:
        manifest = manifest_for_tool(tool)
        action = manifest.actions[0]
        if "workspace" in manifest.resource_types:
            resource_type = "file" if arguments.get("relative_path") else "workspace"
            resource_id = arguments.get("workspace_id") if isinstance(arguments.get("workspace_id"), str) else None
            relative = arguments.get("relative_path")
            relative_path = relative if isinstance(relative, str) else None
        elif "internet" in manifest.resource_types:
            resource_type = "internet"
    return {
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "relative_path": relative_path,
    }


def _facts(
    session,
    *,
    agent_id: str | None,
    tool_name: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    relative_path: str | None,
    registry: ToolRegistry,
) -> PermissionFacts:
    policy = PermissionRepository(session)
    mode = policy.mode()
    found, enabled, canonical = _agent(session, agent_id)
    tool = None if tool_name is None else registry.get(tool_name)
    manifest = manifest_for_tool(tool) if tool is not None else None
    stored = None if tool_name is None else policy.tool_enabled(tool_name)
    tool_enabled = bool(tool is not None and tool.is_enabled() and stored is not False)
    granted = _granted(session, canonical, tool_name, agent_id is None)
    workspace_required = resource_type in _FILE_RESOURCES or (
        manifest is not None and "workspace" in manifest.resource_types
    )
    workspace_id = resource_id if isinstance(resource_id, str) else None
    workspace_granted = False
    if workspace_required and canonical and workspace_id:
        workspace_granted = AgentWorkspaceGrantRepository(session).allows(canonical, workspace_id)
    path_allowed = True
    if workspace_required and canonical and workspace_id:
        path_allowed = _path_allowed(session, canonical, workspace_id, relative_path)
    internet = bool(manifest is not None and "internet" in manifest.resource_types) or resource_type == "internet"
    return PermissionFacts(
        mode=mode,
        agent_id=canonical if agent_id is not None else None,
        agent_found=found,
        agent_enabled=enabled,
        tool_name=tool_name,
        tool_known=tool is not None,
        tool_enabled=tool_enabled,
        tool_granted=granted,
        risk=manifest.risk if manifest is not None else "unknown",
        action=action,
        allowed_actions=manifest.actions if manifest is not None else (),
        resource_type=resource_type,
        workspace_required=workspace_required,
        workspace_granted=workspace_granted,
        path_allowed=path_allowed,
        internet=internet,
        approval_required=bool(manifest.approval_required) if manifest is not None else False,
        change_granted=_change_granted(
            session,
            canonical,
            action,
            resource_id if isinstance(resource_id, str) else None,
            relative_path,
        ),
    )


def _agent(session, agent_id: str | None) -> tuple[bool, bool, str | None]:
    if agent_id is None:
        return True, True, None
    parsed = parse_agent_id(agent_id)
    if parsed is None:
        return False, False, None
    agent = AgentRepository(session).get(parsed)
    if agent is None:
        return False, False, None
    return True, bool(agent.enabled), agent.id


def _granted(session, canonical: str | None, tool_name: str | None, default_assistant: bool) -> bool:
    if tool_name is None:
        return False
    if default_assistant:
        return tool_name in _default_names()
    if canonical is None:
        return False
    return tool_name in AgentToolGrantRepository(session).names_for(canonical)


def _default_names() -> list[str]:
    names: list[str] = []
    for part in get_settings().default_assistant_tools.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return names


def _path_allowed(session, agent_id: str, workspace_id: str, relative_path: str | None) -> bool:
    rows = [
        row
        for row in PermissionRepository(session).path_grants(agent_id)
        if row.workspace_id == workspace_id and row.action == "read"
    ]
    if not rows:
        return True
    try:
        parts = relative_parts(relative_path)
    except FileAccessError:
        return False
    if not parts:
        return False
    target = "/".join(parts)
    for row in rows:
        if row.kind == "file" and row.relative_path == target:
            return True
        if row.kind == "folder" and (target == row.relative_path or target.startswith(row.relative_path + "/")):
            return True
    return False


def explicit_file_grant(
    agent_id: str | None,
    workspace_id: object,
    relative_path: str | None,
    action: str,
) -> bool:
    """True only for an explicit create, write, or delete path grant."""
    session = get_session_factory()()
    try:
        parsed = parse_agent_id(agent_id) if agent_id is not None else None
        if parsed is None or not isinstance(workspace_id, str):
            return False
        return _change_allowed(session, parsed, workspace_id, relative_path, action)
    finally:
        session.close()


def _change_granted(
    session,
    agent_id: str | None,
    action: str,
    workspace_id: str | None,
    relative_path: str | None,
) -> bool:
    if action not in {"create", "write", "delete"}:
        return True
    if agent_id is None or workspace_id is None:
        return False
    return _change_allowed(session, agent_id, workspace_id, relative_path, action)


def _change_allowed(
    session,
    agent_id: str,
    workspace_id: str,
    relative_path: str | None,
    action: str,
) -> bool:
    rows = [
        row
        for row in PermissionRepository(session).path_grants(agent_id)
        if row.workspace_id == workspace_id and row.action == action
    ]
    if not rows:
        return False
    try:
        parts = relative_parts(relative_path)
    except FileAccessError:
        return False
    if not parts:
        return False
    target = "/".join(parts)
    for row in rows:
        if row.kind == "file" and row.relative_path == target:
            return True
        if row.kind == "folder" and (target == row.relative_path or target.startswith(row.relative_path + "/")):
            return True
    return False


def normalize_path_grant(raw_path: str) -> str:
    parts = relative_parts(raw_path)
    if not parts:
        raise FileAccessError("That path is outside the workspace.")
    return "/".join(parts)


def workspace_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


__all__ = [
    "ALLOW",
    "DENY",
    "REQUIRES_APPROVAL",
    "AgentPathGrantRecord",
    "PermissionDecision",
    "McpAccess",
    "evaluate_mcp_access",
    "evaluate_request",
    "explicit_file_grant",
    "normalize_path_grant",
    "request_for_tool",
]
