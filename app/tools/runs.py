"""Tool run log (schema 25): one row per tool run that actually started.

What is stored: tool name, agent, status, duration, approval id, and a short
error. Arguments and outputs are never stored (they can hold private data).

Where it is written: in the request's bound session when there is one (a chat
turn), so the row commits with the turn and never waits for SQLite's single
write lock; otherwise in a short session of its own. Logging never changes a
tool's result: a failed write is logged and dropped.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

logger = logging.getLogger(__name__)

REFUSED = "refused"  # pause or daily budget stopped the run before it started
ERROR_CHARS = 180


class ToolRunRecord(Base):
    __tablename__ = "tool_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(String(ERROR_CHARS), nullable=True)


def record_run(
    tool_name: str,
    *,
    agent_id: str | None,
    status: str,
    started: float,
    approval_id: str | None = None,
    error: str | None = None,
) -> None:
    """Append one row. `started` is a time.monotonic() value."""
    row = ToolRunRecord(
        id=str(uuid4()),
        created_at=datetime.now(timezone.utc),
        agent_id=agent_id,
        tool_name=tool_name[:64],
        status=status[:32],
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        approval_id=approval_id,
        error=(error or None) and error[:ERROR_CHARS],
    )
    from app.agents.team import _session_scope

    session, owned = _session_scope()
    try:
        # Bound session: the row joins the turn's transaction and commits with
        # it, like the permission audit does. Every field is cut to its column
        # size above, so the row cannot make that commit fail.
        session.add(row)
        if owned:
            session.commit()
    except Exception:  # noqa: BLE001 - the log must never break a tool
        logger.warning("Could not record tool run for %s", tool_name, exc_info=True)
        if owned:
            session.rollback()
    finally:
        if owned:
            session.close()
