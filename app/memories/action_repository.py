from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memories.models import PendingMemoryAction, PendingMemoryActionRecord, to_pending_action


class PendingMemoryActionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, action_id: str) -> PendingMemoryAction | None:
        record = self.session.get(PendingMemoryActionRecord, action_id)
        if record is None:
            return None
        return to_pending_action(record)

    def list_pending_for_conversation(self, conversation_id: str) -> list[PendingMemoryAction]:
        statement = (
            select(PendingMemoryActionRecord)
            .where(
                PendingMemoryActionRecord.conversation_id == conversation_id,
                PendingMemoryActionRecord.status == "pending",
            )
            .order_by(PendingMemoryActionRecord.created_at.asc(), PendingMemoryActionRecord.id.asc())
        )
        return [to_pending_action(record) for record in self.session.scalars(statement).all()]

    def add(
        self,
        *,
        action_id: str,
        conversation_id: str | None,
        intent: str,
        operation: str,
        payload: dict,
        target_memory_ids: list[str],
        created_at: datetime,
        expires_at: datetime,
    ) -> PendingMemoryAction:
        record = PendingMemoryActionRecord(
            id=action_id,
            conversation_id=conversation_id,
            intent=intent,
            operation=operation,
            status="pending",
            payload=payload,
            target_memory_ids=target_memory_ids,
            created_at=created_at,
            expires_at=expires_at,
            resolved_at=None,
        )
        self.session.add(record)
        self.session.flush()
        return to_pending_action(record)

    def mark(self, action_id: str, status: str, resolved_at: datetime) -> PendingMemoryAction | None:
        record = self.session.get(PendingMemoryActionRecord, action_id)
        if record is None:
            return None
        record.status = status
        record.resolved_at = resolved_at
        self.session.flush()
        return to_pending_action(record)
