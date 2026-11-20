import asyncio
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.agents.execution import AgentExecutionService
from app.agents.schemas import AgentRead
from app.agents.service import AgentService
from app.conversations.exceptions import (
    AgentConversationConflictError,
    ConversationArchivedError,
    ConversationNotFoundError,
    ConversationPersistenceError,
    InvalidConversationIdError,
    ModelNotConfiguredError,
)
from app.conversations.models import Conversation, Message
from app.conversations.repository import ConversationRepository
from app.conversations.schemas import (
    ConversationListResponse,
    ConversationRead,
    ConversationUpdate,
    MessageListResponse,
    MessageRead,
)
from app.conversations.titles import generate_conversation_title
from app.core.config import get_settings
from app.memories.action_service import MemoryActionService
from app.memories.context import compose_system_prompt
from app.memories.models import Memory
from app.memories.service import MemoryService
from app.routing.schemas import RoutingDecision
from app.routing.service import RoutingService


_turn_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _turn_lock() -> asyncio.Lock:
    """The chat-turn lock of the running event loop (tests start several loops)."""
    loop = asyncio.get_running_loop()
    lock = _turn_locks.get(loop)
    if lock is None:
        lock = _turn_locks[loop] = asyncio.Lock()
    return lock


@dataclass(frozen=True, slots=True)
class ChatTurnResult:
    model: str
    answer: str
    conversation_id: str
    agent_id: str | None
    agent_name: str | None
    memory_intent: str | None = None
    memory_action_id: str | None = None
    confirmation_required: bool | None = None
    listed_memories: tuple[Memory, ...] | None = None
    routing_decision: str | None = None
    selected_agent_id: str | None = None
    selected_agent_name: str | None = None
    routing_reason: str | None = None
    routing_confidence: float | None = None


