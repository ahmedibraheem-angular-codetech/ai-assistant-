"""n8n workflow registry for the local user (phase 7, slice 2). Admin only; agents have no access."""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import n8n
from app.api.routes.permissions import require_local_admin
from app.core.database import get_db

router = APIRouter()


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=300)
    webhook_path: str = Field(min_length=1, max_length=200)
    method: str = "POST"
    enabled: bool = True

    _name = field_validator("name")(n8n.check_name)
    _path = field_validator("webhook_path")(n8n.check_webhook_path)
    _method = field_validator("method")(n8n.check_method)


class WorkflowUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=300)
    webhook_path: str | None = Field(default=None, min_length=1, max_length=200)
    method: str | None = None
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value):
        return value if value is None else n8n.check_name(value)

    @field_validator("webhook_path")
    @classmethod
    def _path(cls, value):
        return value if value is None else n8n.check_webhook_path(value)

    @field_validator("method")
    @classmethod
    def _method(cls, value):
        return value if value is None else n8n.check_method(value)


def _get(session: Session, workflow_id: str) -> n8n.N8nWorkflowRecord:
    row = session.get(n8n.N8nWorkflowRecord, workflow_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return row


@router.get("/n8n/workflows", dependencies=[Depends(require_local_admin)])
def list_workflows(session: Session = Depends(get_db)) -> dict:
    rows = session.scalars(select(n8n.N8nWorkflowRecord).order_by(n8n.N8nWorkflowRecord.name)).all()
    return {"workflows": [n8n.workflow_public(r) for r in rows]}


@router.post("/n8n/workflows", status_code=201, dependencies=[Depends(require_local_admin)])
def create_workflow(payload: WorkflowCreate, session: Session = Depends(get_db)) -> dict:
    if n8n.find_by_name(session, payload.name) is not None:
        raise HTTPException(status_code=409, detail="A workflow with that name already exists.")
    try:
        row = n8n.create_workflow(session, name=payload.name, description=payload.description.strip(),
                                  webhook_path=payload.webhook_path, method=payload.method,
                                  enabled=payload.enabled)
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="A workflow with that name already exists.")
    return n8n.workflow_public(row)


@router.patch("/n8n/workflows/{workflow_id}", dependencies=[Depends(require_local_admin)])
def update_workflow(workflow_id: str, payload: WorkflowUpdate, session: Session = Depends(get_db)) -> dict:
    row = _get(session, workflow_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one field must be provided.")
    for key in ("name", "description", "webhook_path", "method", "enabled"):
        if key in changes and changes[key] is None:
            raise HTTPException(status_code=422, detail=f"{key} cannot be null.")
    if "name" in changes and changes["name"] != row.name:
        if n8n.find_by_name(session, changes["name"]) is not None:
            raise HTTPException(status_code=409, detail="A workflow with that name already exists.")
    for key, value in changes.items():
        setattr(row, key, value.strip() if key == "description" else value)
    row.updated_at = n8n._now()
    session.commit()
    return n8n.workflow_public(row)


@router.delete("/n8n/workflows/{workflow_id}", status_code=204, dependencies=[Depends(require_local_admin)])
def delete_workflow(workflow_id: str, session: Session = Depends(get_db)) -> Response:
    session.delete(_get(session, workflow_id))
    session.commit()
    return Response(status_code=204)


@router.get("/n8n/status", dependencies=[Depends(require_local_admin)])
async def n8n_status() -> dict:
    return await n8n.ping_status()
