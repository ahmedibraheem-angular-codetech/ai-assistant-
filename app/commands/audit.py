"""Command lifecycle notes in the existing permission audit log.

A command starts only after ``command_started`` commits. Completion is written
on a second attempt when the first commit fails. A started row with no
terminal row is closed once, as ``command_reconciled``, without copying
output, paths, or the environment.
"""

import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.commands.faults import controller
from app.commands.registry import CommandSpec
from app.core.database import get_session_factory
from app.permissions.audit import add_audit
from app.permissions.models import PermissionAuditRecord
from app.tools.exceptions import ToolExecutionError

_EXEC = re.compile(r"exec=([0-9a-f]{32})")
_TERMINAL = frozenset({"command_finished", "command_reconciled"})
_START = "The command could not be recorded."
_FINISH = "The command result could not be recorded."


def begin_command_audit(spec: CommandSpec, arguments: dict) -> str:
    """Commit the start row. The command must not run when this raises."""
    execution_id = uuid4().hex
    _write(
        spec,
        arguments,
        execution_id,
        "command_started",
        "started",
        "running",
        None,
        attempts=2,
        failure=_START,
    )
    return execution_id


def finish_command_audit(
    spec: CommandSpec,
    arguments: dict,
    execution_id: str,
    status: str,
    exit_code: int | None,
) -> None:
    """Commit the completion row. A failed commit raises instead of looking successful."""
    _write(
        spec,
        arguments,
        execution_id,
        "command_finished",
        status,
        status,
        exit_code,
        attempts=2,
        failure=_FINISH,
    )


def reconcile_command_audits() -> int:
    """Close started commands that never recorded completion. A second call adds nothing."""
    if controller.block_reconcile:
        raise _database_unavailable()
    last: Exception | None = None
    for _attempt in (1, 2):
        session = get_session_factory()()
        try:
            rows = list(session.scalars(select(PermissionAuditRecord)).all())
            inserted = 0
            seen: set[str] = set()
            for row in rows:
                if row.event != "command_started":
                    continue
                identity = _execution_id(row.detail)
                if identity is None or identity in seen:
                    continue
                seen.add(identity)
                command_id = row.tool_name if isinstance(row.tool_name, str) and row.tool_name else "command"
                inserted += _insert_terminal(
                    session,
                    event="command_reconciled",
                    decision="unresolved",
                    detail=f"exec={identity} {command_id} unresolved",
                    agent_id=row.agent_id,
                    tool_name=command_id,
                    resource_type=row.resource_type,
                    resource_id=row.resource_id,
                    action=row.action or "execute",
                    execution_id=identity,
                )
            session.commit()
            return inserted
        except Exception as exc:
            session.rollback()
            last = exc
        finally:
            session.close()
    raise ToolExecutionError(_FINISH) from last


def _write(
    spec: CommandSpec,
    arguments: dict,
    execution_id: str,
    event: str,
    decision: str,
    detail: str,
    exit_code: int | None,
    *,
    attempts: int,
    failure: str,
) -> None:
    from app.files.authorization import _AGENT, _APPROVAL

    agent_id = _AGENT.get() if isinstance(_AGENT.get(), str) else None
    approval_id = _APPROVAL.get() if isinstance(_APPROVAL.get(), str) else None
    exit_text = "" if exit_code is None else f" exit={exit_code}"
    text = f"exec={execution_id} {spec.command_id} {detail}{exit_text}"
    resource_id = approval_id or (arguments.get("workspace_id") if isinstance(arguments.get("workspace_id"), str) else None)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        session = get_session_factory()()
        try:
            if event in _TERMINAL:
                _insert_terminal(
                    session,
                    event=event,
                    decision=decision,
                    detail=text,
                    agent_id=agent_id,
                    tool_name=spec.command_id,
                    resource_type="workspace",
                    resource_id=resource_id,
                    action="execute",
                    execution_id=execution_id,
                )
            else:
                add_audit(
                    session,
                    event=event,
                    decision=decision,
                    detail=text,
                    agent_id=agent_id,
                    tool_name=spec.command_id,
                    resource_type="workspace",
                    resource_id=resource_id,
                    action="execute",
                )
            _commit(session, event, attempt)
            return
        except Exception as exc:
            session.rollback()
            last = exc
        finally:
            session.close()
    raise ToolExecutionError(failure) from last


def _commit(session, event: str, attempt: int) -> None:
    mode = controller.commit_failure
    if mode == event or (mode == "finished_once" and event == "command_finished" and attempt == 1):
        raise _database_unavailable()
    session.commit()


def _insert_terminal(
    session,
    *,
    event: str,
    decision: str,
    detail: str,
    agent_id: str | None,
    tool_name: str | None,
    resource_type: str | None,
    resource_id: str | None,
    action: str | None,
    execution_id: str,
) -> int:
    """Insert one terminal row unless that execution already has one.

    The existence check and the insert are one statement, so two writers
    cannot both commit a completion or reconciliation row.
    """
    cleaned = " ".join(detail.split())[:180]
    session.execute(
        text(
            """
            INSERT INTO permission_audit (
                created_at, event, decision, agent_id, tool_name,
                resource_type, resource_id, action, detail
            )
            SELECT :created_at, :event, :decision, :agent_id, :tool_name,
                   :resource_type, :resource_id, :action, :detail
            WHERE NOT EXISTS (
                SELECT 1 FROM permission_audit
                WHERE event IN ('command_finished', 'command_reconciled')
                AND detail LIKE :prefix
            )
            """
        ),
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "event": event[:32],
            "decision": decision[:32],
            "agent_id": None if agent_id is None else agent_id[:36],
            "tool_name": None if tool_name is None else tool_name[:64],
            "resource_type": None if resource_type is None else resource_type[:32],
            "resource_id": None if resource_id is None else str(resource_id)[:200],
            "action": None if action is None else action[:32],
            "detail": cleaned,
            "prefix": f"exec={execution_id} %",
        },
    )
    changed = session.execute(text("SELECT changes()")).scalar()
    return int(changed or 0)


def _database_unavailable() -> OperationalError:
    return OperationalError("unable to open database file", {}, RuntimeError("unable to open database file"))


def _execution_id(detail: str | None) -> str | None:
    if not isinstance(detail, str):
        return None
    match = _EXEC.search(detail)
    return None if match is None else match.group(1)
