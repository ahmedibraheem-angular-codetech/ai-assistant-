"""Approval requests pause execution until the local user decides.

These tests do not call a model or a network service.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.routes.permissions import require_local_admin
from app.approvals.models import ApprovalRequestRecord
from app.approvals.service import ApprovalConflict, ApprovalService
from app.core.database import get_engine
from app.permissions.manifests import ToolManifest
from app.tools.base import ASYNC_READ_ONLY, Tool
from app.tools.registry import get_tool_registry
from tests.test_agents import create_agent
from tests.test_tools import grant, scripted_chat, tool_call


class ReadableProbe(Tool):
    name = "readable_probe"
    description = "Read one integer that the user already approved."
    category = "test"
    requires_confirmation = False
    execution_policy = ASYNC_READ_ONLY
    enabled = True
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {"value": {"type": "integer", "minimum": 0, "maximum": 10}},
    }
    permission_manifest = ToolManifest(
        tool_id="readable_probe",
        name="readable_probe",
        description="Read one integer that the user already approved.",
        resource_types=("none",),
        actions=("read",),
        risk="read",
        approval_required=True,
        read_only=True,
        execution_policy=ASYNC_READ_ONLY,
    )

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, arguments: dict) -> dict:
        self.calls += 1
        return {"value": arguments["value"]}


@pytest.fixture
def probe(client):
    tool = ReadableProbe()
    get_tool_registry().register(tool)
    client.put("/api/v1/permissions/mode", json={"mode": "always_ask"})
    try:
        yield tool
    finally:
        get_tool_registry().unregister(tool.name)


def _ask(client, agent_id: str, value: int = 2):
    response = client.post(
        "/api/v1/tools/readable_probe/test",
        json={"arguments": {"value": value}, "agent_id": agent_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmation_required"
    return body["output"]["approval_id"]


def test_pending_request_does_not_run_until_approved(client, probe):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["readable_probe"])
    approval_id = _ask(client, agent["id"])
    assert probe.calls == 0
    listed = client.get("/api/v1/approvals")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    detail = client.get(f"/api/v1/approvals/{approval_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "PENDING"
    assert detail.json()["tool_name"] == "readable_probe"
    assert detail.json()["action"] == "read"
    approved = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "COMPLETED"
    assert approved.json()["output"]["value"] == 2
    assert probe.calls == 1
    again = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert again.status_code == 409
    assert probe.calls == 1
    history = client.get("/api/v1/approvals/history")
    assert history.status_code == 200
    assert history.json()["approvals"][0]["id"] == approval_id


def test_reject_cancel_and_expiry_never_execute(client, probe):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["readable_probe"])
    rejected_id = _ask(client, agent["id"], 1)
    rejected = client.post(f"/api/v1/approvals/{rejected_id}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert client.post(f"/api/v1/approvals/{rejected_id}/approve").status_code == 409
    cancelled_id = _ask(client, agent["id"], 3)
    cancelled = client.post(f"/api/v1/approvals/{cancelled_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    expired_id = _ask(client, agent["id"], 4)
    with Session(get_engine()) as session:
        row = session.get(ApprovalRequestRecord, expired_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        session.commit()
    expired = client.post(f"/api/v1/approvals/{expired_id}/approve")
    assert expired.status_code == 409
    assert expired.json()["detail"]["status"] == "EXPIRED"
    assert probe.calls == 0
    missing = client.get("/api/v1/approvals/does-not-exist")
    assert missing.status_code == 404
    history = client.get("/api/v1/approvals/history")
    statuses = {item["id"]: item["status"] for item in history.json()["approvals"]}
    assert statuses[rejected_id] == "REJECTED"
    assert statuses[cancelled_id] == "CANCELLED"
    assert statuses[expired_id] == "EXPIRED"


def test_same_operation_reuses_one_pending_request(client, probe):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["readable_probe"])
    first = _ask(client, agent["id"], 2)
    second = _ask(client, agent["id"], 2)
    assert first == second
    assert client.get("/api/v1/approvals").json()["count"] == 1


def test_model_cannot_approve_or_forge_a_flag(client, probe, monkeypatch):
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    grant(client, agent["id"], ["readable_probe"])
    approval_id = _ask(client, agent["id"], 2)

    def respond(item, calls):
        if len(calls) == 1:
            return tool_call(
                "readable_probe",
                {"value": 2, "approved": True, "approval_id": approval_id},
            )
        return "Still waiting."

    scripted_chat(monkeypatch, respond)
    response = client.post("/api/v1/chat", json={"message": "Approve it.", "agent_id": agent["id"]})
    assert response.status_code == 200
    assert probe.calls == 0
    current = client.get(f"/api/v1/approvals/{approval_id}")
    assert current.json()["status"] == "PENDING"
    forged = client.post(
        "/api/v1/tools/readable_probe/test",
        json={"arguments": {"value": 2, "approved": True}, "agent_id": agent["id"]},
    )
    assert forged.status_code == 422
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/approvals",
        "raw_path": b"/api/v1/approvals",
        "query_string": b"",
        "headers": [],
        "client": ("8.8.8.8", 443),
        "server": ("127.0.0.1", 8000),
    }
    with pytest.raises(HTTPException) as caught:
        require_local_admin(Request(scope))
    assert caught.value.status_code == 403


def test_changed_operation_or_revoked_grant_blocks_execution(client, probe):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["readable_probe"])
    changed_id = _ask(client, agent["id"], 2)
    with Session(get_engine()) as session:
        row = session.get(ApprovalRequestRecord, changed_id)
        row.resource_id = "other-resource"
        session.commit()
    changed = client.post(f"/api/v1/approvals/{changed_id}/approve")
    assert changed.status_code == 200
    assert changed.json()["status"] == "FAILED"
    tampered_id = _ask(client, agent["id"], 6)
    with Session(get_engine()) as session:
        row = session.get(ApprovalRequestRecord, tampered_id)
        row.arguments_json = '{"value": 9}'
        session.commit()
    tampered = client.post(f"/api/v1/approvals/{tampered_id}/approve")
    assert tampered.status_code == 200
    assert tampered.json()["status"] == "FAILED"
    assert probe.calls == 0
    revoked_id = _ask(client, agent["id"], 5)
    client.put(f"/api/v1/agents/{agent['id']}/tools", json={"allowed_tools": []})
    revoked = client.post(f"/api/v1/approvals/{revoked_id}/approve")
    assert revoked.status_code == 409
    assert probe.calls == 0


def test_restart_leaves_pending_work_unrun(client, probe):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["readable_probe"])
    approval_id = _ask(client, agent["id"], 2)
    with Session(get_engine()) as session:
        record = session.get(ApprovalRequestRecord, approval_id)
        assert record.status == "PENDING"
    assert probe.calls == 0
    listed = client.get("/api/v1/approvals")
    assert listed.json()["count"] == 1


@pytest.mark.asyncio
async def test_one_approval_executes_once_under_concurrency(client, probe):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["readable_probe"])
    approval_id = _ask(client, agent["id"], 2)

    async def decide():
        session = Session(get_engine())
        try:
            service = ApprovalService(session, get_tool_registry())
            try:
                record = await service.approve(approval_id)
                return "ran", record.status
            except ApprovalConflict as exc:
                return "blocked", exc.status
        finally:
            session.close()

    results = await asyncio.gather(decide(), decide())
    assert [item[0] for item in results].count("ran") == 1
    assert probe.calls == 1


def test_executing_request_cannot_be_cancelled(client, probe):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["readable_probe"])
    approval_id = _ask(client, agent["id"], 2)
    with Session(get_engine()) as session:
        row = session.get(ApprovalRequestRecord, approval_id)
        row.status = "EXECUTING"
        session.commit()
    cancelled = client.post(f"/api/v1/approvals/{approval_id}/cancel")
    assert cancelled.status_code == 409
    assert probe.calls == 0
    stored = client.get(f"/api/v1/approvals/{approval_id}")
    assert "2" not in json.dumps(stored.json().get("result"))
