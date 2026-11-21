"""Writing style profile and training-data export. Local admin only."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import style
from app.api.routes.permissions import require_local_admin
from app.core.database import get_db

router = APIRouter()


class StyleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    description: str = Field(default="", max_length=style.MAX_DESCRIPTION)
    samples: list[str] = Field(default_factory=list, max_length=style.MAX_SAMPLES)


@router.get("/style")
def read_style(_local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    return style.read_profile(session)


@router.put("/style")
def update_style(
    payload: StyleUpdate,
    _local: None = Depends(require_local_admin),
    session: Session = Depends(get_db),
):
    from fastapi import HTTPException

    if any(len(sample) > style.MAX_SAMPLE_CHARS for sample in payload.samples):
        raise HTTPException(status_code=422, detail="A sample is longer than 2000 characters.")
    samples = [sample for sample in payload.samples if sample.strip()]
    return style.write_profile(session, payload.enabled, payload.description.strip(), samples)


@router.get("/style/dataset")
def dataset(_local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    return style.dataset_status(session)


@router.post("/style/dataset/export")
def export_dataset(_local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    return style.export_dataset(session)
