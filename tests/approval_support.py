"""Helpers for tool tests after approval_required became enforceable."""

from __future__ import annotations

import json

from app.approvals.models import ApprovalRequestRecord
from app.core.database import get_engine
from sqlalchemy.orm import Session

EXECUTING = "EXECUTING"


class _NormalizedResponse:
    """Minimal TestClient-like response with tool-test status shape."""

    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def finish_tool_test(client, response):
    """Approve a confirmation_required tool-test response and return execution result.

    Non-approval responses are returned unchanged. Approval API bodies are
    normalized to the tool-test shape (``ok`` / ``error``) so existing callers
    keep working.
    """
    if response.status_code != 200:
        return response
    body = response.json()
    if body.get("status") != "confirmation_required":
        return response
    output = body.get("output")
    if not isinstance(output, dict):
        return response
    approval_id = output.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return response
    approved = client.post(f"/api/v1/approvals/{approval_id}/approve")
    if approved.status_code != 200:
        return approved
    result = approved.json()
    status = result.get("status")
    if status == "COMPLETED":
        return _NormalizedResponse(
            200,
            {"status": "ok", "output": result.get("output"), "error": None},
        )
    if status == "FAILED":
        return _NormalizedResponse(
            200,
            {
                "status": "error",
                "output": result.get("output"),
                "error": result.get("result") or "The tool could not be completed.",
            },
        )
    return approved


def prime_executing_approval(client, *, agent_id: str, tool_name: str, arguments: dict) -> str:
    """Create a pending approval via the tool-test API, then mark it EXECUTING.

    Direct ``ToolExecutor.run`` paths bind this id so ``authorize()`` accepts a
    read that still declares ``approval_required`` under ask_sensitive.
    """
    response = client.post(
        f"/api/v1/tools/{tool_name}/test",
        json={"arguments": arguments, "agent_id": agent_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmation_required", body
    approval_id = body["output"]["approval_id"]
    with Session(get_engine()) as session:
        row = session.get(ApprovalRequestRecord, approval_id)
        assert row is not None
        row.status = EXECUTING
        row.execution_status = EXECUTING
        session.commit()
    return approval_id
