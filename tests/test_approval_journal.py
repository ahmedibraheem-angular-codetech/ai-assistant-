"""Approval status follows a linked file journal, and only that journal."""

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.approvals.models import ApprovalRequestRecord
from app.approvals.reconcile import reconcile_linked_approvals, record_worker_failure
from app.approvals.service import ApprovalService
from app.core.database import get_engine, get_session_factory
from app.files.journal import begin_journal, mark_journal
from app.permissions.models import PermissionAuditRecord
from app.tools.registry import get_tool_registry


def _approval(status: str, result: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    approval_id = str(uuid4())
    with Session(get_engine()) as session:
        session.add(
            ApprovalRequestRecord(
                id=approval_id,
                agent_id=None,
                tool_name="update_text_file",
                action="write",
                resource_type="file",
                resource_id="workspace",
                relative_path="note.txt",
                arguments_json="{}",
                fingerprint=approval_id,
                description="Update note.txt",
                risk="sensitive",
                reason="needs confirmation",
                mode="ask_sensitive",
                status=status,
                created_at=now,
                expires_at=now + timedelta(hours=1),
                decided_at=None,
                execution_status=None,
                result_text=result,
            )
        )
        session.commit()
    return approval_id


def _journal(approval_id: str | None, state: str) -> str:
    journal_id = begin_journal(
        workspace_id=str(uuid4()),
        relative_path="note.txt",
        operation="update",
        original_sha256="a" * 64,
        proposed_sha256="b" * 64,
        approval_id=approval_id,
    )
    if state != "PREPARED":
        mark_journal(journal_id, state)
    return journal_id


def _status(approval_id: str) -> tuple[str, str | None]:
    with Session(get_engine()) as session:
        row = session.get(ApprovalRequestRecord, approval_id)
        assert row is not None
        return row.status, row.result_text


def _audit_count(approval_id: str | None = None) -> int:
    with Session(get_engine()) as session:
        query = select(func.count()).select_from(PermissionAuditRecord).where(
            PermissionAuditRecord.event == "approval_reconciled"
        )
        if approval_id is not None:
            query = query.where(PermissionAuditRecord.resource_id == approval_id)
        return int(session.scalar(query) or 0)


def test_worker_loss_corrects_failed_and_executing_approvals(client):
    failed = _approval("FAILED", "The tool could not be completed.")
    executing = _approval("EXECUTING")
    done = _approval("COMPLETED", '{"status":"ok"}')
    _journal(failed, "COMMITTED")
    _journal(executing, "COMMITTED")
    _journal(done, "COMMITTED")
    unrelated = _approval("EXECUTING", "leave me")
    reconcile_linked_approvals("startup")
    reconcile_linked_approvals("startup")
    assert _status(failed) == ("COMPLETED", "The tool could not be completed.")
    assert _status(executing) == ("COMPLETED", '{"status":"ok"}')
    assert _status(done) == ("COMPLETED", '{"status":"ok"}')
    assert _status(unrelated) == ("EXECUTING", "leave me")
    assert _audit_count(failed) == 1
    assert _audit_count(executing) == 1
    assert _audit_count(done) == 0
    assert _audit_count(unrelated) == 0
    with Session(get_engine()) as session:
        detail = session.scalar(
            select(PermissionAuditRecord.detail).where(
                PermissionAuditRecord.event == "approval_reconciled",
                PermissionAuditRecord.resource_id == failed,
            )
        )
    assert detail is not None
    assert "lost-worker-response" in detail
    assert "startup" in detail
    assert failed in detail


def test_closed_and_unsuccessful_journals_are_not_completed(client):
    for status in ("REJECTED", "CANCELLED", "EXPIRED"):
        approval_id = _approval(status, "closed")
        _journal(approval_id, "COMMITTED")
        reconcile_linked_approvals("execution")
        assert _status(approval_id) == (status, "closed")
        assert _audit_count(approval_id) == 0
    for state in ("FAILED", "ROLLBACK_REQUIRED", "RECOVERED", "PREPARED", "BACKUP_READY", "APPLYING", "APPLIED"):
        approval_id = _approval("EXECUTING")
        _journal(approval_id, state)
        reconcile_linked_approvals("execution")
        status, _result = _status(approval_id)
        assert status != "COMPLETED"
        if state in {"FAILED", "ROLLBACK_REQUIRED", "RECOVERED"}:
            assert status == "FAILED"
        else:
            assert status == "EXECUTING"
        reconcile_linked_approvals("execution")
        assert _audit_count(approval_id) <= 1


def test_legacy_journal_without_an_approval_id_does_not_change_an_approval(client):
    approval_id = _approval("EXECUTING", "pending work")
    _journal(None, "COMMITTED")
    reconcile_linked_approvals("startup")
    assert _status(approval_id) == ("EXECUTING", "pending work")
    assert _audit_count(approval_id) == 0


def test_repeated_and_concurrent_reconciliation_writes_one_audit(client):
    approval_id = _approval("FAILED", "The tool could not be completed.")
    _journal(approval_id, "COMMITTED")
    reconcile_linked_approvals("execution")
    reconcile_linked_approvals("startup")
    assert _status(approval_id) == ("COMPLETED", "The tool could not be completed.")
    assert _audit_count(approval_id) == 1

    racing = _approval("EXECUTING")
    _journal(racing, "COMMITTED")
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            barrier.wait(timeout=5)
            reconcile_linked_approvals("execution")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    first = threading.Thread(target=_run)
    second = threading.Thread(target=_run)
    first.start()
    second.start()
    first.join()
    second.join()
    assert errors == []
    assert _status(racing)[0] == "COMPLETED"
    assert _audit_count(racing) == 1


def test_worker_failure_during_reconciliation_stays_completed(client):
    approval_id = _approval("EXECUTING")
    _journal(approval_id, "COMMITTED")
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _reconcile() -> None:
        try:
            barrier.wait(timeout=5)
            reconcile_linked_approvals("execution")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _worker() -> None:
        try:
            barrier.wait(timeout=5)
            record_worker_failure(approval_id, "worker exited after commit")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    left = threading.Thread(target=_reconcile)
    right = threading.Thread(target=_worker)
    left.start()
    right.start()
    left.join()
    right.join()
    assert errors == []
    status, result = _status(approval_id)
    if status == "FAILED":
        reconcile_linked_approvals("execution")
        status, result = _status(approval_id)
    assert status == "COMPLETED"
    assert result in {'{"status":"ok"}', "worker exited after commit"}
    assert _audit_count(approval_id) == 1
    reconcile_linked_approvals("execution")
    assert _audit_count(approval_id) == 1


def test_a_successful_worker_response_cannot_complete_an_uncommitted_journal(client):
    approval_id = _approval("EXECUTING")
    _journal(approval_id, "FAILED")
    service = ApprovalService(get_session_factory()(), get_tool_registry())
    try:
        finished = service._finish_file_approval(
            approval_id,
            "update_text_file",
            None,
            "write",
            SimpleNamespace(status="ok", output={"status": "ok"}, error=None),
        )
        assert finished.status == "FAILED"
    finally:
        service.session.close()
    assert _status(approval_id)[0] == "FAILED"
