"""Append-only permission notes. The caller commits the session."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.permissions.models import PermissionAuditRecord

_LIMIT = 180


def add_audit(
    session: Session,
    *,
    event: str,
    detail: str,
    decision: str | None = None,
    agent_id: str | None = None,
    tool_name: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    action: str | None = None,
) -> None:
    session.add(
        PermissionAuditRecord(
            created_at=datetime.now(timezone.utc),
            event=event[:32],
            decision=None if decision is None else decision[:32],
            agent_id=None if agent_id is None else agent_id[:36],
            tool_name=None if tool_name is None else tool_name[:64],
            resource_type=None if resource_type is None else resource_type[:32],
            resource_id=None if resource_id is None else str(resource_id)[:200],
            action=None if action is None else action[:32],
            detail=" ".join(detail.split())[:_LIMIT],
        )
    )