class ConversationService:
    """Persist chat turns and send a bounded recent window to the model."""

    def __init__(
        self,
        repository: ConversationRepository,
        agents: AgentService,
        execution: AgentExecutionService,
        memories: MemoryService,
        memory_actions: MemoryActionService,
        routing: RoutingService,
    ) -> None:
        self.repository = repository
        self.agents = agents
        self.execution = execution
        self.memories = memories
        self.memory_actions = memory_actions
        self.routing = routing

    async def send(
        self,
        *,
        message: str,
        system_prompt: str | None,
        agent_id: str | None,
        conversation_id: str | None,
    ) -> ChatTurnResult:
        """One chat turn at a time for the whole server.

        A turn keeps its database transaction open while the model answers
        (tools that write during the turn share it). SQLite allows one writer,
        so a second turn started meanwhile failed at once with HTTP 500. Now it
        waits, and it also sees the first turn's answer in its history.
        """
        async with _turn_lock():
            return await self._send(
                message=message, system_prompt=system_prompt, agent_id=agent_id, conversation_id=conversation_id
            )

    async def _send(
        self,
        *,
        message: str,
        system_prompt: str | None,
        agent_id: str | None,
        conversation_id: str | None,
    ) -> ChatTurnResult:
        requested_agent_id = _canonical_id(agent_id) if agent_id is not None else None
        existing = self._load_existing(conversation_id)
        if existing is not None and existing.is_archived:
            raise ConversationArchivedError(existing.id)
        if existing is not None:
            _ensure_same_agent(existing, requested_agent_id)

        explicit = requested_agent_id is not None
        bound_id = existing.agent_id if existing is not None else None
        if explicit:
            agent = self.agents.resolve_for_invocation(requested_agent_id)
        elif bound_id:
            agent = self.agents.resolve_for_invocation(bound_id)
        else:
            agent = None
        settings = get_settings()
        if agent is None and not self.routing.can_route(settings):
            model, prompt = _execution_inputs(None, system_prompt)
        elif agent is not None:
            model, prompt = _execution_inputs(agent, system_prompt)
        else:
            model, prompt = settings.ollama_model, system_prompt
        routing_decision = None
        selected_agent_id = None
        selected_agent_name = None
        routing_reason = None
        routing_confidence = None

        try:
            now = datetime.now(timezone.utc)
            conversation = existing
            if conversation is None:
                conversation = self.repository.create(
                    conversation_id=str(uuid4()),
                    title=generate_conversation_title(message),
                    agent_id=agent.id if agent is not None else None,
                    created_at=now,
                )
            memory_intent = None
            memory_action_id = None
            confirmation_required = None
            listed_memories = None
            clarify_text = None
            if self.memory_actions.intercepts_chat(message, conversation.id):
                self._add_user_message(conversation.id, message, agent.id if agent is not None else None, now)
                command = self.memory_actions.handle_message(
                    message,
                    conversation_id=conversation.id,
                    commit=False,
                )
                answer = command.answer
                agent_name = agent.name if agent is not None else None
                responded_agent_id = agent.id if agent is not None else None
                memory_intent = command.intent
                memory_action_id = command.action_id
                confirmation_required = command.confirmation_required
                if command.memories is not None:
                    listed_memories = tuple(command.memories)
            else:
                started_unbound = agent is None
                if not explicit and self.routing.can_route(settings):
                    prior = self._context_history(conversation.id) if existing is not None else None
                    outcome = await self.routing.choose(message=message, history=prior, settings=settings)
                    if started_unbound and outcome.ran:
                        agent, conversation, clarify_text = self._apply_new_route(outcome, conversation)
                        routing_decision = "clarify" if clarify_text else outcome.decision
                        selected_agent_id = None if clarify_text else outcome.agent_id
                        selected_agent_name = None if clarify_text else outcome.agent_name
                        routing_reason = clarify_text or outcome.reason or None
                        routing_confidence = outcome.confidence
                        if agent is not None:
                            model, prompt = _execution_inputs(agent, system_prompt)
                        elif not model:
                            model = settings.ollama_model or "assistant"
                    elif not started_unbound and outcome.ran:
                        clarify_text = _binding_explanation(agent, outcome)
                        if clarify_text:
                            routing_decision = "clarify"
                            selected_agent_id = agent.id
                            selected_agent_name = agent.name
                            routing_reason = clarify_text
                            routing_confidence = outcome.confidence
                if clarify_text is None and agent is None:
                    model, prompt = _execution_inputs(None, system_prompt)
                self._add_user_message(conversation.id, message, agent.id if agent is not None else None, now)
                history = self._context_history(conversation.id)
                if clarify_text is not None:
                    answer = clarify_text
                    agent_name = agent.name if agent is not None else None
                    responded_agent_id = agent.id if agent is not None else None
                else:
                    memory_block = self.memories.build_injection(
                        message=message,
                        agent_name=agent.name if agent is not None else None,
                        agent_role=agent.role if agent is not None else None,
                    )
                    if agent is not None:
                        result = await self.execution.execute(
                            agent_id=agent.id,
                            message=message,
                            history=history,
                            memory_context=memory_block,
                        )
                        answer = result.answer
                        model = result.model
                        agent_name = result.agent_name
                        responded_agent_id = result.agent_id
                    else:
                        answer = await self.execution.complete(
                            model=model,
                            message=message,
                            system_prompt=compose_system_prompt(prompt, memory_block),
                            history=history,
                            agent_id=None,
                        )
                        agent_name = None
                        responded_agent_id = None
            self.repository.add_message(
                message_id=str(uuid4()),
                conversation_id=conversation.id,
                role="assistant",
                content=answer,
                agent_id=responded_agent_id,
                model=model,
                created_at=datetime.now(timezone.utc),
            )
            self.repository.update(conversation.id, updated_at=datetime.now(timezone.utc))
            self.repository.session.commit()
        except IntegrityError as exc:
            self.repository.session.rollback()
            raise ConversationPersistenceError() from exc
        except SQLAlchemyError as exc:
            self.repository.session.rollback()
            raise ConversationPersistenceError() from exc
        except Exception:
            self.repository.session.rollback()
            raise

        return ChatTurnResult(
            model=model,
            answer=answer,
            conversation_id=conversation.id,
            agent_id=responded_agent_id,
            agent_name=agent_name,
            memory_intent=memory_intent,
            memory_action_id=memory_action_id,
            confirmation_required=confirmation_required,
            listed_memories=listed_memories,
            routing_decision=routing_decision,
            selected_agent_id=selected_agent_id,
            selected_agent_name=selected_agent_name,
            routing_reason=routing_reason,
            routing_confidence=routing_confidence,
        )

    def _add_user_message(self, conversation_id: str, message: str, agent_id: str | None, created_at: datetime) -> None:
        self.repository.add_message(
            message_id=str(uuid4()),
            conversation_id=conversation_id,
            role="user",
            content=message,
            agent_id=agent_id,
            model=None,
            created_at=created_at,
        )

    def _apply_new_route(
        self,
        outcome: RoutingDecision,
        conversation: Conversation,
    ) -> tuple[AgentRead | None, Conversation, str | None]:
        if outcome.decision == "clarify":
            return None, conversation, outcome.clarification or outcome.reason
        if outcome.decision != "agent" or not outcome.agent_id:
            return None, conversation, None
        agent = self.agents.resolve_for_invocation(outcome.agent_id)
        bound = self.repository.bind_agent(conversation.id, agent.id)
        return agent, bound or conversation, None

    def list_conversations(
        self,
        *,
        include_archived: bool,
        limit: int,
        offset: int,
    ) -> ConversationListResponse:
        conversations, total = self.repository.list_conversations(
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
        counts = self.repository.message_counts([conversation.id for conversation in conversations])
        return ConversationListResponse(
            conversations=[_read_conversation(conversation, counts.get(conversation.id, 0)) for conversation in conversations],
            count=total,
            limit=limit,
            offset=offset,
        )

    def get_conversation(self, conversation_id: str) -> ConversationRead:
        conversation = self._require(conversation_id)
        counts = self.repository.message_counts([conversation.id])
        return _read_conversation(conversation, counts.get(conversation.id, 0))

    def list_messages(
        self,
        conversation_id: str,
        *,
        limit: int,
        offset: int,
    ) -> MessageListResponse:
        conversation = self._require(conversation_id)
        messages, total = self.repository.list_messages(conversation.id, limit=limit, offset=offset)
        return MessageListResponse(
            messages=[_read_message(message) for message in messages],
            count=total,
            limit=limit,
            offset=offset,
        )

    def update_conversation(self, conversation_id: str, payload: ConversationUpdate) -> ConversationRead:
        conversation = self._require(conversation_id)
        changes = payload.model_dump(exclude_unset=True)
        updated = self.repository.update(
            conversation.id,
            title=changes.get("title"),
            is_archived=changes.get("is_archived"),
            updated_at=datetime.now(timezone.utc),
        )
        if updated is None:
            raise ConversationNotFoundError(conversation.id)
        try:
            self.repository.session.commit()
        except SQLAlchemyError as exc:
            self.repository.session.rollback()
            raise ConversationPersistenceError() from exc
        counts = self.repository.message_counts([updated.id])
        return _read_conversation(updated, counts.get(updated.id, 0))

    def delete_conversation(self, conversation_id: str) -> None:
        conversation = self._require(conversation_id)
        if not self.repository.delete(conversation.id):
            raise ConversationNotFoundError(conversation.id)
        try:
            self.repository.session.commit()
        except SQLAlchemyError as exc:
            self.repository.session.rollback()
            raise ConversationPersistenceError() from exc

    def _load_existing(self, conversation_id: str | None) -> Conversation | None:
        if conversation_id is None:
            return None
        parsed = _canonical_id(conversation_id)
        conversation = self.repository.get(parsed)
        if conversation is None:
            raise ConversationNotFoundError(parsed)
        return conversation

    def _require(self, conversation_id: str) -> Conversation:
        parsed = _canonical_id(conversation_id)
        conversation = self.repository.get(parsed)
        if conversation is None:
            raise ConversationNotFoundError(parsed)
        return conversation

    def _context_history(self, conversation_id: str) -> list[dict[str, str]]:
        limit = get_settings().chat_context_message_limit
        recent = self.repository.list_recent_messages(conversation_id, limit)
        return [{"role": message.role, "content": message.content} for message in recent]


def _binding_explanation(agent: AgentRead, outcome: RoutingDecision) -> str | None:
    if outcome.decision != "agent" or not outcome.agent_id or outcome.agent_id == agent.id:
        return None
    other = outcome.agent_name or "another agent"
    return (
        f"This conversation is bound to {agent.name} and will stay with that agent. "
        f"The request looks suited to {other}. "
        "Start a new conversation to use that agent."
    )


def _execution_inputs(agent: AgentRead | None, system_prompt: str | None) -> tuple[str, str | None]:
    if agent is not None:
        return agent.model, agent.system_prompt
    settings = get_settings()
    if not settings.ollama_model:
        raise ModelNotConfiguredError()
    return settings.ollama_model, system_prompt


def _ensure_same_agent(conversation: Conversation, requested_agent_id: str | None) -> None:
    bound = conversation.agent_id
    if requested_agent_id is None:
        return
    if bound is None:
        raise AgentConversationConflictError("This conversation is not bound to an agent.")
    if bound != requested_agent_id:
        raise AgentConversationConflictError(
            "This conversation is bound to a different agent and cannot switch agents."
        )


def _canonical_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidConversationIdError() from exc


def _read_conversation(conversation: Conversation, message_count: int) -> ConversationRead:
    return ConversationRead(
        id=conversation.id,
        title=conversation.title,
        agent_id=conversation.agent_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        is_archived=conversation.is_archived,
        message_count=message_count,
    )


def _read_message(message: Message) -> MessageRead:
    return MessageRead(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content,
        agent_id=message.agent_id,
        model=message.model,
        created_at=message.created_at,
        sequence_number=message.sequence_number,
    )
