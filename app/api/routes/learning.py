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
