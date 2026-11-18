from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.models import Agent, AgentRecord, to_agent

_MUTABLE_FIELDS = {
    "name",
    "name_normalized",
    "description",
    "role",
    "system_prompt",
    "model",
    "enabled",
}


class AgentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_agents(self, enabled: bool | None = None) -> list[Agent]:
        statement = select(AgentRecord).order_by(AgentRecord.created_at.asc(), AgentRecord.id.asc())
        if enabled is not None:
            statement = statement.where(AgentRecord.enabled.is_(enabled))
        return [to_agent(record) for record in self.session.scalars(statement).all()]

    def get(self, agent_id: str) -> Agent | None:
        record = self.session.get(AgentRecord, agent_id)
        if record is None:
            return None
        return to_agent(record)

    def get_by_normalized_name(self, name_normalized: str) -> Agent | None:
        statement = select(AgentRecord).where(AgentRecord.name_normalized == name_normalized)
        record = self.session.scalar(statement)
        if record is None:
            return None
        return to_agent(record)

    def insert(
        self,
        *,
        agent_id: str,
        name: str,
        name_normalized: str,
        description: str,
        role: str,
        system_prompt: str,
        model: str,
        enabled: bool,
        created_at: datetime,
        updated_at: datetime,
    ) -> Agent:
        record = AgentRecord(
            id=agent_id,
            name=name,
            name_normalized=name_normalized,
            description=description,
            role=role,
            system_prompt=system_prompt,
            model=model,
            enabled=enabled,
            created_at=created_at,
            updated_at=updated_at,
        )
        return self._persist(record)

    def apply_update(self, agent_id: str, changes: dict[str, object], updated_at: datetime) -> Agent | None:
        unknown = set(changes) - _MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported agent fields: {', '.join(sorted(unknown))}")

        record = self.session.get(AgentRecord, agent_id)
        if record is None:
            return None
        for key, value in changes.items():
            setattr(record, key, value)
        record.updated_at = updated_at
        return self._persist(record)

    def delete(self, agent_id: str) -> bool:
        record = self.session.get(AgentRecord, agent_id)
        if record is None:
            return False
        self.session.delete(record)
        self.session.commit()
        return True

    def _persist(self, record: AgentRecord) -> Agent:
        self.session.add(record)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            raise
        self.session.refresh(record)
        return to_agent(record)
