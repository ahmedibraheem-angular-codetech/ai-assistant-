"""Research lifecycle notes in the existing permission audit log.

The detail records the task, provider, operation, and status. It does not
record API keys, request headers, or page text.

A chat request may already hold the SQLite write lock. In that case the note
is added to the request session and committed with the chat. A direct call
commits on its own session.
"""

from contextvars import ContextVar

from app.core.database import get_session_factory
from app.permissions.audit import add_audit
from app.tools.exceptions import ToolExecutionError

_REQUEST_SESSION: ContextVar = ContextVar("research_request_session", default=None)

_START = "The research request could not be recorded."
_FINISH = "The research result could not be recorded."


def bind_request_session(session):
    return _REQUEST_SESSION.set(session)


def reset_request_session(token) -> None:
    _REQUEST_SESSION.reset(token)


def begin_research_audit(task_id: str, agent_id: str | None, providers: str) -> None:
    _write(
        "research_started",
        "started",
        _detail(task_id, providers, "research", "started"),
        agent_id,
        _START,
    )


def finish_research_audit(task_id: str, agent_id: str | None, providers: str, status: str) -> None:
    _write(
        "research_finished",
        status,
        _detail(task_id, providers, "research", status),
        agent_id,
        _FINISH,
    )


def deny_research_audit(agent_id: str | None, decision: str) -> None:
    safe = "".join(character for character in decision if character.isalnum() or character == "_")[:32]
    _write(
        "research_denied",
        safe or "denied",
        "operation=research status=denied",
        agent_id,
        _START,
    )


def provider_label(names: list[str]) -> str:
    clean: list[str] = []
    for name in names:
        token = "".join(character for character in name if character.isalnum() or character in {"_", "-"})[:32]
        if token and token not in clean:
            clean.append(token)
    return ",".join(clean)[:80] or "none"


def _detail(task_id: str, providers: str, operation: str, status: str) -> str:
    return f"task={task_id} provider={providers} operation={operation} status={status}"


def _commit(session) -> None:
    session.commit()


def _write(event: str, decision: str, detail: str, agent_id: str | None, failure: str) -> None:
    current = _REQUEST_SESSION.get()
    if current is not None:
        try:
            add_audit(
                current,
                event=event,
                decision=decision,
                detail=detail,
                agent_id=agent_id,
                tool_name="research",
                resource_type="internet",
                action="read",
            )
        except Exception as exc:
            raise ToolExecutionError(failure) from exc
        return
    last: Exception | None = None
    for _attempt in (1, 2):
        session = get_session_factory()()
        try:
            add_audit(
                session,
                event=event,
                decision=decision,
                detail=detail,
                agent_id=agent_id,
                tool_name="research",
                resource_type="internet",
                action="read",
            )
            _commit(session)
            return
        except Exception as exc:
            session.rollback()
            last = exc
        finally:
            session.close()
    raise ToolExecutionError(failure) from last
