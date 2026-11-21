"""Proactive insights for the Mac app. Read-only."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import insights
from app.api.routes.permissions import require_local_admin
from app.core.database import get_db

router = APIRouter()


@router.get("/insights")
def read_insights(_local: None = Depends(require_local_admin), session: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    items = insights.collect(session, now)
    return {"insights": items, "count": len(items), "generated_at": now.isoformat()}
