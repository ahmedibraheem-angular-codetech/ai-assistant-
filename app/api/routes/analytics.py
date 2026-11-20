"""Dashboard analytics for the Mac app. Read-only."""

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import analytics
from app.api.routes.permissions import require_local_admin
from app.core.database import get_db

router = APIRouter()


@router.get("/analytics/summary")
def read_summary(
    range: Literal["today", "7d", "30d"] = "7d",  # noqa: A002 - public query name
    _local: None = Depends(require_local_admin),
    session: Session = Depends(get_db),
):
    return analytics.summary(session, range)
