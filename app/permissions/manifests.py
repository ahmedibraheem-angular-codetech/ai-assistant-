"""Tool permission manifests.

A manifest describes what a tool needs. Registering a tool does not grant it
to any agent, and a manifest cannot contain a grant action.
"""

import re
from dataclasses import dataclass

from app.tools.base import ASYNC_READ_ONLY, BLOCKING_READ_ONLY, SIDE_EFFECT, Tool
from app.tools.exceptions import ToolError

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

RESOURCE_TYPES = frozenset(
    {"none", "workspace", "file", "folder", "internet", "terminal", "application", "installer"}
)
ACTIONS = frozenset(
    {
        "execute",
        "read",
        "list",
        "search",
        "metadata",
        "inspect",
        "extract",
        "create",
        "write",
        "modify",
        "delete",
        "install",
        "launch",
        "execute_command",
    }
)
RISKS = frozenset({"low", "read", "sensitive"})
POLICIES = frozenset({ASYNC_READ_ONLY, BLOCKING_READ_ONLY, SIDE_EFFECT})
_MUTATING = frozenset({"modify", "delete", "install", "launch", "execute_command"})
_FILE_ACTIONS = {
    "list_files": ("list",),
    "search_files": ("search",),
    "read_file": ("read", "extract"),
    "get_file_metadata": ("metadata",),
    "inspect_project": ("inspect",),
}


class ToolManifestError(ToolError):
    def __init__(self, message: str = "The tool permission manifest is not valid.") -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ToolManifest:
    tool_id: str
    name: str
    description: str
    resource_types: tuple[str, ...]
    actions: tuple[str, ...]
    risk: str
    approval_required: bool
    read_only: bool
    execution_policy: str


def manifest_for_tool(tool: Tool) -> ToolManifest:
    custom = getattr(tool, "permission_manifest", None)
    if isinstance(custom, ToolManifest):
        return custom
    name = getattr(tool, "name", "")
    policy = getattr(tool, "execution_policy", ASYNC_READ_ONLY)
    if name in _FILE_ACTIONS:
        return ToolManifest(
            tool_id=name,
            name=name,
            description=tool.description,
            resource_types=("workspace", "file", "folder"),
            actions=_FILE_ACTIONS[name],
            risk="read",
            approval_required=True,
            read_only=True,
            execution_policy=policy,
        )
    if name in {"web_search", "research"}:
        return ToolManifest(
            tool_id=name,
            name=name,
            description=tool.description,
            resource_types=("internet",),
            actions=("read",),
            risk="read",
            approval_required=True,
            read_only=True,
            execution_policy=policy,
        )
    sensitive = bool(tool.requires_confirmation) or policy == SIDE_EFFECT
    return ToolManifest(
        tool_id=name,
        name=name,
        description=tool.description,
        resource_types=("none",),
        actions=("execute",),
        risk="sensitive" if sensitive else "low",
        approval_required=sensitive,
        read_only=not sensitive,
        execution_policy=policy,
    )


def validate_manifest(manifest: ToolManifest) -> None:
    if not isinstance(manifest, ToolManifest):
        raise ToolManifestError()
    if manifest.tool_id != manifest.name or _NAME.fullmatch(manifest.name or "") is None:
        raise ToolManifestError("The tool id is not valid.")
    if not isinstance(manifest.description, str) or not manifest.description.strip():
        raise ToolManifestError("The tool description is missing.")
    if len(manifest.description) > 2_000:
        raise ToolManifestError("The tool description is too long.")
    if manifest.risk not in RISKS or manifest.execution_policy not in POLICIES:
        raise ToolManifestError("The tool risk or execution policy is not recognized.")
    if not manifest.resource_types or not manifest.actions:
        raise ToolManifestError("The tool must declare its resources and actions.")
    if any(item in {"grant", "set_permission", "change_mode"} for item in manifest.actions):
        raise ToolManifestError("A tool cannot grant permissions.")
    if any(item not in RESOURCE_TYPES for item in manifest.resource_types):
        raise ToolManifestError("The tool declares an unknown resource type.")
    if any(item not in ACTIONS for item in manifest.actions):
        raise ToolManifestError("The tool declares an unknown action.")
    if "permission" in manifest.resource_types:
        raise ToolManifestError("A tool cannot grant permissions.")
    if manifest.read_only and _MUTATING.intersection(manifest.actions):
        raise ToolManifestError("A read-only tool cannot declare a changing action.")
    if manifest.risk == "low" and (not manifest.read_only or manifest.approval_required):
        raise ToolManifestError("A low-risk tool must be read-only and must not require approval.")
    if manifest.risk == "sensitive" and manifest.read_only and manifest.execution_policy != SIDE_EFFECT:
        if manifest.actions != ("execute",) and _MUTATING.intersection(manifest.actions):
            raise ToolManifestError("The sensitive declaration conflicts with the actions.")
