from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.config import get_settings
from app.memories.context import render_memory_context
from app.memories.exceptions import (
    DuplicateMemoryKeyError,
    InvalidMemoryIdError,
    MemoryNotFoundError,
    MemoryPersistenceError,
)
from app.memories.models import Memory
from app.memories.repository import MemoryRepository
from app.memories.retrieval import KeywordMemoryRetriever, MemoryQuery, MemoryRetriever
from app.memories.schemas import MemoryCategory, MemoryCreate, MemoryUpdate


class MemoryService:
    def __init__(self, repository: MemoryRepository, retriever: MemoryRetriever | None = None) -> None:
        self.repository = repository
        self.retriever = retriever or KeywordMemoryRetriever()

    def create(self, payload: MemoryCreate, *, commit: bool = True) -> Memory:
        normalized = _normalize_key(payload.key)
        if self.repository.get_by_normalized_key(normalized) is not None:
            raise DuplicateMemoryKeyError(payload.key)
        try:
            memory = self.repository.add(
                memory_id=str(uuid4()),
                key=payload.key,
                key_normalized=normalized,
                content=payload.content,
                category=payload.category.value,
                description=payload.description,
                tags=payload.tags,
                enabled=payload.enabled,
                priority=payload.priority,
                created_at=datetime.now(timezone.utc),
            )
            if commit:
                self.repository.session.commit()
        except IntegrityError as exc:
            self.repository.session.rollback()
            raise DuplicateMemoryKeyError(payload.key) from exc
        except SQLAlchemyError as exc:
            self.repository.session.rollback()
            raise MemoryPersistenceError() from exc
        return memory

    def get(self, memory_id: str) -> Memory:
        memory = self.repository.get(_parse_id(memory_id))
        if memory is None:
            raise MemoryNotFoundError(memory_id)
        return memory

    def list_memories(
        self,
        *,
        category: str | None,
        enabled: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Memory], int]:
        return self.repository.list_memories(
            category=category,
            enabled=enabled,
            limit=limit,
            offset=offset,
        )

    def update(self, memory_id: str, payload: MemoryUpdate, *, commit: bool = True) -> Memory:
        parsed_id = _parse_id(memory_id)
        current = self.repository.get(parsed_id)
        if current is None:
            raise MemoryNotFoundError(memory_id)

        changes = payload.model_dump(exclude_unset=True)
        if "key" in changes:
            normalized = _normalize_key(str(changes["key"]))
            existing = self.repository.get_by_normalized_key(normalized)
            if existing is not None and existing.id != parsed_id:
                raise DuplicateMemoryKeyError(str(changes["key"]))
            changes["key_normalized"] = normalized
        if "category" in changes and changes["category"] is not None:
            category = changes["category"]
            changes["category"] = category.value if isinstance(category, MemoryCategory) else str(category)

        try:
            updated = self.repository.update(parsed_id, changes, datetime.now(timezone.utc))
            if commit:
                self.repository.session.commit()
        except IntegrityError as exc:
            self.repository.session.rollback()
            raise DuplicateMemoryKeyError(str(changes.get("key", current.key))) from exc
        except SQLAlchemyError as exc:
            self.repository.session.rollback()
            raise MemoryPersistenceError() from exc
        if updated is None:
            raise MemoryNotFoundError(memory_id)
        return updated

    def delete(self, memory_id: str, *, commit: bool = True) -> None:
        parsed_id = _parse_id(memory_id)
        try:
            deleted = self.repository.delete(parsed_id)
            if commit:
                self.repository.session.commit()
        except SQLAlchemyError as exc:
            self.repository.session.rollback()
            raise MemoryPersistenceError() from exc
        if not deleted:
            raise MemoryNotFoundError(memory_id)

    def set_enabled(self, memory_id: str, enabled: bool, *, commit: bool = True) -> Memory:
        return self.update(memory_id, MemoryUpdate(enabled=enabled), commit=commit)

    def search(
        self,
        *,
        query: str,
        category: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Memory], int]:
        pool = self.repository.list_pool(category=category, enabled=None)
        ranked = self.retriever.rank(pool, MemoryQuery(text=query))
        return ranked[offset : offset + limit], len(ranked)

    def build_injection(
        self,
        *,
        message: str,
        agent_name: str | None = None,
        agent_role: str | None = None,
    ) -> str | None:
        enabled = self.repository.list_pool(enabled=True)
        limit = get_settings().memory_injection_limit
        selected = self.retriever.rank(
            enabled,
            MemoryQuery(text=message, agent_name=agent_name, agent_role=agent_role),
        )[:limit]
        if not selected:
            return None
        return render_memory_context(selected)


def _normalize_key(key: str) -> str:
    return " ".join(key.casefold().split())


def _parse_id(memory_id: str) -> str:
    from uuid import UUID

    try:
        return str(UUID(memory_id))
    except ValueError as exc:
        raise InvalidMemoryIdError() from exc
