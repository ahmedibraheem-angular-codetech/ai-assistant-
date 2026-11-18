"""Cancel pending MCP approvals when a sealed fact is no longer current.

These functions open their own database session. They do not take the gateway
lock, the loader lock, or a session lock, except where reading the published
snapshot takes the loader lock after the caller has released it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.approvals.models import ApprovalRequestRecord
from app.approvals.payloads import McpExecutionPayloadRecord
from app.approvals.review import MCP_TOOLS
from app.permissions.audit import add_audit

UNKNOWN_RESULT = "The MCP operation may have reached the server. It will not be retried."
NOT_SENT = "The MCP operation was not sent."


def invalidate_discovery(server_id: str, generation: int) -> None:
    _cancel(lambda arguments: arguments.get("server_id") == server_id and arguments.get("discovery_generation") != generation)


def invalidate_generation(server_id: str, generation: int) -> None:
    _cancel(
        lambda arguments: arguments.get("server_id") == server_id and arguments.get("process_generation") == generation
    )


def invalidate_epoch(epoch: int) -> None:
    from app.mcp.loader import current_snapshot

    snapshot = current_snapshot()

    def stale(arguments: dict) -> bool:
        if snapshot is None:
            return True
        return arguments.get("epoch") != snapshot.epoch or arguments.get("config_revision") != snapshot.revision

    del epoch
    _cancel(stale)


def settle_mcp_approvals() -> None:
    """Terminalize MCP rows left in APPROVED or EXECUTING. This does not write a frame."""
    from app.core.database import get_session_factory

    session = get_session_factory()()
    try:
        rows = list(
            session.scalars(
                select(ApprovalRequestRecord).where(
                    ApprovalRequestRecord.tool_name.in_(MCP_TOOLS),
                    ApprovalRequestRecord.status.in_(("APPROVED", "EXECUTING")),
                )
            ).all()
        )
        if not rows:
            return
        now = datetime.now(timezone.utc)
        for record in rows:
            executing = record.status == "EXECUTING"
            record.status = "FAILED"
            record.decided_at = record.decided_at or now
            if executing:
                record.execution_status = "unknown"
                record.result_text = UNKNOWN_RESULT
            else:
                record.execution_status = "not_dispatched"
                record.result_text = NOT_SENT
            _delete_payload(session, record.id)
            add_audit(
                session,
                event="approval_failed",
                decision="FAILED",
                detail=record.result_text,
                agent_id=record.agent_id,
                tool_name=record.tool_name,
                resource_type=record.resource_type,
                resource_id=record.id,
                action=record.action,
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def delete_payload(session, approval_id: str) -> None:
    _delete_payload(session, approval_id)


def _cancel(selected) -> None:
    from app.core.database import get_session_factory

    session = get_session_factory()()
    try:
        rows = list(
            session.scalars(
                select(ApprovalRequestRecord).where(
                    ApprovalRequestRecord.tool_name.in_(MCP_TOOLS),
                    ApprovalRequestRecord.status == "PENDING",
                )
            ).all()
        )
        now = datetime.now(timezone.utc)
        changed = False
        for record in rows:
            arguments = _arguments(record.arguments_json)
            if arguments is None or selected(arguments):
                record.status = "CANCELLED"
                record.decided_at = now
                record.execution_status = "CANCELLED"
                _delete_payload(session, record.id)
                add_audit(
                    session,
                    event="approval_cancelled",
                    decision="CANCELLED",
                    detail="The permission behind this request changed.",
                    agent_id=record.agent_id,
                    tool_name=record.tool_name,
                    resource_type=record.resource_type,
                    resource_id=record.id,
                    action=record.action,
                )
                changed = True
        if changed:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _arguments(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _delete_payload(session, approval_id: str) -> None:
    row = session.get(McpExecutionPayloadRecord, approval_id)
    if row is not None:
        session.delete(row)
