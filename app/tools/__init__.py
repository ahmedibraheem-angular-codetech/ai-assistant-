"""Tool execution for the local assistant.

Tools are registered in code, then granted to agents in agent_tool_grants.
The default assistant's grants come from DEFAULT_ASSISTANT_TOOLS. An empty
grant list means the model is not offered tools and the chat prompt is unchanged.

Calculator and datetime are blocking read-only tools. They run in a child
process, not on the server event loop, so a timeout can SIGKILL that process
group. Web search is async and read-only: it stays on the event loop, where
cancellation is cooperative and is not a hard stop. Web search is optional
network access through a replaceable provider and is off unless
WEB_SEARCH_ENABLED is true. The built-in provider is DuckDuckGo's instant-answer
API: no key, no page scraping, and often no publication date. It is not a full
web index.

Add a tool by subclassing Tool, declaring execution_policy, registering it
once, and granting its name. Blocking and side-effecting tools must name an
allowlisted isolation_key. A tool that needs a hard timeout but cannot be
isolated is not run. Side-effecting tools set requires_confirmation and are
not run from a model request or the test API. Killing a worker stops further
work in that process; it does not undo an effect the process already performed.
Permission is checked before a call starts, not while it is running. Tools do
not receive memory or permission services.
"""

from app.tools.registry import get_tool_registry
from app.tools.service import ToolService

__all__ = ["ToolService", "get_tool_registry"]
