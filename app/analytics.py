"""Read-only analytics for the Mac app's dashboard (phase 5).

One call gathers everything the home page shows: headline numbers, the team
and the systems around the assistant, the latest actions, a time series, the
most used tools, and learning and memory counts. It only reads.

Limits, stated so the numbers are not over-read:
- Tool numbers come from approval requests and the permission audit. Tool
  calls that run without an approval are not logged anywhere yet, so they are
  not counted.
- Budget usage is the in-memory counter of today (it resets on restart).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.agents.models import AgentRecord
from app.agents.team import AgentTeamMemberRecord
from app.approvals.models import ApprovalRequestRecord
from app.conversations.models import ConversationRecord, MessageRecord
from app.core import controls
from app.graph.core import GraphEntityRecord, GraphRelationRecord
from app.learning.lessons import AgentLessonRecord
from app.learning.skills import AgentSkillRecord
from app.learning.study import AgentStudyRunRecord
from app.memories.models import MemoryRecord
from app.permissions.models import PermissionAuditRecord
from app.projects.core import ProjectEntryRecord, ProjectRecord

RANGES = {"today": (timedelta(hours=24), timedelta(hours=1), 24), "7d": (timedelta(days=7), timedelta(days=1), 7),
          "30d": (timedelta(days=30), timedelta(days=1), 30)}
QUEUE_SIZE = 8
TOP_TOOLS = 6
DONE = ("COMPLETED",)
FAILED = ("FAILED",)
TOOL_EVENTS = ("approval_required", "denied")


def _naive(moment: datetime) -> datetime:
    """SQLite stores UTC without an offset; compare like with like."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).isoformat()


def _count(session: Session, statement) -> int:
    return int(session.scalar(statement) or 0)


def summary(session: Session, range_name: str = "7d", now: datetime | None = None) -> dict:
    if range_name not in RANGES:
        raise ValueError(f"Unknown range. Use one of: {', '.join(RANGES)}.")
    span, step, buckets = RANGES[range_name]
    end = _naive(now or datetime.now(timezone.utc))
    since = end - span
    before = since - span

    def turns(start, stop):
        return _count(session, select(func.count()).select_from(MessageRecord)
                      .where(MessageRecord.role == "assistant", MessageRecord.created_at >= start,
                             MessageRecord.created_at < stop))

    def conversations(start, stop):
        return _count(session, select(func.count()).select_from(ConversationRecord)
                      .where(ConversationRecord.created_at >= start, ConversationRecord.created_at < stop))

    finished = session.execute(
        select(ApprovalRequestRecord.tool_name, ApprovalRequestRecord.status, ApprovalRequestRecord.agent_id,
               ApprovalRequestRecord.created_at)
        .where(ApprovalRequestRecord.created_at >= since)
    ).all()
    completed = sum(1 for row in finished if row.status in DONE)
    failed = sum(1 for row in finished if row.status in FAILED)

    pending_rows = session.execute(
        select(ApprovalRequestRecord.created_at).where(ApprovalRequestRecord.status == "PENDING")
        .order_by(ApprovalRequestRecord.created_at)
    ).all()

    lessons_new = session.execute(
        select(AgentLessonRecord.status).where(AgentLessonRecord.created_at >= since)
    ).scalars().all()

    agents = _agents(session, since)
    budget = _budget(agents)

    return {
        "range": range_name,
        "since": _iso(since),
        "generated_at": _iso(end),
        "paused": controls.is_paused(),
        "kpis": {
            "chat_turns": {"value": turns(since, end), "previous": turns(before, since)},
            "conversations": {"value": conversations(since, end), "previous": conversations(before, since)},
            "tool_actions": {
                "value": completed + failed, "completed": completed, "failed": failed,
                "success_rate": round(completed / (completed + failed), 3) if completed + failed else None,
            },
            "approvals_pending": {"value": len(pending_rows),
                                  "oldest_at": _iso(pending_rows[0].created_at) if pending_rows else None},
            "lessons_new": {"value": len(lessons_new), "pending": sum(1 for s in lessons_new if s == "pending")},
            "budget": budget,
        },
        "agents": agents,
        "systems": _systems(session, since),
        "queue": _queue(session, {a["id"]: a["name"] for a in agents}),
        "trend": _trend(session, finished, since, step, buckets),
        "tools": _tools(session, finished, since),
        "learning": _learning(session, since),
    }


