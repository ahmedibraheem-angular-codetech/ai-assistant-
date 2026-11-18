from app.permissions.manifests import manifest_for_tool, validate_manifest
from app.tools.base import Tool
from app.tools.exceptions import DuplicateToolError


class ToolRegistry:
    """In-memory catalog of tool implementations."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise DuplicateToolError(tool.name)
        validate_manifest(manifest_for_tool(tool))
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())


_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        from app.tools.builtin import register_builtin_tools

        _registry = ToolRegistry()
        register_builtin_tools(_registry)
    return _registry
