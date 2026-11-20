"""Assistant controls: a global pause (kill switch) and daily budgets per agent.

- Pause: while paused, no tool starts and no model call is made. Pausing also
  kills running file workers. Resume to continue. The flag survives restarts.
- Budgets: optional daily limits per agent for tool runs and chat turns.
  The default (no row) is unlimited unless AGENT_DAILY_TOOL_CALLS /
  AGENT_DAILY_CHAT_TURNS set a global default.

Settings live in the database and are cached in memory. Usage counters are
kept in memory only (they reset on restart and at UTC midnight), so a running
tool never needs a SQLite write lock that the request may already hold.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.database import Base

_PAUSED_KEY = "paused"
_BUDGET_PREFIX = "budget:"


class AssistantControlRecord(Base):
    __tablename__ = "assistant_controls"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AssistantBlockedError(RuntimeError):
    """The assistant is paused or an agent is out of budget."""


@dataclass
class Budget:
    daily_tool_calls: int | None = None
    daily_chat_turns: int | None = None


_lock = threading.Lock()
_loaded = False
_paused: dict | None = None
_budgets: dict[str, Budget] = {}
_usage: dict[tuple[str, str], list[int]] = {}

PAUSED_MESSAGE = "The assistant is paused. Resume it to continue."


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ loading


def load(session: Session | None = None) -> None:
    """Read controls from the database into memory. Called at startup."""
    global _loaded, _paused, _budgets
    own = session is None
    if own:
        from app.core.database import get_session_factory

        session = get_session_factory()()
    try:
        rows = session.scalars(select(AssistantControlRecord)).all()
        paused = None
        budgets: dict[str, Budget] = {}
        for row in rows:
            value = json.loads(row.value)
            if row.key == _PAUSED_KEY:
                paused = value if value.get("paused") else None
            elif row.key.startswith(_BUDGET_PREFIX):
                budgets[row.key[len(_BUDGET_PREFIX):]] = Budget(**value)
    finally:
        if own:
            session.close()
    with _lock:
        _paused = paused
        _budgets = budgets
        _usage.clear()
        _loaded = True


def reset_cache() -> None:
    global _loaded, _paused, _budgets
    with _lock:
        _loaded = False
        _paused = None
        _budgets = {}
        _usage.clear()


def _ensure_loaded() -> None:
    """Lazy load outside the server (scripts, unit tests).

    The server loads strictly at startup, after the schema exists. Without a
    controls table there is no stored pause or budget, so the state is empty.
    """
    global _loaded
    if _loaded:
        return
    from sqlalchemy.exc import OperationalError

    try:
        load()
    except OperationalError:
        with _lock:
            _usage.clear()
            _loaded = True


# ------------------------------------------------------------------ pause


def pause_state() -> dict:
    _ensure_loaded()
    with _lock:
        if _paused is None:
            return {"paused": False}
        return dict(_paused)


def is_paused() -> bool:
    return pause_state()["paused"]


def _store(session: Session, key: str, value: dict | None) -> None:
    row = session.get(AssistantControlRecord, key)
    if value is None:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        session.add(AssistantControlRecord(key=key, value=json.dumps(value), updated_at=_now()))
    else:
        row.value = json.dumps(value)
        row.updated_at = _now()


def set_paused(session: Session, paused: bool, reason: str | None = None) -> dict:
    from app.permissions.audit import add_audit

    global _paused
    value = {"paused": True, "reason": (reason or "")[:200], "since": _now().isoformat()} if paused else None
    _store(session, _PAUSED_KEY, value)
    add_audit(
        session,
        event="assistant_paused" if paused else "assistant_resumed",
        detail=(reason or "")[:120] or ("paused" if paused else "resumed"),
    )
    session.commit()
    _ensure_loaded()
    with _lock:
        _paused = value
    if paused:
        from app.tools.runtime import stop_file_workers

        stop_file_workers()
    return pause_state()


# ------------------------------------------------------------------ budgets


def _default_budget() -> Budget:
    from app.core.config import get_settings

    settings = get_settings()
    return Budget(
        daily_tool_calls=settings.agent_daily_tool_calls,
        daily_chat_turns=settings.agent_daily_chat_turns,
    )


def budget_for(agent_id: str) -> Budget:
    _ensure_loaded()
    with _lock:
        stored = _budgets.get(agent_id)
    return stored if stored is not None else _default_budget()


def set_budget(session: Session, agent_id: str, budget: Budget | None) -> dict:
    from app.permissions.audit import add_audit

    key = _BUDGET_PREFIX + agent_id
    _store(session, key, None if budget is None else budget.__dict__)
    add_audit(
        session,
        event="agent_budget_set" if budget is not None else "agent_budget_cleared",
        detail="" if budget is None else f"tools={budget.daily_tool_calls} turns={budget.daily_chat_turns}",
        agent_id=agent_id,
    )
    session.commit()
    _ensure_loaded()
    with _lock:
        if budget is None:
            _budgets.pop(agent_id, None)
        else:
            _budgets[agent_id] = budget
    return usage_for(agent_id)


def usage_for(agent_id: str) -> dict:
    budget = budget_for(agent_id)
    with _lock:
        used = list(_usage.get((agent_id, _today()), [0, 0]))
    return {
        "agent_id": agent_id,
        "day": _today(),
        "tool_calls": used[0],
        "chat_turns": used[1],
        "daily_tool_calls": budget.daily_tool_calls,
        "daily_chat_turns": budget.daily_chat_turns,
    }


def _consume(agent_id: str | None, index: int, limit_name: str, label: str) -> str | None:
    if is_paused():
        return PAUSED_MESSAGE
    if agent_id is None:
        return None
    limit = getattr(budget_for(agent_id), limit_name)
    key = (agent_id, _today())
    with _lock:
        for stale in [item for item in _usage if item[1] != key[1]]:
            del _usage[stale]
        used = _usage.setdefault(key, [0, 0])
        if limit is not None and used[index] >= limit:
            return f"This agent reached its daily {label} limit ({limit})."
        used[index] += 1
    return None


def consume_tool_call(agent_id: str | None) -> str | None:
    """Return a refusal message, or None after counting one tool run."""
    return _consume(agent_id, 0, "daily_tool_calls", "tool")


def consume_chat_turn(agent_id: str | None) -> str | None:
    """Return a refusal message, or None after counting one chat turn."""
    return _consume(agent_id, 1, "daily_chat_turns", "chat")