def _agents(session: Session, since: datetime) -> list[dict]:
    rows = session.scalars(select(AgentRecord).order_by(AgentRecord.created_at)).all()
    members: dict[str, list[str]] = {}
    for link in session.scalars(select(AgentTeamMemberRecord)).all():
        members.setdefault(link.manager_id, []).append(link.member_id)

    def per_agent(column, statement):
        return dict(session.execute(statement.group_by(column)).all())

    turns = per_agent(MessageRecord.agent_id, select(MessageRecord.agent_id, func.count())
                      .where(MessageRecord.role == "assistant", MessageRecord.created_at >= since))
    last = per_agent(MessageRecord.agent_id, select(MessageRecord.agent_id, func.max(MessageRecord.created_at)))
    actions = per_agent(ApprovalRequestRecord.agent_id, select(ApprovalRequestRecord.agent_id, func.count())
                        .where(ApprovalRequestRecord.created_at >= since))
    pending_lessons = per_agent(AgentLessonRecord.agent_id, select(AgentLessonRecord.agent_id, func.count())
                                .where(AgentLessonRecord.status == "pending"))
    active_skills = per_agent(AgentSkillRecord.agent_id, select(AgentSkillRecord.agent_id, func.count())
                              .where(AgentSkillRecord.status == "active"))
    studied = per_agent(AgentStudyRunRecord.agent_id, select(AgentStudyRunRecord.agent_id, func.count())
                        .where(AgentStudyRunRecord.started_at >= since))
    result = []
    for row in rows:
        usage = controls.usage_for(row.id)
        result.append({
            "id": row.id, "name": row.name, "role": row.role, "enabled": bool(row.enabled),
            "members": members.get(row.id, []),
            "chat_turns": int(turns.get(row.id, 0)), "tool_actions": int(actions.get(row.id, 0)),
            "lessons_pending": int(pending_lessons.get(row.id, 0)), "skills_active": int(active_skills.get(row.id, 0)),
            "study_runs": int(studied.get(row.id, 0)),
            "last_active_at": _iso(last.get(row.id)),
            "usage_today": {key: usage[key] for key in ("tool_calls", "chat_turns", "daily_tool_calls", "daily_chat_turns")},
        })
    return result


def _budget(agents: list[dict]) -> dict:
    """Today's use against limits, summed over agents that have a limit."""
    total = {"tool_calls_used": 0, "tool_calls_limit": 0, "chat_turns_used": 0, "chat_turns_limit": 0}
    for agent in agents:
        usage = agent["usage_today"]
        if usage["daily_tool_calls"]:
            total["tool_calls_used"] += usage["tool_calls"]
            total["tool_calls_limit"] += usage["daily_tool_calls"]
        if usage["daily_chat_turns"]:
            total["chat_turns_used"] += usage["chat_turns"]
            total["chat_turns_limit"] += usage["daily_chat_turns"]
    return total


def _table_count(session: Session, table: str) -> int:
    """Knowledge tables are created with raw SQL; a fresh database may not have them."""
    try:
        return _count(session, text(f"SELECT COUNT(*) FROM {table}"))  # table names are constants here
    except Exception:  # noqa: BLE001 - missing table means zero, not an error page
        session.rollback()
        return 0


