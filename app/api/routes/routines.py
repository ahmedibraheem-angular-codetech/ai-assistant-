"""Scheduled routines for the local user (phase 7, slice 1). Admin only; agents have no access."""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import routines
from app.api.routes.permissions import require_local_admin
from app.core.database import get_db

router = APIRouter()


def _check_time(value: str) -> str:
    if not routines.TIME_RE.match(value):
        raise ValueError("Time must look like HH:MM (00:00 to 23:59).")
    return value


def _check_days(value: list[int]) -> list[int]:
    if any(d < 0 or d > 6 for d in value):
        raise ValueError("Days are numbers 0 (Monday) to 6 (Sunday).")
    return sorted(set(value))


class RoutineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=4000)
    agent_id: str | None = Field(default=None, max_length=36)
    time_of_day: str
    days: list[int] = Field(default_factory=list, max_length=7)
    enabled: bool = True

    _time = field_validator("time_of_day")(_check_time)
    _days = field_validator("days")(_check_days)


class RoutineUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    agent_id: str | None = Field(default=None, max_length=36)
    time_of_day: str | None = None
    days: list[int] | None = Field(default=None, max_length=7)
    enabled: bool | None = None

    @field_validator("time_of_day")
    @classmethod
    def _time(cls, value):
        return value if value is None else _check_time(value)

    @field_validator("days")
    @classmethod
    def _days(cls, value):
        return value if value is None else _check_days(value)


def _get(session: Session, routine_id: str) -> routines.RoutineRecord:
    row = session.get(routines.RoutineRecord, routine_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Routine not found.")
    return row


@router.get("/routines", dependencies=[Depends(require_local_admin)])
def list_routines(session: Session = Depends(get_db)) -> dict:
    rows = session.scalars(select(routines.RoutineRecord).order_by(routines.RoutineRecord.time_of_day,
                                                                   routines.RoutineRecord.created_at)).all()
    return {"routines": [routines.routine_public(r) for r in rows]}


@router.post("/routines", status_code=201, dependencies=[Depends(require_local_admin)])
def create_routine(payload: RoutineCreate, session: Session = Depends(get_db)) -> dict:
    row = routines.create_routine(session, name=" ".join(payload.name.split()), prompt=payload.prompt.strip(),
                                  agent_id=payload.agent_id, time_of_day=payload.time_of_day, days=payload.days,
                                  enabled=payload.enabled)
    return routines.routine_public(row)


@router.patch("/routines/{routine_id}", dependencies=[Depends(require_local_admin)])
def update_routine(routine_id: str, payload: RoutineUpdate, session: Session = Depends(get_db)) -> dict:
    row = _get(session, routine_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one field must be provided.")
    for key in ("name", "prompt", "time_of_day", "days", "enabled"):
        if key in changes and changes[key] is None:
            raise HTTPException(status_code=422, detail=f"{key} cannot be null.")
    if "name" in changes:
        row.name = " ".join(changes["name"].split())
    if "prompt" in changes:
        row.prompt = changes["prompt"].strip()
    if "agent_id" in changes:
        row.agent_id = changes["agent_id"]
    if "time_of_day" in changes:
        row.time_of_day = changes["time_of_day"]
    if "days" in changes:
        row.days = routines.normalize_days(changes["days"])
    if "enabled" in changes:
        row.enabled = changes["enabled"]
    row.updated_at = routines._now()
    session.commit()
    return routines.routine_public(row)


@router.delete("/routines/{routine_id}", status_code=204, dependencies=[Depends(require_local_admin)])
def delete_routine(routine_id: str, session: Session = Depends(get_db)) -> Response:
    session.delete(_get(session, routine_id))
    session.commit()
    return Response(status_code=204)


@router.post("/routines/{routine_id}/run", dependencies=[Depends(require_local_admin)])
async def run_routine_now(routine_id: str, session: Session = Depends(get_db)) -> dict:
    _get(session, routine_id)
    session.close()
    result = await routines.run_routine(routine_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Routine not found.")
    return result
