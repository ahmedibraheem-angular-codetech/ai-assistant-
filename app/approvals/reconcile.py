"""Make an approval status match a finished file journal.

The journal id is stored on the journal row. Paths and hashes are not used to
guess which approval a change belongs to. A missing approval id is left alone.
"""

import logging

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.approvals.models import ApprovalRequestRecord
from app.core.database import get_session_factory
from app.files.journal import COMMITTED, FAILED, RECOVERED, ROLLBACK_REQUIRED
from app.files.models import FileOperationJournalRecord
from app.permissions.audit import add_audit

logger = logging.getLogger(__name__)

_CLOSED = frozenset({"REJECTED", "CANCELLED", "EXPIRED", "COMPLETED", "PENDING", "APPROVED"})
_UNSUCCESSFUL = frozenset({FAILED, ROLLBACK_REQUIRED, RECOVERED})
_SOURCES = frozenset({"startup", "execution"})
_logged: set[str] = set()


def journal_state_for(approval_id: str) -> str | None:
    session = get_session_factory()()
    try:
        row = session.scalar(
            select(FileOperationJournalRecord).where(FileOperationJournalRecord.approval_id == approval_id)
        )
        return None if row is None else row.state
    finally:
        session.close()


def record_worker_failure(approval_id: str, message: str) -> bool:
    """Store a worker failure only while the approval is still EXECUTING."""
    session = get_session_factory()()
    try:
        record = session.get(ApprovalRequestRecord, approval_id)
        if record is None or record.status != "EXECUTING":
            return False
        changed = session.execute(
            update(ApprovalRequestRecord)
            .where(
                ApprovalRequestRecord.id == approval_id,
                ApprovalRequestRecord.status == "EXECUTING",
            )
            .values(status="FAILED", execution_status="FAILED", result_text=message[:400])
        )
        if changed.rowcount != 1:
            session.rollback()
            return False
        add_audit(
            session,
            event="approval_failed",
            decision="FAILED",
            detail=message,
            agent_id=record.agent_id,
            tool_name=record.tool_name,
            resource_id=approval_id,
            action=record.action,
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reconcile_linked_approvals(source: str) -> None:
    """Correct linked approvals. A second call does not add another audit row."""
    if source not in _SOURCES:
        source = "execution"
    session = get_session_factory()()
    try:
        rows = list(session.scalars(select(FileOperationJournalRecord)).all())
        linked = [
            (row.id, row.state, row.approval_id)
            for row in rows
            if isinstance(row.approval_id, str) and row.approval_id
        ]
        unlinked = [row.id for row in rows if not row.approval_id]
    finally:
        session.close()
    for journal_id, state, approval_id in linked:
        _reconcile_one(journal_id, state, approval_id, source)
    for journal_id in unlinked:
        _warn_once(
            f"unlinked:{journal_id}",
            "File journal %s has no approval id, so its approval was left unchanged.",
            journal_id,
        )


def _reconcile_one(journal_id: str, state: str, approval_id: str, source: str) -> None:
    for _attempt in (1, 2):
        session = get_session_factory()()
        try:
            record = session.get(ApprovalRequestRecord, approval_id)
            if record is None:
                _warn_once(
                    f"missing:{approval_id}",
                    "File journal %s points at approval %s, which was not found.",
                    journal_id,
                    approval_id,
                )
                return
            status = record.status
            if status in _CLOSED:
                if state == COMMITTED and status in {"REJECTED", "CANCELLED", "EXPIRED"}:
                    _warn_once(
                        f"closed:{approval_id}",
                        "File journal %s is committed, but approval %s is %s and was not changed.",
                        journal_id,
                        approval_id,
                        status,
                    )
                return
            if state == COMMITTED and status in {"EXECUTING", "FAILED"}:
                if _complete(session, record, journal_id, status, source):
                    return
                continue
            if state in _UNSUCCESSFUL and status == "EXECUTING":
                if _fail(session, record, journal_id, state, source):
                    return
                continue
            if status == "EXECUTING":
                _warn_once(
                    f"open:{approval_id}:{state}",
                    "File journal %s is %s, so approval %s was not marked successful.",
                    journal_id,
                    state,
                    approval_id,
                )
            return
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _complete(session: Session, record: ApprovalRequestRecord, journal_id: str, previous: str, source: str) -> bool:
    values: dict[str, str] = {"status": "COMPLETED"}
    if previous == "EXECUTING":
        values["execution_status"] = "ok"
        values["result_text"] = '{"status":"ok"}'
    changed = session.execute(
        update(ApprovalRequestRecord)
        .where(ApprovalRequestRecord.id == record.id, ApprovalRequestRecord.status == previous)
        .values(**values)
    )
    if changed.rowcount != 1:
        session.rollback()
        return False
    reason = "lost-worker-response" if previous == "FAILED" else "journal-committed"
    _audit(session, record, journal_id, previous, "COMPLETED", source, reason)
    session.commit()
    return True


def _fail(session: Session, record: ApprovalRequestRecord, journal_id: str, state: str, source: str) -> bool:
    message = f"The file journal is {state}."
    changed = session.execute(
        update(ApprovalRequestRecord)
        .where(ApprovalRequestRecord.id == record.id, ApprovalRequestRecord.status == "EXECUTING")
        .values(status="FAILED", execution_status="FAILED", result_text=message[:400])
    )
    if changed.rowcount != 1:
        session.rollback()
        return False
    _audit(session, record, journal_id, "EXECUTING", "FAILED", source, f"journal-{state}")
    session.commit()
    return True


def _audit(
    session: Session,
    record: ApprovalRequestRecord,
    journal_id: str,
    previous: str,
    new: str,
    source: str,
    reason: str,
) -> None:
    detail = f"a={record.id} j={journal_id} {previous}->{new} {source} {reason}"
    add_audit(
        session,
        event="approval_reconciled",
        decision=new,
        detail=detail,
        agent_id=record.agent_id,
        tool_name=record.tool_name,
        resource_type="file",
        resource_id=record.id,
        action=previous,
    )


def _warn_once(key: str, message: str, *args: object) -> None:
    if key in _logged:
        return
    _logged.add(key)
    logger.warning(message, *args)
