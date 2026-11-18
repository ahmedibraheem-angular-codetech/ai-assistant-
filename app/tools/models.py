import json

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentToolGrantRecord(Base):
    """Tools an agent is allowed to request.

    The agents table is unchanged. No rows means the agent has no tools.
    """

    __tablename__ = "agent_tool_grants"

    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tool_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class ToolOutcome:
    """Normalized tool result. Error text is written by the tool layer."""

    def __init__(self, tool: str, status: str, output: dict | None, error: str | None) -> None:
        self.tool = tool
        self.status = status
        self.output = output
        self.error = error

    def model_text(self) -> str:
        body: dict = {"status": self.status, "tool": self.tool}
        if self.output is not None:
            body["output"] = self.output
        if self.error:
            body["error"] = self.error
        return "Tool result: " + json.dumps(body, sort_keys=True)
