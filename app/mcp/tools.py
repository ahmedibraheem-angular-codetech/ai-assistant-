"""Parent tools for MCP. Remote tool names are arguments, not registry entries."""

import asyncio

from app.files.authorization import _AGENT
from app.mcp.gateway import GatewayError, discover_resources, discover_tools
from app.permissions.manifests import ToolManifest
from app.tools.base import ASYNC_READ_ONLY, Tool
from app.tools.exceptions import ToolExecutionError

LIST_MCP_TOOLS = "list_mcp_tools"
LIST_MCP_RESOURCES = "list_mcp_resources"
CALL_MCP_TOOL = "call_mcp_tool"
READ_MCP_RESOURCE = "read_mcp_resource"
MCP_PARENT_TOOLS = frozenset(
    {LIST_MCP_TOOLS, LIST_MCP_RESOURCES, CALL_MCP_TOOL, READ_MCP_RESOURCE}
)

_SERVER = {"type": "string", "minLength": 1, "maxLength": 64}
_REMOTE = {"type": "string", "minLength": 1, "maxLength": 64}
_URI = {"type": "string", "minLength": 1, "maxLength": 400}
_ARGUMENTS = {"type": "object"}


def _object(required: list[str], properties: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _manifest(name: str, description: str, *, sensitive: bool) -> ToolManifest:
    return ToolManifest(
        tool_id=name,
        name=name,
        description=description,
        resource_types=("none",),
        actions=("execute",),
        risk="sensitive" if sensitive else "read",
        approval_required=sensitive,
        read_only=not sensitive,
        execution_policy=ASYNC_READ_ONLY,
    )


class _McpTool(Tool):
    execution_policy = ASYNC_READ_ONLY
    requires_confirmation = False
    timeout_seconds = 60.0
    enabled = True
    category = "mcp"

    def _agent(self) -> str | None:
        agent_id = _AGENT.get()
        if agent_id is None:
            return None
        if not isinstance(agent_id, str) or not agent_id:
            raise ToolExecutionError("The MCP operation was not sent.")
        return agent_id


class ListMcpToolsTool(_McpTool):
    name = LIST_MCP_TOOLS
    description = "List MCP tools the selected server authorizes for this agent."
    input_schema = _object(["server_id"], {"server_id": _SERVER})
    permission_manifest = _manifest(name, description, sensitive=False)

    async def run(self, arguments: dict) -> dict:
        agent_id = self._agent()
        try:
            return await asyncio.to_thread(discover_tools, agent_id, arguments["server_id"])
        except GatewayError as exc:
            raise ToolExecutionError("The MCP operation was not sent.") from exc


class ListMcpResourcesTool(_McpTool):
    name = LIST_MCP_RESOURCES
    description = "List MCP resources the selected server authorizes for this agent."
    input_schema = _object(["server_id"], {"server_id": _SERVER})
    permission_manifest = _manifest(name, description, sensitive=False)

    async def run(self, arguments: dict) -> dict:
        agent_id = self._agent()
        try:
            return await asyncio.to_thread(discover_resources, agent_id, arguments["server_id"])
        except GatewayError as exc:
            raise ToolExecutionError("The MCP operation was not sent.") from exc


class CallMcpToolTool(_McpTool):
    name = CALL_MCP_TOOL
    description = "Ask to call one authorized MCP tool. The call waits for its own approval."
    input_schema = _object(
        ["server_id", "remote_tool_name", "tool_arguments"],
        {"server_id": _SERVER, "remote_tool_name": _REMOTE, "tool_arguments": _ARGUMENTS},
    )
    permission_manifest = _manifest(name, description, sensitive=True)

    async def run(self, arguments: dict) -> dict:
        del arguments
        raise ToolExecutionError("The MCP operation was not sent.")


class ReadMcpResourceTool(_McpTool):
    name = READ_MCP_RESOURCE
    description = "Ask to read one authorized MCP resource. The read waits for its own approval."
    input_schema = _object(
        ["server_id", "resource_uri"],
        {"server_id": _SERVER, "resource_uri": _URI},
    )
    permission_manifest = _manifest(name, description, sensitive=True)

    async def run(self, arguments: dict) -> dict:
        del arguments
        raise ToolExecutionError("The MCP operation was not sent.")


def builtin_mcp_tools() -> list[Tool]:
    return [
        ListMcpToolsTool(),
        ListMcpResourcesTool(),
        CallMcpToolTool(),
        ReadMcpResourceTool(),
    ]
