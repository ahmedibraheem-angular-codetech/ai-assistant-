from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Memory:
    id: str
    key: str
    content: str
    category: str
    description: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    enabled: bool
    priority: int


class MemoryRecord(Base):
    """Explicit user memory. Conversation messages are never copied here."""

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    key_normalized: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, index=True)


def to_memory(record: MemoryRecord) -> Memory:
    tags = record.tags if isinstance(record.tags, list) else []
    return Memory(
        id=record.id,
        key=record.key,
        content=record.content,
        category=record.category,
        description=record.description,
        tags=[str(tag) for tag in tags],
        created_at=as_utc(record.created_at),
        updated_at=as_utc(record.updated_at),
        enabled=bool(record.enabled),
        priority=int(record.priority),
    )


@dataclass(frozen=True, slots=True)
class PendingMemoryAction:
    """A single-use proposal. Confirmation can change only this stored operation."""

    id: str
    conversation_id: str | None
    intent: str
    operation: str
    status: str
    payload: dict
    target_memory_ids: list[str]
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None


class PendingMemoryActionRecord(Base):
    """Explicit remember or forget proposal awaiting confirmation.

    There is no user account. Chat confirmation is limited to the conversation
    that created the action. The action id is the local confirmation capability.
    """

    __tablename__ = "pending_memory_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    intent: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    target_memory_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def to_pending_action(record: PendingMemoryActionRecord) -> PendingMemoryAction:
    payload = record.payload if isinstance(record.payload, dict) else {}
    raw_ids = record.target_memory_ids if isinstance(record.target_memory_ids, list) else []
    return PendingMemoryAction(
        id=record.id,
        conversation_id=record.conversation_id,
        intent=record.intent,
        operation=record.operation,
        status=record.status,
        payload=payload,
        target_memory_ids=[str(item) for item in raw_ids],
        created_at=as_utc(record.created_at),
        expires_at=as_utc(record.expires_at),
        resolved_at=as_utc(record.resolved_at) if record.resolved_at is not None else None,
    )
