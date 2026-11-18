from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.conversations.models import (
    Conversation,
    ConversationRecord,
    Message,
    MessageRecord,
    to_conversation,
    to_message,
)


class ConversationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, conversation_id: str) -> Conversation | None:
        record = self.session.get(ConversationRecord, conversation_id)
        if record is None:
            return None
        return to_conversation(record)

    def list_conversations(
        self,
        *,
        include_archived: bool,
        limit: int,
        offset: int,
    ) -> tuple[list[Conversation], int]:
        filters = []
        if not include_archived:
            filters.append(ConversationRecord.is_archived.is_(False))
        total = self.session.scalar(select(func.count()).select_from(ConversationRecord).where(*filters)) or 0
        statement = (
            select(ConversationRecord)
            .where(*filters)
            .order_by(ConversationRecord.updated_at.desc(), ConversationRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
        conversations = [to_conversation(record) for record in self.session.scalars(statement).all()]
        return conversations, int(total)

    def message_counts(self, conversation_ids: list[str]) -> dict[str, int]:
        if not conversation_ids:
            return {}
        statement = (
            select(MessageRecord.conversation_id, func.count())
            .where(MessageRecord.conversation_id.in_(conversation_ids))
            .group_by(MessageRecord.conversation_id)
        )
        return {conversation_id: int(count) for conversation_id, count in self.session.execute(statement).all()}

    def create(
        self,
        *,
        conversation_id: str,
        title: str,
        agent_id: str | None,
        created_at: datetime,
    ) -> Conversation:
        record = ConversationRecord(
            id=conversation_id,
            title=title,
            agent_id=agent_id,
            created_at=created_at,
            updated_at=created_at,
            is_archived=False,
        )
        self.session.add(record)
        self.session.flush()
        return to_conversation(record)

    def update(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        is_archived: bool | None = None,
        updated_at: datetime | None = None,
    ) -> Conversation | None:
        record = self.session.get(ConversationRecord, conversation_id)
        if record is None:
            return None
        if title is not None:
            record.title = title
        if is_archived is not None:
            record.is_archived = is_archived
        if updated_at is not None:
            record.updated_at = updated_at
        self.session.flush()
        return to_conversation(record)

    def bind_agent(self, conversation_id: str, agent_id: str) -> Conversation | None:
        """Attach an agent once. A conversation that already has an agent is left unchanged."""
        record = self.session.get(ConversationRecord, conversation_id)
        if record is None:
            return None
        if record.agent_id is None:
            record.agent_id = agent_id
            self.session.flush()
        return to_conversation(record)

    def delete(self, conversation_id: str) -> bool:
        record = self.session.get(ConversationRecord, conversation_id)
        if record is None:
            return False
        self.session.delete(record)
        self.session.flush()
        return True

    def add_message(
        self,
        *,
        message_id: str,
        conversation_id: str,
        role: str,
        content: str,
        agent_id: str | None,
        model: str | None,
        created_at: datetime,
    ) -> Message:
        sequence_number = self._next_sequence(conversation_id)
        record = MessageRecord(
            id=message_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            agent_id=agent_id,
            model=model,
            created_at=created_at,
            sequence_number=sequence_number,
        )
        self.session.add(record)
        self.session.flush()
        return to_message(record)

    def list_messages(
        self,
        conversation_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[Message], int]:
        total = self.session.scalar(
            select(func.count())
            .select_from(MessageRecord)
            .where(MessageRecord.conversation_id == conversation_id)
        ) or 0
        statement = (
            select(MessageRecord)
            .where(MessageRecord.conversation_id == conversation_id)
            .order_by(MessageRecord.sequence_number.asc())
            .limit(limit)
            .offset(offset)
        )
        messages = [to_message(record) for record in self.session.scalars(statement).all()]
        return messages, int(total)

    def list_recent_messages(self, conversation_id: str, limit: int) -> list[Message]:
        statement = (
            select(MessageRecord)
            .where(MessageRecord.conversation_id == conversation_id)
            .order_by(MessageRecord.sequence_number.desc())
            .limit(limit)
        )
        records = list(self.session.scalars(statement).all())
        records.reverse()
        return [to_message(record) for record in records]

    def _next_sequence(self, conversation_id: str) -> int:
        current = self.session.scalar(
            select(func.max(MessageRecord.sequence_number)).where(
                MessageRecord.conversation_id == conversation_id
            )
        )
        return int(current or 0) + 1
