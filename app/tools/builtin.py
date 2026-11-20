from app.agents.team import DelegateTaskTool
from app.commands.tools import builtin_command_tools
from app.files.tools import builtin_file_tools
from app.knowledge.tools import builtin_knowledge_tools
from app.mcp.tools import builtin_mcp_tools
from app.research.tool import ResearchTool
from app.tools.calculator import CalculatorTool
from app.tools.datetime_tool import DateTimeTool
from app.tools.registry import ToolRegistry
from app.tools.web_search import DuckDuckGoSearchProvider, WebSearchTool


def register_builtin_tools(registry: ToolRegistry) -> None:
    for tool in (
        CalculatorTool(),
        DateTimeTool(),
        WebSearchTool(DuckDuckGoSearchProvider()),
        ResearchTool(),
        DelegateTaskTool(),
        *builtin_file_tools(),
        *builtin_command_tools(),
        *builtin_knowledge_tools(),
        *builtin_mcp_tools(),
    ):
        if registry.get(tool.name) is None:
            registry.register(tool)
