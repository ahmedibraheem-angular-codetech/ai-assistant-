"""Agent lessons and feedback for the local user (phase 4, slice 1)."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agents.service import AgentService
from app.api.deps import get_agent_service
from app.api.routes.control import _agent_id
from app.api.routes.permissions import require_local_admin
from app.core.database import get_db
from app.learning import lessons

router = APIRouter()


class LessonCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=lessons.MAX_LESSON_CHARS)
    topic: str = Field(default="", max_length=200)


class LessonUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "pending", "retired"]


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=lessons.MAX_LESSON_CHARS)
    topic: str = Field(default="", max_length=200)


@router.get("/agents/{agent_id}/lessons")
def list_lessons(agent_id: str, status: str | None = None, _local: None = Depends(require_local_admin),
                 agents: AgentService = Depends(get_agent_service), session: Session = Depends(get_db)):
    items = lessons.list_lessons(session, _agent_id(agents, agent_id), status)
    return {"lessons": items, "count": len(items)}


@router.post("/agents/{agent_id}/lessons", status_code=201)
def add_lesson(agent_id: str, payload: LessonCreate, _local: None = Depends(require_local_admin),
               agents: AgentService = Depends(get_agent_service), session: Session = Depends(get_db)):
    try:
        return lessons.add_lesson(session, _agent_id(agents, agent_id), payload.text, topic=payload.topic,
                                  source="user", status="active")
    except lessons.LessonError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/agents/{agent_id}/feedback", status_code=201)
def feedback(agent_id: str, payload: Feedback, _local: None = Depends(require_local_admin),
             agents: AgentService = Depends(get_agent_service), session: Session = Depends(get_db)):
    """A rating; a comment becomes an active lesson the agent recalls next time."""
    from app.permissions.audit import add_audit

    canonical = _agent_id(agents, agent_id)
    add_audit(session, event="agent_feedback", decision=payload.rating, detail=(payload.comment or "")[:120],
              agent_id=canonical)
    lesson = None
    if payload.comment and payload.comment.strip():
        prefix = "Do this again: " if payload.rating == "up" else "Avoid this: "
        try:
            lesson = lessons.add_lesson(session, canonical, prefix + payload.comment.strip(), topic=payload.topic,
                                        source="feedback", status="active", commit=False)
        except lessons.LessonError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return {"rating": payload.rating, "lesson": lesson}


@router.patch("/lessons/{lesson_id}")
def update_lesson(lesson_id: str, payload: LessonUpdate, _local: None = Depends(require_local_admin),
                  session: Session = Depends(get_db)):
    try:
        return lessons.set_status(session, lesson_id, payload.status)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/lessons/{lesson_id}", status_code=204)
def delete_lesson(lesson_id: str, _local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    try:
        lessons.delete_lesson(session, lesson_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


# ------------------------------------------------------------- skills & evals

from app.learning import skills  # noqa: E402


class SkillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=60)
    description: str = Field(min_length=1, max_length=300)
    steps: str = Field(min_length=1, max_length=skills.MAX_STEPS_CHARS)


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["candidate", "active", "retired", "rejected"]


class EvalCaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=2_000)
    must_include: list[str] = Field(default_factory=list, max_length=10)
    must_not_include: list[str] = Field(default_factory=list, max_length=10)


def _skill_errors(fn):
    try:
        return fn()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except skills.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/agents/{agent_id}/skills")
def list_skills(agent_id: str, status: str | None = None, _local: None = Depends(require_local_admin),
                agents: AgentService = Depends(get_agent_service), session: Session = Depends(get_db)):
    items = skills.list_skills(session, _agent_id(agents, agent_id), status)
    return {"skills": items, "count": len(items)}


@router.post("/agents/{agent_id}/skills", status_code=201)
def add_skill(agent_id: str, payload: SkillCreate, _local: None = Depends(require_local_admin),
              agents: AgentService = Depends(get_agent_service), session: Session = Depends(get_db)):
    """A skill written by the user is trusted and active at once."""
    canonical = _agent_id(agents, agent_id)
    return _skill_errors(lambda: skills.save_skill(session, canonical, name=payload.name,
                                                   description=payload.description, steps=payload.steps,
                                                   source="user", status="active"))


@router.patch("/skills/{skill_id}")
def update_skill(skill_id: str, payload: SkillUpdate, _local: None = Depends(require_local_admin),
                 session: Session = Depends(get_db)):
    return _skill_errors(lambda: skills.set_skill_status(session, skill_id, payload.status))


@router.post("/skills/{skill_id}/rollback")
def rollback_skill(skill_id: str, _local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    return _skill_errors(lambda: skills.rollback(session, skill_id))


@router.post("/skills/{skill_id}/evaluate")
async def evaluate_skill(skill_id: str, _local: None = Depends(require_local_admin),
                         agents: AgentService = Depends(get_agent_service), session: Session = Depends(get_db)):
    from app.core.controls import AssistantBlockedError

    row = session.get(skills.AgentSkillRecord, skill_id)
    if row is None:
        raise HTTPException(status_code=404, detail="That skill was not found.")
    agent = agents.get(row.agent_id)
    try:
        return await skills.evaluate(session, agent, skill_id)
    except AssistantBlockedError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except skills.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/agents/{agent_id}/eval-cases")
def list_cases(agent_id: str, _local: None = Depends(require_local_admin),
               agents: AgentService = Depends(get_agent_service), session: Session = Depends(get_db)):
    items = skills.list_cases(session, _agent_id(agents, agent_id))
    return {"cases": items, "count": len(items)}


@router.post("/agents/{agent_id}/eval-cases", status_code=201)
def add_case(agent_id: str, payload: EvalCaseCreate, _local: None = Depends(require_local_admin),
             agents: AgentService = Depends(get_agent_service), session: Session = Depends(get_db)):
    canonical = _agent_id(agents, agent_id)
    return _skill_errors(lambda: skills.add_case(session, canonical, payload.prompt, payload.must_include,
                                                 payload.must_not_include))


@router.delete("/eval-cases/{case_id}", status_code=204)
def delete_case(case_id: str, _local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    _skill_errors(lambda: skills.delete_case(session, case_id))
    return Response(status_code=204)


@router.get("/agents/{agent_id}/profile")
def agent_profile(agent_id: str, _local: None = Depends(require_local_admin),
                  agents: AgentService = Depends(get_agent_service), session: Session = Depends(get_db)):
    """The agent's "CV": active skills, lessons, recent evaluations, and today's usage."""
    from app.core.controls import usage_for

    canonical = _agent_id(agents, agent_id)
    agent = agents.get(canonical)
    all_skills = skills.list_skills(session, canonical)
    return {
        "agent": {"id": agent.id, "name": agent.name, "role": agent.role},
        "skills": [s for s in all_skills if s["status"] == "active"],
        "skills_waiting": [s for s in all_skills if s["status"] in {"candidate", "pending"}],
        "lessons_active": lessons.list_lessons(session, canonical, "active"),
        "lessons_pending": lessons.list_lessons(session, canonical, "pending"),
        "eval_cases": len(skills.list_cases(session, canonical)),
        "recent_evaluations": skills.eval_runs(session, canonical)[:10],
        "usage_today": usage_for(canonical),
    }
