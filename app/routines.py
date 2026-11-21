"""Scheduled routines (phase 7, slice 1).

A routine is a saved prompt that runs as a normal chat turn at a time of day.
It goes through `ConversationService.send`, so the pause switch, daily budgets
and approvals all apply: a routine can ask for approval but never bypasses it.
Only the local admin creates routines (no tool, no agent access).

Catch-up: if the Mac was asleep at the scheduled moment, the routine runs once
when it wakes, but only within `routine_catchup_hours`. Missed days are never
backfilled.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, DateTime, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.database import Base

logger = logging.getLogger(__name__)
TICK_SECONDS = 30
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class RoutineRecord(Base):
    __tablename__ = "routines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    time_of_day: Mapped[str] = mapped_column(String(5), nullable=False)
    days: Mapped[str] = mapped_column(String(13), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(8), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse_days(days: str) -> set[int]:
    """'' means every day. Otherwise a comma list of 0-6 (Monday = 0)."""
    text = (days or "").strip()
    if not text:
        return set(range(7))
    return {int(part) for part in text.split(",")}


def normalize_days(days: list[int]) -> str:
    unique = sorted(set(days))
    return "" if len(unique) == 7 else ",".join(str(d) for d in unique)


def _scheduled_moment(routine, day, tz: ZoneInfo) -> datetime:
    hour, minute = (int(p) for p in routine.time_of_day.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=tz)


def next_due(routine, now: datetime, tz: ZoneInfo) -> datetime:
    """The next scheduled moment strictly after `now` (for display)."""
    local = _aware(now).astimezone(tz)
    allowed = parse_days(routine.days)
    for offset in range(0, 8):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in allowed:
            continue
        moment = _scheduled_moment(routine, day, tz)
        if moment > local:
            return moment
    raise ValueError("No allowed day.")  # unreachable for a non-empty day set


def is_due(routine, now: datetime, tz: ZoneInfo, catchup_hours: float = 6) -> bool:
    local = _aware(now).astimezone(tz)
    if not routine.enabled or local.date().weekday() not in parse_days(routine.days):
        return False
    moment = _scheduled_moment(routine, local.date(), tz)
    if local < moment or local > moment + timedelta(hours=catchup_hours):
        return False
    last = _aware(routine.last_run_at)
    if last is not None and last >= moment:
        return False
    created = _aware(routine.created_at)
    # A routine made after today's moment waits for the next one.
    return not (created is not None and created >= moment)


def routine_public(row: RoutineRecord, tz: ZoneInfo | None = None) -> dict:
    tz = tz or _tz()
    return {
        "id": row.id,
        "name": row.name,
        "prompt": row.prompt,
        "agent_id": row.agent_id,
        "time_of_day": row.time_of_day,
        "days": [] if not row.days else [int(p) for p in row.days.split(",")],
        "enabled": row.enabled,
        "created_at": _aware(row.created_at).isoformat(),
        "updated_at": _aware(row.updated_at).isoformat(),
        "last_run_at": None if row.last_run_at is None else _aware(row.last_run_at).isoformat(),
        "last_status": row.last_status,
        "last_error": row.last_error,
        "last_conversation_id": row.last_conversation_id,
        "next_run_at": next_due(row, _now(), tz).isoformat(),
    }


def _tz() -> ZoneInfo:
    from app.core.config import get_settings

    try:
        return ZoneInfo(get_settings().routine_timezone)
    except Exception:
        return ZoneInfo("Africa/Cairo")


def create_routine(session: Session, *, name: str, prompt: str, agent_id: str | None, time_of_day: str,
                   days: list[int], enabled: bool = True) -> RoutineRecord:
    now = _now()
    row = RoutineRecord(id=str(uuid4()), name=name, prompt=prompt, agent_id=agent_id, time_of_day=time_of_day,
                        days=normalize_days(days), enabled=enabled, created_at=now, updated_at=now)
    session.add(row)
    session.commit()
    return row


async def run_routine(routine_id: str) -> dict | None:
    """Run one routine now in its own DB session and record the outcome."""
    from app.agents.execution import AgentExecutionService
    from app.agents.repository import AgentRepository
    from app.agents.service import AgentService
    from app.conversations.repository import ConversationRepository
    from app.conversations.schemas import ConversationUpdate
    from app.conversations.service import ConversationService
    from app.core.controls import AssistantBlockedError
    from app.core.database import get_session_factory
    from app.files.grants import AgentWorkspaceGrantRepository
    from app.memories.action_repository import PendingMemoryActionRepository
    from app.memories.action_service import MemoryActionService
    from app.memories.repository import MemoryRepository
    from app.memories.service import MemoryService
    from app.providers.ollama import OllamaProvider
    from app.routing.service import RoutingService
    from app.tools.grants import AgentToolGrantRepository
    from app.tools.registry import get_tool_registry
    from app.tools.service import ToolService

    session = get_session_factory()()
    try:
        row = session.get(RoutineRecord, routine_id)
        if row is None:
            return None
        prompt, agent_id, name = row.prompt, row.agent_id, row.name
        status, error, conversation_id = "ok", None, None
        try:
            agents = AgentService(AgentRepository(session))
            tools = ToolService(get_tool_registry(), AgentToolGrantRepository(session), agents,
                                workspace_grants=AgentWorkspaceGrantRepository(session))
            memories = MemoryService(MemoryRepository(session))
            service = ConversationService(
                ConversationRepository(session), agents, AgentExecutionService(agents, OllamaProvider(), tools),
                memories, MemoryActionService(PendingMemoryActionRepository(session), memories),
                RoutingService(agents, OllamaProvider()),
            )
            result = await service.send(message=prompt, system_prompt=None, agent_id=agent_id, conversation_id=None)
            conversation_id = result.conversation_id
            service.update_conversation(conversation_id, ConversationUpdate(title=f"⏰ {name}"[:200]))
        except AssistantBlockedError as exc:
            status, error = "refused", str(exc)[:200]
        except Exception as exc:  # noqa: BLE001 - one routine must not stop the loop
            logger.exception("Routine %s failed", routine_id)
            session.rollback()
            status, error = "error", (str(exc) or type(exc).__name__)[:200]
        session.rollback()
        row = session.get(RoutineRecord, routine_id)
        if row is None:  # deleted while running
            return None
        row.last_run_at = _now()
        row.last_status = status
        row.last_error = error
        row.last_conversation_id = conversation_id
        session.commit()
        return routine_public(row)
    finally:
        session.close()


async def run_due(now: datetime | None = None) -> list[dict]:
    from app.core.config import get_settings
    from app.core.database import get_session_factory

    now = now or _now()
    tz, catchup = _tz(), get_settings().routine_catchup_hours
    session = get_session_factory()()
    try:
        due = [r.id for r in session.scalars(select(RoutineRecord).where(RoutineRecord.enabled.is_(True))).all()
               if is_due(r, now, tz, catchup)]
    finally:
        session.close()
    results = []
    for routine_id in due:  # one at a time
        outcome = await run_routine(routine_id)
        if outcome is not None:
            results.append(outcome)
    return results


async def scheduler_loop() -> None:
    while True:
        try:
            await asyncio.sleep(TICK_SECONDS)
            await run_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Routine scheduler tick failed")
