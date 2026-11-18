"""Create, decide, and resume approval requests.

The permission engine remains the authority. This module does not treat an
approval as a permanent grant, and it does not leave a database transaction
open while a tool runs.
"""

import asyncio
import hmac
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approvals.fingerprint import operation_fingerprint, present_arguments
from app.approvals.mcp_policy import NOT_SENT, UNKNOWN_RESULT, delete_payload
from app.approvals.payloads import McpExecutionPayloadRecord
from app.approvals.review import MCP_CALL_TOOL, MCP_READ_TOOL, MCP_TOOLS, build_review, stored_path_matches
from app.approvals.models import ApprovalRequestRecord
from app.core.config import get_settings
from app.permissions.audit import add_audit
from app.permissions.engine import ALLOW, DENY, REQUIRES_APPROVAL
from app.permissions.manifests import manifest_for_tool
from app.permissions.service import evaluate_request, request_for_tool
from app.tools.base import SIDE_EFFECT
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry

PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
CANCELLED = "CANCELLED"
EXECUTING = "EXECUTING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

_FILE_MUTATIONS = frozenset({"create_text_file", "update_text_file", "quarantine_file"})
_FILE_TOOLS = frozenset(
    {
        "list_files",
        "search_files",
        "read_file",
        "get_file_metadata",
        "inspect_project",
        "create_text_file",
        "update_text_file",
        "quarantine_file",
        "show_directory",
        "measure_file",
        "preview_text",
        "bounded_wait",
    }
)


class ApprovalNotFound(Exception):
    def __init__(self) -> None:
        super().__init__("That approval request was not found.")


class ApprovalConflict(Exception):
    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"That approval request is {status}.")


class ApprovalNotReviewable(Exception):
    def __init__(self) -> None:
        super().__init__("That operation cannot be reviewed for execution.")


