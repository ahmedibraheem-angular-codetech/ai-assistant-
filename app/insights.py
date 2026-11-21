"""Proactive insights: things that need the user, derived from local data.

No model calls. Each rule is isolated: one failing rule never hides the others.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.models import AgentRecord
from app.approvals.models import ApprovalRequestRecord
from app.core import controls
from app.core.config import get_settings
from app.learning.lessons import AgentLessonRecord
from app.learning.skills import AgentSkillRecord
from app.routines import RoutineRecord

log = logging.getLogger(__name__)

APPROVAL_WAIT = timedelta(hours=1)
PAUSE_WAIT = timedelta(hours=2)
BUDGET_RATIO = 0.8
OLLAMA_TTL = 60.0
_ollama_cache: tuple[float, bool] | None = None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _insight(id_: str, kind: str, severity: str, title: str, detail: str, link: str) -> dict:
    return {"id": id_, "kind": kind, "severity": severity, "title": title, "detail": detail, "link": link}


def _age(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 1)
    if minutes < 120:
        return f"{minutes} دقيقة"
    return f"{minutes // 60} ساعة"


def _approvals(session: Session, now: datetime) -> list[dict]:
    cutoff = now - APPROVAL_WAIT
    rows = [_aware(value) for value in session.scalars(
        select(ApprovalRequestRecord.created_at).where(ApprovalRequestRecord.status == "PENDING")
    ).all()]
    old = [value for value in rows if value <= cutoff]
    if not old:
        return []
    oldest = now - min(old)
    return [_insight(
        "approvals-waiting", "approvals", "warn", "طلبات موافقة تنتظرك",
        f"{len(old)} طلب بانتظار قرارك، أقدمها منذ {_age(oldest)}.", "/approvals",
    )]


def _routines(session: Session, now: datetime) -> list[dict]:
    rows = session.scalars(select(RoutineRecord).where(RoutineRecord.last_status.in_(("error", "refused")))).all()
    result = []
    for row in rows:
        failed = row.last_status == "error"
        result.append(_insight(
            f"routine-{row.id}", "routine", "warn" if failed else "info",
            f"الروتين «{row.name}» " + ("فشل" if failed else "رُفض"),
            (row.last_error or "آخر تشغيل لم ينجح.")[:120], "/routines",
        ))
    return result


def _budgets(session: Session, now: datetime) -> list[dict]:
    result = []
    for agent in session.scalars(select(AgentRecord)).all():
        usage = controls.usage_for(agent.id)
        for used_key, limit_key, label in (("tool_calls", "daily_tool_calls", "أدوات"),
                                           ("chat_turns", "daily_chat_turns", "محادثات")):
            limit = usage[limit_key]
            if limit and usage[used_key] >= limit * BUDGET_RATIO:
                reached = usage[used_key] >= limit
                result.append(_insight(
                    f"budget-{agent.id}-{used_key}", "budget", "high" if reached else "warn",
                    f"ميزانية {agent.name} اليومية ({label})",
                    f"استُهلك {usage[used_key]} من {limit} اليوم.", "/team",
                ))
    return result


def _review(session: Session, now: datetime) -> list[dict]:
    result = []
    lessons = session.scalar(select(func.count()).select_from(AgentLessonRecord)
                             .where(AgentLessonRecord.status == "pending")) or 0
    skills = session.scalar(select(func.count()).select_from(AgentSkillRecord)
                            .where(AgentSkillRecord.status.in_(("pending", "candidate")))) or 0
    if lessons:
        result.append(_insight("lessons-review", "review", "info", "دروس تنتظر مراجعتك",
                               f"{lessons} درس بانتظار القبول أو الرفض.", "/team"))
    if skills:
        result.append(_insight("skills-review", "review", "info", "مهارات تنتظر مراجعتك",
                               f"{skills} مهارة بانتظار القبول أو الرفض.", "/team"))
    return result


def _paused(session: Session, now: datetime) -> list[dict]:
    state = controls.pause_state()
    if not state.get("paused") or not state.get("since"):
        return []
    elapsed = now - _aware(datetime.fromisoformat(state["since"]))
    if elapsed < PAUSE_WAIT:
        return []
    return [_insight("assistant-paused", "paused", "warn", "المساعد متوقف مؤقتًا",
                     f"متوقف منذ {_age(elapsed)}. استأنفه عندما تكون جاهزًا.", "/settings")]


def _ollama_up() -> bool:
    url = get_settings().ollama_base_url.rstrip("/") + "/api/version"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - local configured URL
            return response.status == 200
    except Exception:  # noqa: BLE001 - any failure means unreachable
        return False


def _ollama(session: Session, now: datetime) -> list[dict]:
    global _ollama_cache
    stamp = time.monotonic()
    if _ollama_cache is None or stamp - _ollama_cache[0] > OLLAMA_TTL:
        _ollama_cache = (stamp, _ollama_up())
    if _ollama_cache[1]:
        return []
    return [_insight("ollama-down", "model", "high", "نموذج Ollama غير متاح",
                     "تعذّر الوصول إلى Ollama. لن يرد المساعد قبل تشغيله.", "/settings")]


CHECKS: list[Callable[[Session, datetime], list[dict]]] = [
    _approvals, _routines, _budgets, _review, _paused, _ollama,
]


def collect(session: Session, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    result: list[dict] = []
    for check in CHECKS:
        try:
            result.extend(check(session, now))
        except Exception:  # noqa: BLE001 - isolate each rule
            session.rollback()
            log.warning("insight check %s failed", check.__name__, exc_info=True)
    return result
