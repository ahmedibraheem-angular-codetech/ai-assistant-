from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.memories.models import Memory, MemoryRecord, to_memory


class MemoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, memory_id: str) -> Memory | None:
        record = self.session.get(MemoryRecord, memory_id)
        if record is None:
            return None
        return to_memory(record)

    def get_by_normalized_key(self, key_normalized: str) -> Memory | None:
        statement = select(MemoryRecord).where(MemoryRecord.key_normalized == key_normalized)
        record = self.session.scalar(statement)
        if record is None:
            return None
        return to_memory(record)

    def list_memories(
        self,
        *,
        category: str | None,
        enabled: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Memory], int]:
        filters = _filters(category, enabled)
        total = self.session.scalar(select(func.count()).select_from(MemoryRecord).where(*filters)) or 0
        statement = (
            select(MemoryRecord)
            .where(*filters)
            .order_by(MemoryRecord.priority.desc(), MemoryRecord.updated_at.desc(), MemoryRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [to_memory(record) for record in self.session.scalars(statement).all()], int(total)

    def list_pool(self, *, category: str | None = None, enabled: bool | None = None) -> list[Memory]:
        statement = (
            select(MemoryRecord)
            .where(*_filters(category, enabled))
            .order_by(MemoryRecord.priority.desc(), MemoryRecord.updated_at.desc())
        )
        return [to_memory(record) for record in self.session.scalars(statement).all()]

    def add(
        self,
        *,
        memory_id: str,
        key: str,
        key_normalized: str,
        content: str,
        category: str,
        description: str | None,
        tags: list[str],
        enabled: bool,
        priority: int,
        created_at: datetime,
    ) -> Memory:
        record = MemoryRecord(
            id=memory_id,
            key=key,
            key_normalized=key_normalized,
            content=content,
            category=category,
            description=description,
            tags=tags,
            enabled=enabled,
            priority=priority,
            created_at=created_at,
            updated_at=created_at,
        )
        self.session.add(record)
        self.session.flush()
        return to_memory(record)

    def update(
        self,
        memory_id: str,
        changes: dict[str, object],
        updated_at: datetime,
    ) -> Memory | None:
        record = self.session.get(MemoryRecord, memory_id)
        if record is None:
            return None
        allowed = {
            "key",
            "key_normalized",
            "content",
            "category",
            "description",
            "tags",
            "enabled",
            "priority",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported memory fields: {', '.join(sorted(unknown))}")
        for field_name, value in changes.items():
            setattr(record, field_name, value)
        record.updated_at = updated_at
        self.session.flush()
        return to_memory(record)

    def delete(self, memory_id: str) -> bool:
        record = self.session.get(MemoryRecord, memory_id)
        if record is None:
            return False
        self.session.delete(record)
        self.session.flush()
        return True


def _filters(category: str | None, enabled: bool | None) -> list:
    filters = []
    if category is not None:
        filters.append(MemoryRecord.category == category)
    if enabled is not None:
        filters.append(MemoryRecord.enabled.is_(enabled))
    return filters
