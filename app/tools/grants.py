from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.tools.models import AgentToolGrantRecord


class AgentToolGrantRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def names_for(self, agent_id: str) -> list[str]:
        statement = (
            select(AgentToolGrantRecord.tool_name)
            .where(AgentToolGrantRecord.agent_id == agent_id)
            .order_by(AgentToolGrantRecord.position.asc(), AgentToolGrantRecord.tool_name.asc())
        )
        return list(self.session.scalars(statement).all())

    def replace(self, agent_id: str, names: list[str]) -> None:
        unique: list[str] = []
        for name in names:
            if name not in unique:
                unique.append(name)
        self.session.execute(
            delete(AgentToolGrantRecord).where(AgentToolGrantRecord.agent_id == agent_id)
        )
        for position, name in enumerate(unique):
            self.session.add(
                AgentToolGrantRecord(agent_id=agent_id, tool_name=name, position=position)
            )
        self.session.commit()
