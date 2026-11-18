"""Confirmation-gated memory changes.

The language model never calls memory mutation methods. A proposal stores the
exact operation and target ids, and confirmation executes only that record.
Chat confirmation is limited to the conversation that created the action.
There is no user authentication: anyone who can call this local API and
present the action id can confirm it.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.memories.action_repository import PendingMemoryActionRepository
from app.memories.exceptions import (
    InvalidMemoryActionIdError,
    MemoryActionNotFoundError,
    MemoryActionStateError,
    MemoryPersistenceError,
)
from app.memories.intents import (
    is_cancellation,
    is_confirmation,
    parse_memory_intent,
    suggest_memory_fields,
)
from app.memories.models import Memory, PendingMemoryAction, as_utc
from app.memories.schemas import MemoryCreate
from app.memories.service import MemoryService

_TOKEN = re.compile(r"[a-z0-9]+")
_FILLERS = frozenset({"old", "my", "the", "a", "an", "please", "just", "that", "about", "saved"})
_STATE_MESSAGES = {
    "expired": "This memory action has expired.",
    "confirmed": "This memory action was already confirmed.",
    "cancelled": "This memory action was cancelled.",
}


@dataclass(frozen=True, slots=True)
class MemoryCommandOutcome:
    handled: bool
    answer: str = ""
    intent: str | None = None
    action_id: str | None = None
    confirmation_required: bool | None = None
    memories: list[Memory] | None = None


@dataclass(frozen=True, slots=True)
class ActionProposal:
    intent: str
    operation: str | None
    status: str | None
    confirmation_required: bool
    action_id: str | None
    expires_at: datetime | None
    clarification: str | None
    proposed_memory: dict | None
    matching_memories: list[Memory]
    count: int
    limit: int
    offset: int
    answer: str


@dataclass(frozen=True, slots=True)
class ActionExecution:
    action_id: str
    status: str
    intent: str
    operation: str
    memories: list[Memory]
    answer: str


class MemoryActionService:
    def __init__(self, repository: PendingMemoryActionRepository, memories: MemoryService) -> None:
        self.repository = repository
        self.memories = memories

    def propose(
        self,
        message: str,
        *,
        conversation_id: str | None,
        limit: int,
        offset: int,
        commit: bool = True,
    ) -> ActionProposal:
        parsed = parse_memory_intent(message)
        if parsed.intent.value == "normal_chat":
            return _empty_proposal("normal_chat", "That is not a memory command.")
        if parsed.ambiguous:
            return _clarification(parsed.intent.value, parsed.clarification or "Which memory do you mean?")
        if parsed.intent.value == "list_memories":
            return self._list(parsed.category, parsed.list_all, limit, offset)
        if parsed.intent.value == "remember":
            return self._propose_remember(parsed.subject or "", conversation_id, commit=commit)
        return self._propose_target(
            intent=parsed.intent.value,
            operation={"forget": "delete", "disable_memory": "disable", "enable_memory": "enable"}[parsed.intent.value],
            subject=parsed.subject or "",
            conversation_id=conversation_id,
            commit=commit,
        )

    def intercepts_chat(self, message: str, conversation_id: str) -> bool:
        """True when the message is an explicit memory command and must not be routed."""
        if is_confirmation(message) or is_cancellation(message):
            return bool(self.repository.list_pending_for_conversation(conversation_id))
        return parse_memory_intent(message).intent.value != "normal_chat"

    def handle_message(self, message: str, *, conversation_id: str, commit: bool = False) -> MemoryCommandOutcome:
        if is_confirmation(message) or is_cancellation(message):
            return self._resolve_in_conversation(
                conversation_id,
                confirm=is_confirmation(message),
                commit=commit,
            )
        proposal = self.propose(
            message,
            conversation_id=conversation_id,
            limit=20,
            offset=0,
            commit=commit,
        )
        if proposal.intent == "normal_chat":
            return MemoryCommandOutcome(handled=False)
        memories = proposal.matching_memories if proposal.intent != "remember" else None
        return MemoryCommandOutcome(
            handled=True,
            answer=proposal.answer,
            intent=proposal.intent,
            action_id=proposal.action_id,
            confirmation_required=proposal.confirmation_required,
            memories=memories,
        )

    def get_action(self, action_id: str) -> ActionProposal:
        action = self._load(action_id)
        return _view(action)

    def confirm(self, action_id: str, *, commit: bool = True) -> ActionExecution:
        action = self._require_pending(action_id, commit=commit)
        try:
            memories = self._execute_stored(action)
            self.repository.mark(action.id, "confirmed", datetime.now(timezone.utc))
            if commit:
                self.repository.session.commit()
        except SQLAlchemyError as exc:
            self.repository.session.rollback()
            raise MemoryPersistenceError() from exc
        except Exception:
            self.repository.session.rollback()
            raise
        answer = _completion_answer(action.operation, memories)
        return ActionExecution(action.id, "confirmed", action.intent, action.operation, memories, answer)

    def cancel(self, action_id: str, *, commit: bool = True) -> ActionExecution:
        action = self._require_pending(action_id, commit=commit)
        try:
            self.repository.mark(action.id, "cancelled", datetime.now(timezone.utc))
            if commit:
                self.repository.session.commit()
        except SQLAlchemyError as exc:
            self.repository.session.rollback()
            raise MemoryPersistenceError() from exc
        snapshots = _snapshots(action)
        return ActionExecution(
            action.id,
            "cancelled",
            action.intent,
            action.operation,
            snapshots,
            "Cancelled. Nothing was changed.",
        )

    def _propose_remember(self, fact: str, conversation_id: str | None, *, commit: bool) -> ActionProposal:
        fields = suggest_memory_fields(fact)
        if not str(fields["content"]).strip():
            return _clarification("remember", "What should I remember? Say it as: Remember that ...")
        try:
            preview = MemoryCreate.model_validate(fields)
        except ValidationError:
            return _clarification("remember", "That fact cannot be saved as a single memory. Shorten it and try again.")
        normalized = " ".join(preview.key.casefold().split())
        if self.memories.repository.get_by_normalized_key(normalized) is not None:
            return _clarification(
                "remember",
                f"A saved memory already uses the key '{preview.key}'. Nothing new was saved.",
            )
        stored = preview.model_dump(mode="json")
        answer = _remember_answer(stored)
        action = self._store(
            conversation_id=conversation_id,
            intent="remember",
            operation="create",
            payload={"memory": stored, "answer": answer, "clarification": None, "snapshots": []},
            target_memory_ids=[],
            commit=commit,
        )
        return ActionProposal(
            intent="remember",
            operation="create",
            status="pending",
            confirmation_required=True,
            action_id=action.id,
            expires_at=action.expires_at,
            clarification=None,
            proposed_memory=stored,
            matching_memories=[],
            count=0,
            limit=20,
            offset=0,
            answer=f"{answer}\nAction: {action.id}\nExpires: {action.expires_at.isoformat()}",
        )

    def _propose_target(
        self,
        *,
        intent: str,
        operation: str,
        subject: str,
        conversation_id: str | None,
        commit: bool,
    ) -> ActionProposal:
        matches = _select_targets(self.memories.repository.list_pool(), subject)
        if not matches:
            return _clarification(intent, f"I could not find a saved memory matching '{subject}'. Nothing was changed.")
        if len(matches) > 1:
            return _clarification(
                intent,
                "I found more than one matching memory, so I did not change anything.\n"
                + _memory_lines(matches)
                + "\nName the specific fact you mean.",
                matches,
            )
        memory = matches[0]
        if operation == "disable" and not memory.enabled:
            return _clarification(intent, f"'{memory.key}' is already disabled. Nothing was changed.", matches)
        if operation == "enable" and memory.enabled:
            return _clarification(intent, f"'{memory.key}' is already enabled. Nothing was changed.", matches)
        snapshot = _snapshot(memory)
        verb = {"delete": "delete", "disable": "stop using", "enable": "use again"}[operation]
        answer = (
            f"I found one memory. Nothing has been changed yet.\n{_memory_lines(matches)}\n"
            f"Reply confirm to {verb} it, or cancel to keep it unchanged."
        )
        action = self._store(
            conversation_id=conversation_id,
            intent=intent,
            operation=operation,
            payload={"memory": None, "answer": answer, "clarification": None, "snapshots": [snapshot]},
            target_memory_ids=[memory.id],
            commit=commit,
        )
        return ActionProposal(
            intent=intent,
            operation=operation,
            status="pending",
            confirmation_required=True,
            action_id=action.id,
            expires_at=action.expires_at,
            clarification=None,
            proposed_memory=None,
            matching_memories=[memory],
            count=1,
            limit=20,
            offset=0,
            answer=f"{answer}\nAction: {action.id}\nExpires: {action.expires_at.isoformat()}",
        )

    def _list(self, category: str | None, list_all: bool, limit: int, offset: int) -> ActionProposal:
        items, count = self.memories.list_memories(
            category=None if list_all else category,
            enabled=None,
            limit=limit,
            offset=offset,
        )
        if count == 0:
            answer = "I do not have any saved memories for that yet."
        else:
            answer = f"I have {count} saved memories.\n{_memory_lines(items)}"
            if offset + len(items) < count:
                answer += "\nMore memories are available on the next page."
        return ActionProposal(
            intent="list_memories",
            operation="list",
            status=None,
            confirmation_required=False,
            action_id=None,
            expires_at=None,
            clarification=None,
            proposed_memory=None,
            matching_memories=items,
            count=count,
            limit=limit,
            offset=offset,
            answer=answer,
        )

    def _store(
        self,
        *,
        conversation_id: str | None,
        intent: str,
        operation: str,
        payload: dict,
        target_memory_ids: list[str],
        commit: bool,
    ) -> PendingMemoryAction:
        now = datetime.now(timezone.utc)
        try:
            action = self.repository.add(
                action_id=str(uuid4()),
                conversation_id=conversation_id,
                intent=intent,
                operation=operation,
                payload=payload,
                target_memory_ids=target_memory_ids,
                created_at=now,
                expires_at=now + timedelta(seconds=get_settings().memory_action_ttl_seconds),
            )
            if commit:
                self.repository.session.commit()
        except SQLAlchemyError as exc:
            self.repository.session.rollback()
            raise MemoryPersistenceError() from exc
        return action

    def _resolve_in_conversation(self, conversation_id: str, *, confirm: bool, commit: bool) -> MemoryCommandOutcome:
        pending = self.repository.list_pending_for_conversation(conversation_id)
        live = [item for item in pending if _effective_status(item) == "pending"]
        expired = [item for item in pending if _effective_status(item) == "expired"]
        if len(live) > 1:
            lines = "\n".join(f"- {item.id} ({item.operation})" for item in live)
            return MemoryCommandOutcome(
                handled=True,
                answer=(
                    "More than one memory action is waiting in this conversation. "
                    "Confirm a specific action id instead of guessing.\n" + lines
                ),
                confirmation_required=True,
            )
        if len(live) == 1:
            result = (
                self.confirm(live[0].id, commit=commit) if confirm else self.cancel(live[0].id, commit=commit)
            )
            return MemoryCommandOutcome(
                handled=True,
                answer=result.answer,
                intent=result.intent,
                action_id=result.action_id,
                confirmation_required=False,
                memories=result.memories,
            )
        if expired:
            return MemoryCommandOutcome(
                handled=True,
                answer="That memory action has expired. Nothing was changed.",
                intent=expired[0].intent,
                action_id=expired[0].id,
                confirmation_required=False,
            )
        return MemoryCommandOutcome(handled=False)

    def _execute_stored(self, action: PendingMemoryAction) -> list[Memory]:
        if action.operation == "create":
            data = action.payload.get("memory")
            if not isinstance(data, dict):
                raise MemoryActionStateError("This memory action cannot be executed.")
            created = self.memories.create(
                MemoryCreate(
                    key=str(data["key"]),
                    content=str(data["content"]),
                    category=str(data["category"]),
                    description=data.get("description"),
                    tags=[str(tag) for tag in data.get("tags") or []],
                    enabled=True,
                    priority=int(data.get("priority") or 0),
                ),
                commit=False,
            )
            return [created]
        ids = list(action.target_memory_ids)
        if action.operation == "delete":
            snapshots = [item for item in _snapshots(action) if item.id in set(ids)]
            for memory_id in ids:
                self.memories.delete(memory_id, commit=False)
            return snapshots
        if action.operation == "disable":
            return [self.memories.set_enabled(memory_id, False, commit=False) for memory_id in ids]
        if action.operation == "enable":
            return [self.memories.set_enabled(memory_id, True, commit=False) for memory_id in ids]
        raise MemoryActionStateError("This memory action cannot be executed.")

    def _require_pending(self, action_id: str, *, commit: bool) -> PendingMemoryAction:
        action = self._load(action_id)
        status = _effective_status(action)
        if status == "pending":
            return action
        if status == "expired" and action.status == "pending":
            self.repository.mark(action.id, "expired", datetime.now(timezone.utc))
            if commit:
                self.repository.session.commit()
        raise MemoryActionStateError(_STATE_MESSAGES.get(status, "This memory action cannot be executed."))

    def _load(self, action_id: str) -> PendingMemoryAction:
        parsed = _parse_action_id(action_id)
        action = self.repository.get(parsed)
        if action is None:
            raise MemoryActionNotFoundError(parsed)
        return action


def _select_targets(memories: list[Memory], subject: str) -> list[Memory]:
    phrase = " ".join(subject.casefold().split())
    phrase_hits = [memory for memory in memories if phrase and phrase in _haystack(memory)]
    if phrase_hits:
        return phrase_hits
    tokens = [
        token
        for token in _TOKEN.findall(phrase)
        if len(token) >= 3 and token not in _FILLERS
    ]
    if not tokens:
        return []
    required = set(tokens)
    return [memory for memory in memories if required <= set(_TOKEN.findall(_haystack(memory)))]


def _haystack(memory: Memory) -> str:
    tags = " ".join(memory.tags)
    return " ".join(f"{memory.key} {memory.content} {memory.description or ''} {tags}".casefold().split())


def _effective_status(action: PendingMemoryAction) -> str:
    if action.status == "pending" and action.expires_at <= datetime.now(timezone.utc):
        return "expired"
    return action.status


def _snapshot(memory: Memory) -> dict:
    return {
        "id": memory.id,
        "key": memory.key,
        "content": memory.content,
        "category": memory.category,
        "description": memory.description,
        "tags": list(memory.tags),
        "created_at": memory.created_at.isoformat(),
        "updated_at": memory.updated_at.isoformat(),
        "enabled": memory.enabled,
        "priority": memory.priority,
    }


def _memory_from_snapshot(data: dict) -> Memory:
    return Memory(
        id=str(data["id"]),
        key=str(data["key"]),
        content=str(data["content"]),
        category=str(data["category"]),
        description=data.get("description"),
        tags=[str(tag) for tag in data.get("tags") or []],
        created_at=as_utc(data["created_at"]),
        updated_at=as_utc(data["updated_at"]),
        enabled=bool(data["enabled"]),
        priority=int(data["priority"]),
    )


def _snapshots(action: PendingMemoryAction) -> list[Memory]:
    raw = action.payload.get("snapshots") or []
    if not isinstance(raw, list):
        return []
    return [_memory_from_snapshot(item) for item in raw if isinstance(item, dict)]


def _view(action: PendingMemoryAction) -> ActionProposal:
    status = _effective_status(action)
    memory = action.payload.get("memory") if isinstance(action.payload.get("memory"), dict) else None
    snapshots = _snapshots(action)
    return ActionProposal(
        intent=action.intent,
        operation=action.operation,
        status=status,
        confirmation_required=status == "pending",
        action_id=action.id,
        expires_at=action.expires_at,
        clarification=action.payload.get("clarification"),
        proposed_memory=memory,
        matching_memories=snapshots,
        count=len(snapshots),
        limit=20,
        offset=0,
        answer=str(action.payload.get("answer") or ""),
    )


def _remember_answer(stored: dict) -> str:
    tags = ", ".join(stored.get("tags") or []) or "none"
    return (
        "I can save this memory after you confirm. Nothing has been saved yet.\n"
        f"Key: {stored['key']}\n"
        f"Category: {stored['category']}\n"
        f"Content: {stored['content']}\n"
        f"Description: {stored.get('description') or 'none'}\n"
        f"Tags: {tags}\n"
        "Reply confirm to save it, or cancel to discard it."
    )


def _completion_answer(operation: str, memories: list[Memory]) -> str:
    label = memories[0].key if memories else "the memory"
    if operation == "create":
        return f"Saved memory '{label}'."
    if operation == "delete":
        return f"Deleted memory '{label}'."
    if operation == "disable":
        return f"Disabled memory '{label}'. It will no longer be used in chat."
    if operation == "enable":
        return f"Enabled memory '{label}'."
    return "The memory action was completed."


def _memory_lines(memories: list[Memory]) -> str:
    lines = []
    for memory in memories:
        state = "enabled" if memory.enabled else "disabled"
        lines.append(f"- [{state}] {memory.key} ({memory.category}): {memory.content}")
    return "\n".join(lines)


def _clarification(intent: str, answer: str, matches: list[Memory] | None = None) -> ActionProposal:
    found = matches or []
    return ActionProposal(
        intent=intent,
        operation=None,
        status=None,
        confirmation_required=False,
        action_id=None,
        expires_at=None,
        clarification=answer,
        proposed_memory=None,
        matching_memories=found,
        count=len(found),
        limit=20,
        offset=0,
        answer=answer,
    )


def _empty_proposal(intent: str, answer: str) -> ActionProposal:
    return ActionProposal(
        intent=intent,
        operation=None,
        status=None,
        confirmation_required=False,
        action_id=None,
        expires_at=None,
        clarification=None,
        proposed_memory=None,
        matching_memories=[],
        count=0,
        limit=20,
        offset=0,
        answer=answer,
    )


def _parse_action_id(action_id: str) -> str:
    try:
        return str(UUID(action_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidMemoryActionIdError() from exc
