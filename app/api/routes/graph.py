"""Knowledge graph for the local user (phase 3, slice 4)."""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.routes.permissions import require_local_admin
from app.core.database import get_db
from app.graph import core

router = APIRouter()


class EntityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern="^(person|project|organization|place|topic|other)$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)


class RelationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=120)
    relation: str = Field(min_length=1, max_length=60)
    object: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=1_000)
    subject_type: str | None = None
    object_type: str | None = None


def _errors(fn):
    try:
        return fn()
    except core.GraphNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except core.GraphError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/graph/entities")
def list_entities(type: str | None = None, _local: None = Depends(require_local_admin),
                  session: Session = Depends(get_db)):
    items = core.list_entities(session, type)
    return {"entities": items, "count": len(items)}


@router.post("/graph/entities", status_code=201)
def add_entity(payload: EntityCreate, _local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    return _errors(lambda: core.upsert_entity(session, entity_type=payload.type, name=payload.name,
                                              description=payload.description))


@router.delete("/graph/entities/{entity_id}", status_code=204)
def delete_entity(entity_id: str, _local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    _errors(lambda: core.delete_entity(session, entity_id))
    return Response(status_code=204)


@router.post("/graph/relations", status_code=201)
def add_relation(payload: RelationCreate, _local: None = Depends(require_local_admin),
                 session: Session = Depends(get_db)):
    return _errors(lambda: core.add_relation(
        session, subject=payload.subject, predicate=payload.relation, obj=payload.object, note=payload.note,
        subject_type=payload.subject_type, object_type=payload.object_type,
    ))


@router.delete("/graph/relations/{relation_id}", status_code=204)
def delete_relation(relation_id: str, _local: None = Depends(require_local_admin),
                    session: Session = Depends(get_db)):
    _errors(lambda: core.delete_relation(session, relation_id))
    return Response(status_code=204)


@router.get("/graph/lookup")
def lookup(name: str, type: str | None = None, _local: None = Depends(require_local_admin),
           session: Session = Depends(get_db)):
    return _errors(lambda: core.lookup(session, name, type))
