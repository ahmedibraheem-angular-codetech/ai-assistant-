from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Agent:
    id: str
    name: str
    description: str
    role: str
    system_prompt: str
    model: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class AgentRecord(Base):
    """Persistent agent configuration. Names are unique via name_normalized."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(200), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def to_agent(record: AgentRecord) -> Agent:
    return Agent(
        id=record.id,
        name=record.name,
        description=record.description,
        role=record.role,
        system_prompt=record.system_prompt,
        model=record.model,
        enabled=bool(record.enabled),
        created_at=as_utc(record.created_at),
        updated_at=as_utc(record.updated_at),
    )
