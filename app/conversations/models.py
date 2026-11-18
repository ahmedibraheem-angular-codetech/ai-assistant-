from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    title: str
    agent_id: str | None
    created_at: datetime
    updated_at: datetime
    is_archived: bool


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    conversation_id: str
    role: str
    content: str
    agent_id: str | None
    model: str | None
    created_at: datetime
    sequence_number: int


class ConversationRecord(Base):
    """A chat thread.

    agent_id is fixed after creation. Messages are the transcript only.
    They are not copied into personal memory, preferences, or a retrieval index.
    """

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)

    messages: Mapped[list["MessageRecord"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
    )


class MessageRecord(Base):
    """One turn in a conversation, stored in full regardless of context limits."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence_number", name="uq_messages_conversation_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)

    conversation: Mapped[ConversationRecord] = relationship(back_populates="messages")


def to_conversation(record: ConversationRecord) -> Conversation:
    return Conversation(
        id=record.id,
        title=record.title,
        agent_id=record.agent_id,
        created_at=as_utc(record.created_at),
        updated_at=as_utc(record.updated_at),
        is_archived=bool(record.is_archived),
    )


def to_message(record: MessageRecord) -> Message:
    return Message(
        id=record.id,
        conversation_id=record.conversation_id,
        role=record.role,
        content=record.content,
        agent_id=record.agent_id,
        model=record.model,
        created_at=as_utc(record.created_at),
        sequence_number=record.sequence_number,
    )