def _systems(session: Session, since: datetime) -> dict:
    return {
        "memories": {"count": _count(session, select(func.count()).select_from(MemoryRecord)),
                     "added": _count(session, select(func.count()).select_from(MemoryRecord)
                                     .where(MemoryRecord.created_at >= since))},
        "knowledge": {"documents": _table_count(session, "knowledge_documents"),
                      "chunks": _table_count(session, "knowledge_chunks")},
        "projects": {"count": _count(session, select(func.count()).select_from(ProjectRecord)),
                     "entries": _count(session, select(func.count()).select_from(ProjectEntryRecord))},
        "graph": {"entities": _count(session, select(func.count()).select_from(GraphEntityRecord)),
                  "relations": _count(session, select(func.count()).select_from(GraphRelationRecord))},
        "tools": {"denied": _count(session, select(func.count()).select_from(PermissionAuditRecord)
                                   .where(PermissionAuditRecord.event == "denied",
                                          PermissionAuditRecord.created_at >= since))},
    }


def _queue(session: Session, names: dict[str, str]) -> list[dict]:
    rows = session.scalars(select(ApprovalRequestRecord).order_by(ApprovalRequestRecord.created_at.desc())
                           .limit(QUEUE_SIZE)).all()
    return [{
        "id": row.id, "tool": row.tool_name, "agent_id": row.agent_id,
        "agent_name": names.get(row.agent_id or "", None), "description": row.description,
        "risk": row.risk, "status": row.status, "created_at": _iso(row.created_at),
    } for row in rows]


def _trend(session: Session, finished, since: datetime, step: timedelta, buckets: int) -> dict:
    def index(moment: datetime) -> int | None:
        position = int((_naive(moment) - since) / step)
        return position if 0 <= position < buckets else None

    turns = [0] * buckets
    done = [0] * buckets
    failed = [0] * buckets
    for (moment,) in session.execute(select(MessageRecord.created_at)
                                     .where(MessageRecord.role == "assistant", MessageRecord.created_at >= since)):
        slot = index(moment)
        if slot is not None:
            turns[slot] += 1
    for row in finished:
        slot = index(row.created_at)
        if slot is None:
            continue
        if row.status in DONE:
            done[slot] += 1
        elif row.status in FAILED:
            failed[slot] += 1
    return {
        "step": "hour" if step == timedelta(hours=1) else "day",
        "starts": [_iso(since + step * i) for i in range(buckets)],
        "chat_turns": turns, "tool_completed": done, "tool_failed": failed,
    }


def _tools(session: Session, finished, since: datetime) -> list[dict]:
    uses: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    for row in finished:
        uses[row.tool_name] += 1
        if row.status in FAILED:
            failures[row.tool_name] += 1
    denied = session.execute(
        select(PermissionAuditRecord.tool_name, func.count())
        .where(PermissionAuditRecord.event == "denied", PermissionAuditRecord.created_at >= since,
               PermissionAuditRecord.tool_name.is_not(None))
        .group_by(PermissionAuditRecord.tool_name)
    ).all()
    denials = {name: int(count) for name, count in denied}
    for name in denials:
        uses.setdefault(name, 0)
    ranked = sorted(uses, key=lambda name: (-(uses[name] + denials.get(name, 0)), name))[:TOP_TOOLS]
    return [{"tool": name, "actions": uses[name], "failed": failures[name], "denied": denials.get(name, 0)}
            for name in ranked]


def _learning(session: Session, since: datetime) -> dict:
    def by_status(model):
        return {status: int(count) for status, count in
                session.execute(select(model.status, func.count()).group_by(model.status)).all()}

    lessons = by_status(AgentLessonRecord)
    skills = by_status(AgentSkillRecord)
    return {
        "lessons_active": lessons.get("active", 0), "lessons_pending": lessons.get("pending", 0),
        "skills_active": skills.get("active", 0), "skills_candidate": skills.get("candidate", 0) + skills.get("pending", 0),
        "study_runs": _count(session, select(func.count()).select_from(AgentStudyRunRecord)
                             .where(AgentStudyRunRecord.started_at >= since)),
        "memories_added": _count(session, select(func.count()).select_from(MemoryRecord)
                                 .where(MemoryRecord.created_at >= since)),
    }
