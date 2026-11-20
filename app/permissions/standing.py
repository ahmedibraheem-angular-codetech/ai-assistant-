"""Standing approvals: a user's lasting yes for reads by one agent.

Scope rules (kept narrow on purpose):
- Only tools whose manifest is risk "read", read_only, and approval_required.
  Mutations, commands, sensitive tools, and MCP calls still ask every time.
- Applies only in ask_sensitive mode. always_ask keeps asking; restricted_auto
  keeps denying.
- Workspace tools need a workspace_id and may narrow to a path prefix. The
  normal tool grant, workspace grant, and path allow-list still apply.
- Created, listed, and revoked only through the local admin API.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.files.access import relative_parts
from app.files.exceptions import FileAccessError
from app.permissions.audit import add_audit
from app.permissions.manifests import ToolManifest, manifest_for_tool
from app.permissions.models import StandingApprovalRecord

MAX_ACTIVE_PER_AGENT = 50


class StandingApprovalError(ValueError):
    """The request cannot become a standing approval."""


def eligible(manifest: ToolManifest | None) -> bool:
    return bool(
        manifest is not None
        and manifest.risk == "read"
        and manifest.read_only
        and manifest.approval_required
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _live(row: StandingApprovalRecord, now: datetime) -> bool:
    if row.revoked_at is not None:
        return False
    expires = _aware(row.expires_at)
    return expires is None or expires > now


def _normalize(path: str | None) -> str | None:
    if path is None or path.strip() in {"", "."}:
        return None
    try:
        parts = relative_parts(path)
    except FileAccessError as exc:
        raise StandingApprovalError("That path is not valid.") from exc
    return "/".join(parts) if parts else None


def matches(
    session: Session,
    *,
    agent_id: str | None,
    tool_name: str | None,
    manifest: ToolManifest | None,
    workspace_required: bool,
    workspace_id: str | None,
    relative_path: str | None,
) -> bool:
    """True when a live standing approval covers this exact read."""
    if agent_id is None or tool_name is None or not eligible(manifest):
        return False
    rows = session.scalars(
        select(StandingApprovalRecord).where(
            StandingApprovalRecord.agent_id == agent_id,
            StandingApprovalRecord.tool_name == tool_name,
        )
    ).all()
    if not rows:
        return False
    now = _now()
    target: str | None = None
    if workspace_required:
        if not workspace_id:
            return False
        if relative_path:
            try:
                parts = relative_parts(relative_path)
            except FileAccessError:
                return False
            target = "/".join(parts) if parts else None
    for row in rows:
        if not _live(row, now):
            continue
        if workspace_required:
            if row.workspace_id != workspace_id:
                continue
            prefix = row.path_prefix
            if prefix is None:
                return True
            if target is not None and (target == prefix or target.startswith(prefix + "/")):
                return True
            continue
        if row.workspace_id is None:
            return True
    return False


def _public(row: StandingApprovalRecord) -> dict:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "tool_name": row.tool_name,
        "workspace_id": row.workspace_id,
        "path_prefix": row.path_prefix,
        "created_at": _aware(row.created_at).isoformat(),
        "expires_at": None if row.expires_at is None else _aware(row.expires_at).isoformat(),
        "active": _live(row, _now()),
    }


def create(
    session: Session,
    *,
    registry,
    agent_id: str,
    tool_name: str,
    workspace_id: str | None,
    path_prefix: str | None,
    expires_in_days: int | None,
) -> dict:
    tool = registry.get(tool_name)
    manifest = manifest_for_tool(tool) if tool is not None else None
    if tool is None:
        raise StandingApprovalError("That tool is not available.")
    if not eligible(manifest):
        raise StandingApprovalError(
            "Standing approvals are only for read-only tools that normally ask. "
            "Changes, commands, and sensitive tools always ask."
        )
    workspace_tool = "workspace" in manifest.resource_types
    if workspace_tool and not workspace_id:
        raise StandingApprovalError("This tool needs a workspace_id.")
    if not workspace_tool and (workspace_id or path_prefix):
        raise StandingApprovalError("This tool does not use a workspace.")
    prefix = _normalize(path_prefix) if workspace_tool else None
    now = _now()
    active = [
        row
        for row in session.scalars(
            select(StandingApprovalRecord).where(StandingApprovalRecord.agent_id == agent_id)
        ).all()
        if _live(row, now)
    ]
    if len(active) >= MAX_ACTIVE_PER_AGENT:
        raise StandingApprovalError("This agent has too many standing approvals. Revoke some first.")
    row = StandingApprovalRecord(
        id=str(uuid4()),
        agent_id=agent_id,
        tool_name=tool_name,
        workspace_id=workspace_id if workspace_tool else None,
        path_prefix=prefix,
        created_at=now,
        expires_at=None if expires_in_days is None else now + timedelta(days=expires_in_days),
        revoked_at=None,
    )
    session.add(row)
    add_audit(
        session,
        event="standing_approval_added",
        detail=f"scope={prefix or '*'} expires_in_days={expires_in_days or 'never'}",
        agent_id=agent_id,
        tool_name=tool_name,
        resource_type="workspace" if workspace_tool else "none",
        resource_id=workspace_id,
        action="read",
    )
    session.commit()
    return _public(row)


def list_for(session: Session, agent_id: str | None = None) -> list[dict]:
    query = select(StandingApprovalRecord).order_by(StandingApprovalRecord.created_at)
    if agent_id is not None:
        query = query.where(StandingApprovalRecord.agent_id == agent_id)
    return [_public(row) for row in session.scalars(query).all() if row.revoked_at is None]


def revoke(session: Session, approval_id: str) -> bool:
    row = session.get(StandingApprovalRecord, approval_id)
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = _now()
    add_audit(
        session,
        event="standing_approval_revoked",
        detail=f"scope={row.path_prefix or '*'}",
        agent_id=row.agent_id,
        tool_name=row.tool_name,
        resource_id=row.workspace_id,
        action="read",
    )
    session.commit()
    return True
