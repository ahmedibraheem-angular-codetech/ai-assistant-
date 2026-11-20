"""Project memory for the local user (phase 3, slice 1)."""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.routes.permissions import require_local_admin
from app.core.database import get_db
from app.projects import core

router = APIRouter()


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4_000)


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=4_000)
    archived: bool | None = None


class EntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern="^(decision|task|note|status)$")
    text: str = Field(min_length=1, max_length=core.MAX_ENTRY_CHARS)


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    done: bool


def _errors(fn):
    try:
        return fn()
    except core.ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except core.ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/projects")
def list_projects(
    include_archived: bool = False,
    _local: None = Depends(require_local_admin),
    session: Session = Depends(get_db),
):
    items = core.list_projects(session, include_archived)
    return {"projects": items, "count": len(items)}


@router.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, _local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    return _errors(lambda: core.create_project(session, payload.name, payload.description))


@router.patch("/projects/{project}")
def update_project(project: str, payload: ProjectUpdate, _local: None = Depends(require_local_admin),
                   session: Session = Depends(get_db)):
    return _errors(lambda: core.update_project(session, project, description=payload.description,
                                               archived=payload.archived))


@router.delete("/projects/{project}", status_code=204)
def delete_project(project: str, _local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    _errors(lambda: core.delete_project(session, project))
    return Response(status_code=204)


@router.get("/projects/{project}/brief")
def project_brief(project: str, _local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    return _errors(lambda: core.brief(session, project))


@router.get("/projects/{project}/entries")
def list_entries(project: str, kind: str | None = None, _local: None = Depends(require_local_admin),
                 session: Session = Depends(get_db)):
    items = _errors(lambda: core.entries(session, project, kind))
    return {"entries": items, "count": len(items)}


@router.post("/projects/{project}/entries", status_code=201)
def add_entry(project: str, payload: EntryCreate, _local: None = Depends(require_local_admin),
              session: Session = Depends(get_db)):
    return _errors(lambda: core.add_entry(session, project, kind=payload.kind, text=payload.text))


@router.patch("/project-entries/{entry_id}")
def update_task(entry_id: str, payload: TaskUpdate, _local: None = Depends(require_local_admin),
                session: Session = Depends(get_db)):
    return _errors(lambda: core.set_task_done(session, entry_id, payload.done))


@router.delete("/project-entries/{entry_id}", status_code=204)
def delete_entry(entry_id: str, _local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    _errors(lambda: core.delete_entry(session, entry_id))
    return Response(status_code=204)
