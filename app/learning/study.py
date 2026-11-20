"""Scheduled study time (phase 4, slice 3).

Each agent can have a study plan: topics and an interval. A background loop
runs due plans. One study session:
1. Evaluates the agent's candidate skills (when it has eval cases) and
   promotes or rejects them.
2. Researches up to 3 topics with the normal `research` tool, through the
   normal permission gate. Without a standing approval for research, the
   session leaves an approval request in the inbox instead of searching.
3. Saves what it learned as lessons with status "pending": web content is
   untrusted, so nothing reaches the agent's prompt until the user approves.
4. Records a run the user reads in the study digest.

The pause switch and daily budgets apply. Nothing here can change permissions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.database import Base

logger = logging.getLogger(__name__)
TOPICS_PER_SESSION = 3
MAX_TOPICS = 10
LESSONS_PER_TOPIC = 2
TICK_SECONDS = 60


class AgentStudyPlanRecord(Base):
    __tablename__ = "agent_study_plans"

    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    topics: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list
    interval_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # rotates topics
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentStudyRunRecord(Base):
    __tablename__ = "agent_study_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # done | skipped | failed
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def plan_public(row: AgentStudyPlanRecord) -> dict:
    return {
        "agent_id": row.agent_id,
        "topics": json.loads(row.topics),
        "interval_hours": row.interval_hours,
        "enabled": row.enabled,
        "last_run_at": None if row.last_run_at is None else _aware(row.last_run_at).isoformat(),
        "next_run_at": _aware(row.next_run_at).isoformat(),
    }


def set_plan(session: Session, agent_id: str, topics: list[str], interval_hours: int, enabled: bool = True) -> dict:
    clean = [" ".join(t.split())[:200] for t in topics if t.strip()][:MAX_TOPICS]
    if not clean:
        raise ValueError("Give at least one topic.")
    row = session.get(AgentStudyPlanRecord, agent_id)
    if row is None:
        row = AgentStudyPlanRecord(agent_id=agent_id, topics="[]", interval_hours=interval_hours, enabled=enabled,
                                   cursor=0, next_run_at=_now() + timedelta(hours=interval_hours))
        session.add(row)
    row.topics = json.dumps(clean, ensure_ascii=False)
    row.interval_hours = interval_hours
    row.enabled = enabled
    row.next_run_at = (_aware(row.last_run_at) or _now()) + timedelta(hours=interval_hours)
    session.commit()
    return plan_public(row)


def get_plan(session: Session, agent_id: str) -> dict | None:
    row = session.get(AgentStudyPlanRecord, agent_id)
    return None if row is None else plan_public(row)


def delete_plan(session: Session, agent_id: str) -> bool:
    row = session.get(AgentStudyPlanRecord, agent_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def _record(session: Session, agent_id: str, started: datetime, status: str, summary: str, detail: dict) -> dict:
    row = AgentStudyRunRecord(id=str(uuid4()), agent_id=agent_id, started_at=started, status=status,
                              summary=summary[:2_000], detail=json.dumps(detail, ensure_ascii=False, default=str))
    session.add(row)
    session.commit()
    return run_public(row)


def run_public(row: AgentStudyRunRecord) -> dict:
    return {"id": row.id, "agent_id": row.agent_id, "started_at": _aware(row.started_at).isoformat(),
            "status": row.status, "summary": row.summary, "detail": json.loads(row.detail)}


def _lessons_from_report(report: dict, topic: str) -> list[str]:
    texts = []
    for claim in (report.get("claims") or [])[:LESSONS_PER_TOPIC]:
        text = claim.get("text") if isinstance(claim, dict) else None
        sources = claim.get("sources") if isinstance(claim, dict) else None
        url = sources[0].get("url") if sources and isinstance(sources[0], dict) else None
        if isinstance(text, str) and text.strip():
            texts.append(f"[{topic}] {text.strip()}" + (f" (source: {url})" if url else ""))
    if not texts:
        for finding in (report.get("findings") or [])[:LESSONS_PER_TOPIC]:
            if isinstance(finding, str) and finding.strip():
                texts.append(f"[{topic}] {finding.strip()}")
    return [t[:590] for t in texts]


async def study_once(agent_id: str) -> dict:
    """Run one study session for one agent now. Uses its own DB session."""
    from app.agents.repository import AgentRepository
    from app.agents.service import AgentService
    from app.core.controls import AssistantBlockedError, is_paused
    from app.core.database import get_session_factory
    from app.files.grants import AgentWorkspaceGrantRepository
    from app.learning import lessons, skills
    from app.tools.grants import AgentToolGrantRepository
    from app.tools.registry import get_tool_registry
    from app.tools.service import ToolService

    started = _now()
    session = get_session_factory()()
    plan = None
    try:
        plan = session.get(AgentStudyPlanRecord, agent_id)
        agents = AgentService(AgentRepository(session))
        agent = agents.get(agent_id)
        detail: dict = {"evaluations": [], "topics": [], "lessons_pending": 0, "approvals_waiting": 0}
        if is_paused():
            return _record(session, agent_id, started, "skipped", "The assistant is paused.", detail)
        if not agent.enabled:
            return _record(session, agent_id, started, "skipped", "The agent is turned off.", detail)

        # 1. Evaluate candidate skills.
        if skills.list_cases(session, agent_id):
            for candidate in skills.list_skills(session, agent_id, "candidate"):
                try:
                    result = await skills.evaluate(session, agent, candidate["id"])
                    detail["evaluations"].append({"skill": candidate["name"], "version": candidate["version"],
                                                  "decision": result["decision"],
                                                  "score": f"{result['baseline']} -> {result['candidate']}"})
                except AssistantBlockedError as exc:
                    detail["evaluations"].append({"skill": candidate["name"], "decision": "stopped", "reason": str(exc)})
                    break
                except Exception as exc:  # one bad skill must not stop the session
                    detail["evaluations"].append({"skill": candidate["name"], "decision": "error", "reason": str(exc)[:200]})

        # 2-3. Research topics through the normal permission gate.
        topics = json.loads(plan.topics) if plan is not None else []
        if topics:
            start = plan.cursor % len(topics)
            todays = [topics[(start + i) % len(topics)] for i in range(min(TOPICS_PER_SESSION, len(topics)))]
            plan.cursor = (start + len(todays)) % len(topics)
            session.commit()
            tools = ToolService(get_tool_registry(), AgentToolGrantRepository(session), agents,
                                workspace_grants=AgentWorkspaceGrantRepository(session))
            for topic in todays:
                entry = {"topic": topic}
                try:
                    outcome = await tools.test("research", {"task": f"Latest useful knowledge about: {topic}"}, agent_id)
                except Exception as exc:
                    entry["result"] = f"not available: {str(exc)[:160]}"
                    detail["topics"].append(entry)
                    continue
                if outcome.status == "confirmation_required":
                    entry["result"] = "waiting for your approval (no standing approval for research)"
                    detail["approvals_waiting"] += 1
                elif outcome.status == "ok" and isinstance(outcome.output, dict):
                    saved = 0
                    for text in _lessons_from_report(outcome.output, topic):
                        try:
                            lessons.add_lesson(session, agent_id, text, topic=topic, source="self", status="pending")
                            saved += 1
                        except lessons.LessonError:
                            break
                    entry["result"] = f"{saved} lesson(s) waiting for your review"
                    detail["lessons_pending"] += saved
                else:
                    entry["result"] = f"{outcome.status}: {(outcome.error or '')[:160]}"
                detail["topics"].append(entry)
        parts = []
        if detail["evaluations"]:
            parts.append(f"{len(detail['evaluations'])} skill evaluation(s)")
        if detail["lessons_pending"]:
            parts.append(f"{detail['lessons_pending']} new lesson(s) to review")
        if detail["approvals_waiting"]:
            parts.append(f"{detail['approvals_waiting']} research approval(s) waiting")
        summary = f"{agent.name}: " + (", ".join(parts) if parts else "nothing new this time")
        return _record(session, agent_id, started, "done", summary, detail)
    except Exception as exc:
        logger.exception("Study session failed for %s", agent_id)
        session.rollback()
        return _record(session, agent_id, started, "failed", f"The study session failed: {type(exc).__name__}", {})
    finally:
        session.close()


async def run_due(now: datetime | None = None) -> list[dict]:
    """Run every enabled plan whose time has come; reschedule them."""
    from app.core.database import get_session_factory

    now = now or _now()
    session = get_session_factory()()
    try:
        due = [row.agent_id for row in session.scalars(select(AgentStudyPlanRecord).where(
            AgentStudyPlanRecord.enabled.is_(True))).all() if _aware(row.next_run_at) <= now]
    finally:
        session.close()
    results = []
    for agent_id in due:
        results.append(await study_once(agent_id))
        session = get_session_factory()()
        try:
            row = session.get(AgentStudyPlanRecord, agent_id)
            if row is not None:
                row.last_run_at = now
                row.next_run_at = now + timedelta(hours=row.interval_hours)
                session.commit()
        finally:
            session.close()
    return results


def digest(session: Session, since: datetime) -> dict:
    rows = session.scalars(select(AgentStudyRunRecord).where(AgentStudyRunRecord.started_at >= since)
                           .order_by(AgentStudyRunRecord.started_at.desc()).limit(100)).all()
    runs = [run_public(r) for r in rows]
    return {
        "since": since.isoformat(),
        "runs": runs,
        "lessons_to_review": sum(r["detail"].get("lessons_pending", 0) for r in runs),
        "approvals_waiting": sum(r["detail"].get("approvals_waiting", 0) for r in runs),
        "skills_promoted": sum(1 for r in runs for e in r["detail"].get("evaluations", []) if e.get("decision") == "promoted"),
        "skills_rejected": sum(1 for r in runs for e in r["detail"].get("evaluations", []) if e.get("decision") == "rejected"),
    }


async def scheduler_loop() -> None:
    """Background loop started by the app lifespan."""
    while True:
        try:
            await asyncio.sleep(TICK_SECONDS)
            await run_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Study scheduler tick failed")