class ApprovalService:
    def __init__(self, session: Session, registry: ToolRegistry, executor: ToolExecutor | None = None) -> None:
        self.session = session
        self.registry = registry
        self.executor = executor or ToolExecutor()
        self.last_output: dict | None = None

    def create(
        self,
        *,
        agent_id: str | None,
        tool_name: str,
        arguments: dict,
        reason: str,
        mode: str,
    ) -> ApprovalRequestRecord:
        from app.files.changes import discard_proposal, inspect_change, write_proposal

        spec = request_for_tool(self.registry, tool_name, arguments)
        tool = self.registry.get(tool_name)
        manifest = manifest_for_tool(tool) if tool is not None else None
        staged = inspect_change(tool_name, agent_id, arguments)
        stored_arguments = staged.arguments
        fingerprint = operation_fingerprint(
            agent_id=agent_id,
            tool_name=tool_name,
            action=spec["action"],
            resource_type=spec["resource_type"],
            resource_id=spec["resource_id"],
            arguments=stored_arguments,
        )
        self._expire_due()
        existing = self.session.scalar(
            select(ApprovalRequestRecord).where(
                ApprovalRequestRecord.fingerprint == fingerprint,
                ApprovalRequestRecord.status == PENDING,
            )
        )
        if existing is not None:
            return existing
        now = datetime.now(timezone.utc)
        record_id = str(uuid4())
        if staged.payload is not None:
            write_proposal(record_id, staged.payload)
        record = ApprovalRequestRecord(
            id=record_id,
            agent_id=agent_id,
            tool_name=tool_name,
            action=spec["action"],
            resource_type=spec["resource_type"],
            resource_id=spec["resource_id"],
            relative_path=stored_arguments.get("relative_path") or spec["relative_path"],
            arguments_json=json.dumps(stored_arguments, sort_keys=True),
            fingerprint=fingerprint,
            description=_description(tool.description if tool is not None else tool_name, spec),
            risk=manifest.risk if manifest is not None else "sensitive",
            reason=reason[:180],
            mode=mode,
            status=PENDING,
            created_at=now,
            expires_at=now + timedelta(seconds=get_settings().approval_ttl_seconds),
            decided_at=None,
            execution_status=None,
            result_text=None,
        )
        add_audit(
            self.session,
            event="approval_created",
            decision=REQUIRES_APPROVAL,
            detail=record.reason,
            agent_id=agent_id,
            tool_name=tool_name,
            resource_type=spec["resource_type"],
            resource_id=record.id,
            action=spec["action"],
        )
        if stored_arguments.get("operation") in {"create", "update", "delete"}:
            digest = ""
            content = stored_arguments.get("content")
            if isinstance(content, dict) and isinstance(content.get("digest"), str):
                digest = content["digest"][:16]
            add_audit(
                self.session,
                event="file_change_proposed",
                decision=REQUIRES_APPROVAL,
                detail=f"{stored_arguments['operation']} {stored_arguments.get('relative_path', '')} {digest}".strip()[:180],
                agent_id=agent_id,
                tool_name=tool_name,
                resource_type="file",
                resource_id=record.id,
                action=spec["action"],
            )
        self.session.add(record)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            discard_proposal(record_id)
            existing = self.session.scalar(
                select(ApprovalRequestRecord).where(
                    ApprovalRequestRecord.fingerprint == fingerprint,
                    ApprovalRequestRecord.status == PENDING,
                )
            )
            if existing is None:
                raise
            return existing
        return record

    def create_mcp(
        self,
        *,
        agent_id: str | None,
        tool_name: str,
        arguments: dict,
        reason: str,
        mode: str,
    ) -> ApprovalRequestRecord:
        """Seal one MCP operation and commit the approval and payload together.

        The approval id and expiration are chosen before any MCP wait. The
        gateway returns a package and does not touch this session. The
        discovery pin is held only across the local commit.
        """
        from app.mcp.gateway import GatewayError
        from app.mcp.session import SessionError

        if tool_name not in MCP_TOOLS or not isinstance(agent_id, str):
            raise ApprovalNotReviewable()
        approval_id = str(uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=get_settings().approval_ttl_seconds)
        try:
            package = _seal_mcp_package(
                approval_id=approval_id,
                agent_id=agent_id,
                tool_name=tool_name,
                arguments=arguments,
                expires_at=expires_at,
            )
        except (GatewayError, SessionError, ApprovalNotReviewable) as exc:
            raise ApprovalNotReviewable() from exc
        return self._commit_mcp_package(
            package,
            agent_id=agent_id,
            tool_name=tool_name,
            reason=reason,
            mode=mode,
            expires_at=expires_at,
        )

    async def create_mcp_async(
        self,
        *,
        agent_id: str | None,
        tool_name: str,
        arguments: dict,
        reason: str,
        mode: str,
    ) -> ApprovalRequestRecord:
        """Seal on a worker thread, then commit on this session's thread."""
        from app.mcp.gateway import GatewayError
        from app.mcp.session import SessionError

        if tool_name not in MCP_TOOLS or not isinstance(agent_id, str):
            raise ApprovalNotReviewable()
        approval_id = str(uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=get_settings().approval_ttl_seconds)
        try:
            package = await asyncio.to_thread(
                _seal_mcp_package,
                approval_id=approval_id,
                agent_id=agent_id,
                tool_name=tool_name,
                arguments=arguments,
                expires_at=expires_at,
            )
        except (GatewayError, SessionError, ApprovalNotReviewable) as exc:
            raise ApprovalNotReviewable() from exc
        return self._commit_mcp_package(
            package,
            agent_id=agent_id,
            tool_name=tool_name,
            reason=reason,
            mode=mode,
            expires_at=expires_at,
        )

    def _commit_mcp_package(
        self,
        package,
        *,
        agent_id: str,
        tool_name: str,
        reason: str,
        mode: str,
        expires_at,
    ) -> ApprovalRequestRecord:
        from app.mcp.session import pin_discovery

        approval_id = package.arguments["approval_id"]
        pin = None
        try:
            hook = _before_pin
            if hook is not None:
                hook(package)
            pin = pin_discovery(package.session, package.discovery_at_response)
            after = _after_pin
            if after is not None:
                after(pin)
            current = operation_fingerprint(
                agent_id=agent_id,
                tool_name=tool_name,
                action=package.action,
                resource_type=package.resource_type,
                resource_id=None,
                arguments=package.arguments,
            )
            if not hmac.compare_digest(current, package.fingerprint):
                raise ApprovalNotReviewable()
            record = ApprovalRequestRecord(
                id=approval_id,
                agent_id=agent_id,
                tool_name=tool_name,
                action=package.action,
                resource_type=package.resource_type,
                resource_id=None,
                relative_path=None,
                arguments_json=json.dumps(package.arguments, sort_keys=True, separators=(",", ":")),
                fingerprint=package.fingerprint,
                description=package.description,
                risk="sensitive",
                reason=reason[:180],
                mode=mode,
                status=PENDING,
                created_at=datetime.now(timezone.utc),
                expires_at=expires_at,
                decided_at=None,
                execution_status=None,
                result_text=None,
            )
            self.session.add(record)
            self.session.add(
                McpExecutionPayloadRecord(
                    approval_id=approval_id,
                    nonce=package.nonce,
                    ciphertext=package.ciphertext,
                )
            )
            self._audit(record, "approval_required", REQUIRES_APPROVAL, reason)
            fault = _commit_fault
            if fault is not None:
                fault()
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        finally:
            if pin is not None:
                pin.release()
        return record

    def list_pending(self) -> list[ApprovalRequestRecord]:
        self._expire_due()
        statement = (
            select(ApprovalRequestRecord)
            .where(ApprovalRequestRecord.status == PENDING)
            .order_by(ApprovalRequestRecord.created_at.asc())
        )
        return list(self.session.scalars(statement).all())

    def history(self) -> list[ApprovalRequestRecord]:
        self._expire_due()
        statement = (
            select(ApprovalRequestRecord)
            .where(ApprovalRequestRecord.status != PENDING)
            .order_by(ApprovalRequestRecord.created_at.desc())
        )
        return list(self.session.scalars(statement).all())

    def get(self, request_id: str) -> ApprovalRequestRecord:
        self._expire_due()
        record = self.session.get(ApprovalRequestRecord, request_id)
        if record is None:
            raise ApprovalNotFound()
        return record

    async def approve(self, request_id: str) -> ApprovalRequestRecord:
        current = self.get(request_id)
        if current.tool_name in MCP_TOOLS:
            return await self._execute_mcp(request_id)
        record = self._claim(request_id, PENDING, APPROVED, "approval_granted")
        return await self._execute(record.id)

    def reject(self, request_id: str) -> ApprovalRequestRecord:
        return self._claim(request_id, PENDING, REJECTED, "approval_rejected")

    def cancel(self, request_id: str) -> ApprovalRequestRecord:
        return self._claim(request_id, PENDING, CANCELLED, "approval_cancelled")

    def cancel_pending(
        self,
        *,
        agent_id: str | None = None,
        tool_names: set[str] | None = None,
        workspace_id: str | None = None,
    ) -> None:
        statement = select(ApprovalRequestRecord).where(ApprovalRequestRecord.status == PENDING)
        if agent_id is not None:
            statement = statement.where(ApprovalRequestRecord.agent_id == agent_id)
        if tool_names is not None:
            statement = statement.where(ApprovalRequestRecord.tool_name.in_(tool_names))
        if workspace_id is not None:
            statement = statement.where(ApprovalRequestRecord.resource_id == workspace_id)
        now = datetime.now(timezone.utc)
        for record in self.session.scalars(statement).all():
            record.status = CANCELLED
            record.decided_at = now
            record.execution_status = CANCELLED
            _discard_proposal(record.id)
            delete_payload(self.session, record.id)
            add_audit(
                self.session,
                event="approval_cancelled",
                decision=CANCELLED,
                detail="The permission behind this request changed.",
                agent_id=record.agent_id,
                tool_name=record.tool_name,
                resource_type=record.resource_type,
                resource_id=record.id,
                action=record.action,
            )

    async def _execute(self, request_id: str) -> ApprovalRequestRecord:
        self.session.expire_all()
        record = self.session.get(ApprovalRequestRecord, request_id)
        if record is None:
            raise ApprovalNotFound()
        if record.status != APPROVED:
            raise ApprovalConflict(record.status)
        arguments = json.loads(record.arguments_json)
        current = operation_fingerprint(
            agent_id=record.agent_id,
            tool_name=record.tool_name,
            action=record.action,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            arguments=arguments,
        )
        if not hmac.compare_digest(current, record.fingerprint):
            return self._fail(record, "The operation changed after approval.", "revalidation_failed")
        if not stored_path_matches(record, arguments):
            return self._fail(record, "The operation changed after approval.", "revalidation_failed")
        sealed_path = arguments.get("relative_path")
        decision = evaluate_request(
            agent_id=record.agent_id,
            tool_name=record.tool_name,
            action=record.action,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            relative_path=sealed_path if isinstance(sealed_path, str) else None,
            registry=self.registry,
        )
        if decision.decision == DENY or decision.decision not in {ALLOW, REQUIRES_APPROVAL}:
            return self._fail(record, decision.reason, "revalidation_failed")
        claimed = self.session.execute(
            update(ApprovalRequestRecord)
            .where(ApprovalRequestRecord.id == request_id, ApprovalRequestRecord.status == APPROVED)
            .values(status=EXECUTING, execution_status=EXECUTING)
        )
        if claimed.rowcount != 1:
            self.session.rollback()
            self.session.expire_all()
            current_row = self.session.get(ApprovalRequestRecord, request_id)
            raise ApprovalConflict(current_row.status if current_row is not None else FAILED)
        agent_id = record.agent_id
        tool_name = record.tool_name
        reason = record.reason
        action = record.action
        self.session.expire(record)
        add_audit(
            self.session,
            event="approval_executing",
            decision=EXECUTING,
            detail=reason,
            agent_id=agent_id,
            tool_name=tool_name,
            resource_id=request_id,
            action=action,
        )
        self.session.commit()
        self.session.expire_all()
        record = self.session.get(ApprovalRequestRecord, request_id)
        tool = self.registry.get(record.tool_name)
        if tool is None:
            record = self.session.get(ApprovalRequestRecord, request_id)
            return self._fail(record, "That tool is not available.", "approval_failed")
        confirmed = bool(tool.requires_confirmation or tool.execution_policy == SIDE_EFFECT)
        approval_id = record.id
        tracked_tool = record.tool_name
        tracked_agent = record.agent_id
        tracked_action = record.action
        try:
            outcome = await self.executor.run(
                tool,
                arguments,
                confirmed=confirmed,
                agent_id=record.agent_id,
                approval_id=record.id,
            )
        except Exception:
            if tracked_tool in _FILE_MUTATIONS:
                from types import SimpleNamespace

                return self._finish_file_approval(
                    approval_id,
                    tracked_tool,
                    tracked_agent,
                    tracked_action,
                    SimpleNamespace(status="error", output=None, error="The tool could not be completed."),
                )
            record = self.session.get(ApprovalRequestRecord, request_id)
            return self._fail(record, "The tool could not be completed.", "approval_failed")
        if tracked_tool in _FILE_MUTATIONS:
            return self._finish_file_approval(
                approval_id,
                tracked_tool,
                tracked_agent,
                tracked_action,
                outcome,
            )
        record = self.session.get(ApprovalRequestRecord, request_id)
        if outcome.status == "ok":
            record.status = COMPLETED
            record.execution_status = "ok"
            record.result_text = _stored_result(record.tool_name, outcome.status, outcome.output)
            self.last_output = outcome.output
            event = "approval_completed"
        else:
            record.status = FAILED
            record.execution_status = outcome.status
            record.result_text = (outcome.error or "The tool could not be completed.")[:400]
            event = "approval_failed"
        add_audit(
            self.session,
            event=event,
            decision=record.status,
            detail=record.result_text or record.status,
            agent_id=record.agent_id,
            tool_name=record.tool_name,
            resource_id=record.id,
            action=record.action,
        )
        self.session.commit()
        _discard_proposal(record.id)
        return record

    def _finish_file_approval(self, approval_id: str, tool_name: str, agent_id: str | None, action: str, outcome) -> ApprovalRequestRecord:
        """Apply a file worker result only when the journal agrees it finished."""
        from app.approvals.reconcile import journal_state_for, reconcile_linked_approvals, record_worker_failure
        from app.files.journal import COMMITTED, reconcile_file_journal

        self.session.rollback()
        reconcile_file_journal()
        state = journal_state_for(approval_id)
        if state == COMMITTED and outcome.status == "ok":
            completed = self._complete_if_executing(approval_id, tool_name, agent_id, action, outcome)
            if not completed:
                reconcile_linked_approvals("execution")
                fresh = self.session.get(ApprovalRequestRecord, approval_id)
                if fresh is not None and fresh.status == COMPLETED:
                    self.last_output = outcome.output
            _discard_proposal(approval_id)
            return self.session.get(ApprovalRequestRecord, approval_id)
        if state == COMMITTED:
            record_worker_failure(approval_id, outcome.error or "The tool could not be completed.")
        else:
            message = outcome.error or "The file change was not committed."
            if outcome.status == "ok":
                message = "The file change was not committed."
            record_worker_failure(approval_id, message)
        reconcile_linked_approvals("execution")
        _discard_proposal(approval_id)
        return self.session.get(ApprovalRequestRecord, approval_id)

    def _complete_if_executing(self, approval_id: str, tool_name: str, agent_id: str | None, action: str, outcome) -> bool:
        claimed = self.session.execute(
            update(ApprovalRequestRecord)
            .where(
                ApprovalRequestRecord.id == approval_id,
                ApprovalRequestRecord.status == EXECUTING,
            )
            .values(
                status=COMPLETED,
                execution_status="ok",
                result_text=_stored_result(tool_name, outcome.status, outcome.output),
            )
        )
        if claimed.rowcount != 1:
            self.session.rollback()
            return False
        add_audit(
            self.session,
            event="approval_completed",
            decision=COMPLETED,
            detail=_stored_result(tool_name, outcome.status, outcome.output),
            agent_id=agent_id,
            tool_name=tool_name,
            resource_id=approval_id,
            action=action,
        )
        self.session.commit()
        self.last_output = outcome.output
        return True

    async def _execute_mcp(self, request_id: str) -> ApprovalRequestRecord:
        """Move one MCP approval from PENDING to EXECUTING, then dispatch once.

        Checks that do not decrypt fail the pending row as not_dispatched.
        The gateway is called only after that EXECUTING commit returns.
        """
        from app.mcp.gateway import DispatchOutcome, SealedDispatch, dispatch
        from app.permissions.service import evaluate_mcp_access

        self.session.expire_all()
        record = self.get(request_id)
        if record.status != PENDING:
            raise ApprovalConflict(record.status)
        if not build_review(record)["verified"]:
            raise ApprovalNotReviewable()
        payload = self.session.get(McpExecutionPayloadRecord, request_id)
        if payload is None:
            return self._finish_mcp_pending(record, "not_dispatched", NOT_SENT)
        try:
            arguments = json.loads(record.arguments_json)
        except json.JSONDecodeError:
            return self._finish_mcp_pending(record, "not_dispatched", NOT_SENT)
        if not isinstance(arguments, dict):
            return self._finish_mcp_pending(record, "not_dispatched", NOT_SENT)
        current = operation_fingerprint(
            agent_id=record.agent_id,
            tool_name=record.tool_name,
            action=record.action,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            arguments=arguments,
        )
        if not hmac.compare_digest(current, record.fingerprint):
            return self._finish_mcp_pending(record, "not_dispatched", NOT_SENT)
        access = evaluate_mcp_access(
            agent_id=record.agent_id,
            tool_name=record.tool_name,
            arguments=_mcp_permission_arguments(record.tool_name, arguments),
            registry=self.registry,
        )
        if not access.permitted or access.kind != "approval":
            return self._finish_mcp_pending(record, "not_dispatched", NOT_SENT)
        nonce = bytes(payload.nonce)
        ciphertext = bytes(payload.ciphertext)
        sealed = SealedDispatch(
            approval_id=record.id,
            agent_id=record.agent_id,
            tool_name=record.tool_name,
            fingerprint=record.fingerprint,
            arguments=arguments,
            nonce=nonce,
            ciphertext=ciphertext,
        )
        self._claim(request_id, PENDING, EXECUTING, "approval_executing")
        try:
            outcome = await asyncio.to_thread(dispatch, sealed)
        except Exception:
            outcome = DispatchOutcome("unknown", UNKNOWN_RESULT)
        return self._finish_mcp_executing(request_id, outcome)

    def _finish_mcp_pending(self, record: ApprovalRequestRecord, classification: str, message: str) -> ApprovalRequestRecord:
        record.status = FAILED
        record.execution_status = classification
        record.result_text = message[:400]
        record.decided_at = datetime.now(timezone.utc)
        delete_payload(self.session, record.id)
        self._audit(record, "approval_failed", FAILED, message)
        self.session.commit()
        return record

    def _finish_mcp_executing(self, request_id: str, outcome) -> ApprovalRequestRecord:
        self.session.expire_all()
        record = self.session.get(ApprovalRequestRecord, request_id)
        if record is None:
            raise ApprovalNotFound()
        if outcome.classification == "completed":
            status = COMPLETED
            execution_status = "ok"
            message = outcome.public_result
            event = "approval_completed"
        elif outcome.classification == "remote_failed":
            status = FAILED
            execution_status = "remote_failed"
            message = outcome.public_result
            event = "approval_failed"
        elif outcome.classification == "unknown":
            status = FAILED
            execution_status = "unknown"
            message = UNKNOWN_RESULT
            event = "approval_failed"
        else:
            status = FAILED
            execution_status = "not_dispatched"
            message = NOT_SENT
            event = "approval_failed"
        claimed = self.session.execute(
            update(ApprovalRequestRecord)
            .where(ApprovalRequestRecord.id == request_id, ApprovalRequestRecord.status == EXECUTING)
            .values(status=status, execution_status=execution_status, result_text=message[:400])
        )
        if claimed.rowcount != 1:
            self.session.rollback()
            current = self.session.get(ApprovalRequestRecord, request_id)
            raise ApprovalConflict(current.status if current is not None else FAILED)
        delete_payload(self.session, request_id)
        self.session.expire(record)
        record = self.session.get(ApprovalRequestRecord, request_id)
        self._audit(record, event, status, message)
        self.session.commit()
        self.last_output = {"status": execution_status}
        return record

    def _claim(self, request_id: str, source: str, target: str, event: str) -> ApprovalRequestRecord:
        self._expire_due()
        record = self.session.get(ApprovalRequestRecord, request_id)
        if record is None:
            raise ApprovalNotFound()
        if record.status == EXPIRED:
            raise ApprovalConflict(EXPIRED)
        now = datetime.now(timezone.utc)
        if record.status == PENDING and _as_utc(record.expires_at) <= now:
            record.status = EXPIRED
            record.decided_at = now
            self._audit(record, "approval_expired", EXPIRED, "The approval request expired.")
            self.session.commit()
            raise ApprovalConflict(EXPIRED)
        claimed = self.session.execute(
            update(ApprovalRequestRecord)
            .where(
                ApprovalRequestRecord.id == request_id,
                ApprovalRequestRecord.status == source,
                ApprovalRequestRecord.expires_at > now.replace(tzinfo=None),
            )
            .values(status=target, decided_at=now, execution_status=target)
        )
        if claimed.rowcount != 1:
            self.session.rollback()
            current = self.session.get(ApprovalRequestRecord, request_id)
            raise ApprovalConflict(current.status if current is not None else FAILED)
        self._audit_id(request_id, event, target)
        if target in {REJECTED, CANCELLED, EXPIRED}:
            _discard_proposal(request_id)
            delete_payload(self.session, request_id)
        self.session.commit()
        self.session.expire_all()
        return self.session.get(ApprovalRequestRecord, request_id)

    def _fail(self, record: ApprovalRequestRecord, message: str, event: str) -> ApprovalRequestRecord:
        record.status = FAILED
        record.execution_status = FAILED
        record.result_text = message[:400]
        self._audit(record, event, FAILED, message)
        self.session.commit()
        _discard_proposal(record.id)
        return record

    def _expire_due(self) -> None:
        now = datetime.now(timezone.utc)
        due = list(
            self.session.scalars(
                select(ApprovalRequestRecord).where(
                    ApprovalRequestRecord.status == PENDING,
                    ApprovalRequestRecord.expires_at <= now.replace(tzinfo=None),
                )
            ).all()
        )
        if not due:
            return
        for record in due:
            record.status = EXPIRED
            record.decided_at = now
            record.execution_status = EXPIRED
            self._audit(record, "approval_expired", EXPIRED, "The approval request expired.")
            _discard_proposal(record.id)
            delete_payload(self.session, record.id)
        self.session.commit()

    def _audit_id(self, request_id: str, event: str, decision: str) -> None:
        record = self.session.get(ApprovalRequestRecord, request_id)
        if record is None:
            return
        record.status = decision if decision in {APPROVED, REJECTED, CANCELLED, EXPIRED} else record.status
        self._audit(record, event, decision, record.reason)

    def _audit(self, record: ApprovalRequestRecord, event: str, decision: str, detail: str) -> None:
        add_audit(
            self.session,
            event=event,
            decision=decision,
            detail=detail,
            agent_id=record.agent_id,
            tool_name=record.tool_name,
            resource_type=record.resource_type,
            resource_id=record.id,
            action=record.action,
        )


def _as_utc(value: datetime) -> datetime:
    """SQLite returns these timestamps without an offset. Treat that as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def execution_matches(approval_id: str | None, *, agent_id: str | None, tool_name: str | None, action: str, arguments: dict) -> bool:
    """True only when the parent has already moved this exact operation to EXECUTING."""
    if not isinstance(approval_id, str) or not approval_id:
        return False
    from app.core.database import get_session_factory

    session = get_session_factory()()
    try:
        record = session.get(ApprovalRequestRecord, approval_id)
        if record is None or record.status != EXECUTING:
            return False
        if record.agent_id != agent_id or record.tool_name != tool_name or record.action != action:
            return False
        arguments_match = operation_fingerprint(
            agent_id=record.agent_id,
            tool_name=record.tool_name,
            action=record.action,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            arguments=arguments,
        )
        return hmac.compare_digest(arguments_match, record.fingerprint)
    finally:
        session.close()


def cancel_pending(session: Session, **filters) -> None:
    ApprovalService(session, _empty_registry()).cancel_pending(**filters)


class _EmptyRegistry:
    def get(self, _name: str):
        return None

    def list_tools(self):
        return []


def _mcp_permission_arguments(tool_name: str, arguments: dict) -> dict:
    if tool_name == MCP_CALL_TOOL:
        return {
            "server_id": arguments.get("server_id"),
            "remote_tool_name": arguments.get("remote_tool_name"),
        }
    return {"server_id": arguments.get("server_id"), "resource_uri": arguments.get("resource_uri")}


_before_pin = None
_after_pin = None
_commit_fault = None


def set_before_pin(callback) -> None:
    """Test hook. It runs after the seal returns and before the discovery pin."""
    global _before_pin
    _before_pin = callback


def set_after_pin(callback) -> None:
    """Test hook. It runs while the discovery pin holds the send lock."""
    global _after_pin
    _after_pin = callback


def set_commit_fault(callback) -> None:
    """Test hook. It runs while the pin is held and before the approval commit."""
    global _commit_fault
    _commit_fault = callback


def _seal_mcp_package(
    *,
    approval_id: str,
    agent_id: str,
    tool_name: str,
    arguments: dict,
    expires_at,
):
    """Return a gateway seal. This function does not touch an approval session."""
    from app.mcp.gateway import seal_call, seal_read

    server_id = arguments.get("server_id")
    if not isinstance(server_id, str):
        raise ApprovalNotReviewable()
    if tool_name == MCP_CALL_TOOL:
        return seal_call(
            approval_id=approval_id,
            agent_id=agent_id,
            expires_at=expires_at,
            server_id=server_id,
            remote_tool_name=arguments.get("remote_tool_name"),
            tool_arguments=arguments.get("tool_arguments"),
        )
    if tool_name == MCP_READ_TOOL:
        return seal_read(
            approval_id=approval_id,
            agent_id=agent_id,
            expires_at=expires_at,
            server_id=server_id,
            resource_uri=arguments.get("resource_uri"),
        )
    raise ApprovalNotReviewable()


def _register_mcp_listeners() -> None:
    from app.approvals.mcp_policy import invalidate_discovery, invalidate_epoch, invalidate_generation
    from app.mcp.session import set_after_epoch, set_discovery_listener, set_generation_listener

    set_discovery_listener(invalidate_discovery)
    set_generation_listener(invalidate_generation)
    set_after_epoch(invalidate_epoch)


_register_mcp_listeners()


def _empty_registry() -> ToolRegistry:
    return _EmptyRegistry()  # type: ignore[return-value]


def _description(tool_description: str, spec: dict) -> str:
    resource = spec["resource_type"]
    if spec.get("resource_id"):
        resource = f"{resource} {spec['resource_id']}"
    if spec.get("relative_path"):
        resource = f"{resource} {spec['relative_path']}"
    text = f"{tool_description} Action {spec['action']} on {resource}."
    return " ".join(text.split())[:300]


def _stored_result(tool_name: str, status: str, output: dict | None) -> str:
    if tool_name in _FILE_TOOLS:
        return json.dumps({"status": status})
    if not output:
        return json.dumps({"status": status})
    return json.dumps({"status": status, "output": output}, default=str)[:400]


def to_public(record: ApprovalRequestRecord) -> dict:
    public = {
        "id": record.id,
        "agent_id": record.agent_id,
        "tool_id": record.tool_name,
        "tool_name": record.tool_name,
        "resource_type": record.resource_type,
        "resource_id": record.resource_id,
        "action": record.action,
        "description": record.description,
        "arguments": _public_arguments(record),
        "change": _change_preview(record),
        "risk": record.risk,
        "reason": record.reason,
        "mode": record.mode,
        "status": record.status,
        "created_at": record.created_at,
        "expires_at": record.expires_at,
        "decided_at": record.decided_at,
        "execution_status": record.execution_status,
        "result": record.result_text,
        "review": build_review(record),
    }
    if record.execution_status == "unknown":
        public["outcome"] = "unknown"
        public["result"] = UNKNOWN_RESULT
    return public


def _discard_proposal(request_id: str) -> None:
    from app.files.changes import discard_proposal

    discard_proposal(request_id)


def _public_arguments(record: ApprovalRequestRecord) -> dict | None:
    try:
        parsed = json.loads(record.arguments_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return present_arguments(parsed)


def _change_preview(record: ApprovalRequestRecord) -> dict | None:
    from app.files.changes import describe_change

    try:
        arguments = json.loads(record.arguments_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, dict):
        return None
    return describe_change(arguments, record.id)
