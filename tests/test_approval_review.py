"""The approval review is a read-only view of the sealed record."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.approvals.fingerprint import bind_arguments, operation_fingerprint
from app.approvals.models import ApprovalRequestRecord
from app.approvals.review import MCP_TOOLS, build_review, format_expiry
from app.core.database import get_engine
from app.permissions.manifests import ToolManifest
from app.permissions.service import evaluate_request
from app.tools.base import ASYNC_READ_ONLY, Tool
from app.tools.registry import get_tool_registry
from app.tools.worker import ALLOWED_TOOLS
from tests.test_agents import create_agent
from tests.test_approvals import ReadableProbe
from tests.test_files import grant_workspace, register
from tests.test_tools import grant


@pytest.fixture
def probe(client):
    tool = ReadableProbe()
    get_tool_registry().register(tool)
    client.put("/api/v1/permissions/mode", json={"mode": "always_ask"})
    try:
        yield tool
    finally:
        get_tool_registry().unregister(tool.name)


def _seal(**arguments) -> dict:
    return bind_arguments(arguments)


def _insert(
    tool_name: str,
    action: str,
    arguments: dict,
    *,
    fingerprint: str | None = None,
    agent_id: str = "agent-1",
    resource_type: str = "none",
    resource_id: str | None = None,
    relative_path: str | None = None,
) -> str:
    record_id = str(uuid4())
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=10)
    stored = dict(arguments)
    if tool_name in MCP_TOOLS:
        stored["approval_id"] = record_id
        stored["expires_at"] = format_expiry(expires)
        stored.setdefault("process_generation", 1)
        stored.setdefault("discovery_generation", 1)
    digest = fingerprint or operation_fingerprint(
        agent_id=agent_id,
        tool_name=tool_name,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        arguments=stored,
    )
    record = ApprovalRequestRecord(
        id=record_id,
        agent_id=agent_id,
        tool_name=tool_name,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        relative_path=relative_path,
        arguments_json=json.dumps(stored, sort_keys=True),
        fingerprint=digest,
        description="Review the sealed operation.",
        risk="sensitive",
        reason="review",
        mode="ask_sensitive",
        status="PENDING",
        created_at=now,
        expires_at=expires,
        decided_at=None,
        execution_status=None,
        result_text=None,
    )
    with Session(get_engine()) as session:
        session.add(record)
        session.commit()
    return record_id


def _arguments_digest(arguments: dict) -> str:
    payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mcp_arguments() -> dict:
    sealed = _seal(
        server_id="server-1",
        server_display_name="Local notes",
        config_revision="a" * 64,
        epoch=3,
        executable_device=1,
        executable_inode=2,
        executable_sha256="b" * 64,
        env_sha256="c" * 64,
        remote_tool_name="lookup",
        input_schema_sha256="d" * 64,
        arguments_sha256="0" * 64,
        tool_arguments={"query": "price", "api_key": "super-secret-value"},
        environment={"TOKEN": "env-secret-value"},
    )
    sealed["arguments_sha256"] = _arguments_digest(sealed["tool_arguments"])
    return sealed


def test_pending_probe_review_uses_the_sealed_record(client, probe):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["readable_probe"])
    proposed = client.post(
        "/api/v1/tools/readable_probe/test",
        json={"arguments": {"value": 2}, "agent_id": agent["id"]},
    )
    approval_id = proposed.json()["output"]["approval_id"]
    detail = client.get(f"/api/v1/approvals/{approval_id}")
    review = detail.json()["review"]
    assert review["reviewable"] is True
    assert review["verified"] is True
    assert review["mcp_execution_available"] is False
    assert review["approval_id"] == approval_id
    assert review["status"] == "PENDING"
    assert review["operation"] == "read"
    assert review["arguments"] == {"value": 2}
    assert review["arguments_text"] == '{"value":2}'
    assert review["mcp"]["applies"] is False
    assert review["mcp"]["live_verification"] == "not_applicable"
    assert review["mcp"]["fields"]["server_id"] == {"status": "not_applicable", "value": None}
    assert review["mcp"]["fields"]["remote_tool_name"]["status"] == "not_applicable"
    again = client.get(f"/api/v1/approvals/{approval_id}")
    assert again.json()["review"] == review
    assert probe.calls == 0


def test_mcp_review_shows_the_seal_and_hides_secrets(client):
    approval_id = _insert("call_mcp_tool", "execute", _mcp_arguments())
    first = client.get(f"/api/v1/approvals/{approval_id}")
    second = client.get(f"/api/v1/approvals/{approval_id}")
    assert first.status_code == 200
    assert first.json()["review"] == second.json()["review"]
    review = first.json()["review"]
    assert review["reviewable"] is True
    assert review["verified"] is True
    assert review["mcp_execution_available"] is False
    assert review["mcp"]["live_verification"] == "not_performed"
    with Session(get_engine()) as session:
        stored_arguments = json.loads(session.get(ApprovalRequestRecord, approval_id).arguments_json)
    assert review["fingerprint"] == operation_fingerprint(
        agent_id="agent-1",
        tool_name="call_mcp_tool",
        action="execute",
        resource_type="none",
        resource_id=None,
        arguments=stored_arguments,
    )
    fields = review["mcp"]["fields"]
    assert fields["server_id"] == {"status": "sealed", "value": "server-1"}
    assert fields["server_display_name"]["value"] == "Local notes"
    assert fields["config_revision"]["value"] == "a" * 64
    assert fields["executable_sha256"]["value"] == "b" * 64
    assert fields["remote_tool_name"]["value"] == "lookup"
    assert fields["input_schema_sha256"]["status"] == "sealed"
    assert fields["env_sha256"]["value"] == "c" * 64
    assert review["arguments"]["tool_arguments"]["query"] == "price"
    assert review["arguments"]["tool_arguments"]["api_key"] == "***"
    assert review["arguments"]["environment"] == "***"
    assert first.json()["arguments"] == review["arguments"]
    body = first.text
    assert "super-secret-value" not in body
    assert "env-secret-value" not in body
    assert any(item["field"] == "api_key" for item in review["hidden"])
    assert "Hidden fields remain part of the integrity seal." in review["hidden"][0]["reason"]
    with Session(get_engine()) as session:
        stored = session.get(ApprovalRequestRecord, approval_id)
        before = (stored.arguments_json, stored.fingerprint, stored.status)
    client.get(f"/api/v1/approvals/{approval_id}")
    with Session(get_engine()) as session:
        stored = session.get(ApprovalRequestRecord, approval_id)
        assert (stored.arguments_json, stored.fingerprint, stored.status) == before


def test_incomplete_or_modified_mcp_seal_fails_closed(client):
    incomplete = _mcp_arguments()
    del incomplete["config_revision"]
    incomplete_id = _insert("call_mcp_tool", "execute", incomplete)
    missing = client.get(f"/api/v1/approvals/{incomplete_id}").json()["review"]
    assert missing["verified"] is False
    assert missing["reviewable"] is False
    assert missing["mcp"]["fields"]["config_revision"] == {"status": "unavailable", "value": None}
    assert any("Configuration revision" in warning for warning in missing["warnings"])
    refused = client.post(f"/api/v1/approvals/{incomplete_id}/approve")
    assert refused.status_code == 409
    assert refused.json()["detail"]["status"] == "NOT_REVIEWABLE"
    with Session(get_engine()) as session:
        assert session.get(ApprovalRequestRecord, incomplete_id).status == "PENDING"

    complete_id = _insert("call_mcp_tool", "execute", _mcp_arguments())
    with Session(get_engine()) as session:
        row = session.get(ApprovalRequestRecord, complete_id)
        body = json.loads(row.arguments_json)
        body["input_schema_sha256"] = "f" * 64
        body["executable_sha256"] = "9" * 64
        body["tool_arguments"] = {"query": "changed"}
        row.arguments_json = json.dumps(body, sort_keys=True)
        session.commit()
    changed = client.get(f"/api/v1/approvals/{complete_id}").json()["review"]
    assert changed["verified"] is False
    assert changed["reviewable"] is False
    assert changed["mcp"]["fields"]["input_schema_sha256"]["value"] == "f" * 64
    assert changed["mcp"]["fields"]["executable_sha256"]["value"] == "9" * 64
    blocked = client.post(f"/api/v1/approvals/{complete_id}/approve")
    assert blocked.status_code == 409
    with Session(get_engine()) as session:
        assert session.get(ApprovalRequestRecord, complete_id).status == "PENDING"


def test_review_cannot_execute_mcp_tools(client):
    approval_id = _insert("read_mcp_resource", "execute", _seal(
        server_id="server-1",
        server_display_name="Local notes",
        config_revision="a" * 64,
        epoch=3,
        executable_device=1,
        executable_inode=2,
        executable_sha256="b" * 64,
        env_sha256="c" * 64,
        resource_uri="notes://price",
    ))
    review = client.get(f"/api/v1/approvals/{approval_id}").json()["review"]
    assert review["reviewable"] is True
    assert review["mcp"]["fields"]["resource_uri"]["value"] == "notes://price"
    decision = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert decision.status_code == 200
    assert decision.json()["status"] == "FAILED"
    assert decision.json()["execution_status"] == "not_dispatched"
    assert get_tool_registry().get("call_mcp_tool") is not None
    assert get_tool_registry().get("read_mcp_resource") is not None
    assert "call_mcp_tool" not in ALLOWED_TOOLS
    assert "read_mcp_resource" not in ALLOWED_TOOLS
    with Session(get_engine()) as session:
        stored = session.get(ApprovalRequestRecord, approval_id)
        assert build_review(stored)["arguments_text"] == review["arguments_text"]


def test_invalid_mcp_representation_is_not_verified(client):
    malformed = _mcp_arguments()
    malformed["epoch"] = True
    malformed["executable_sha256"] = "not-a-hash"
    malformed["executable_device"] = -1
    approval_id = _insert("call_mcp_tool", "execute", malformed)
    review = client.get(f"/api/v1/approvals/{approval_id}").json()["review"]
    assert review["verified"] is False
    assert review["reviewable"] is False
    assert review["mcp_execution_available"] is False
    assert review["mcp"]["live_verification"] == "not_performed"
    assert review["mcp"]["fields"]["epoch"]["status"] == "invalid"
    assert review["mcp"]["fields"]["executable_sha256"] == {"status": "invalid", "value": None}
    assert review["mcp"]["fields"]["executable_device"]["status"] == "invalid"
    assert any("invalid" in warning for warning in review["warnings"])
    refused = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert refused.status_code == 409
    assert refused.json()["detail"]["status"] == "NOT_REVIEWABLE"

    original = _mcp_arguments()
    original_id = _insert("call_mcp_tool", "execute", original)
    with Session(get_engine()) as session:
        original_row = session.get(ApprovalRequestRecord, original_id)
        original_fingerprint = original_row.fingerprint
        original_row.status = "CANCELLED"
        session.commit()
    inconsistent = _mcp_arguments()
    inconsistent["arguments_sha256"] = "ab" * 32
    inconsistent_id = _insert("call_mcp_tool", "execute", inconsistent, fingerprint=original_fingerprint)
    drifted = client.get(f"/api/v1/approvals/{inconsistent_id}").json()["review"]
    assert drifted["verified"] is False
    assert drifted["reviewable"] is False
    assert drifted["mcp"]["fields"]["arguments_sha256"]["status"] == "sealed"
    assert any("fingerprint" in warning for warning in drifted["warnings"])


_SECRET_VALUES = (
    "raw-private-key-value",
    "visible-env-path-secret",
    "nested-password-value",
    "nested-api-token-value",
    "nested-private-key-value",
    "top-api-key-value",
    "top-password-value",
    "list-password-value",
)


def test_public_approval_responses_redact_every_secret(client):
    arguments = _seal(
        private_key="raw-private-key-value",
        password="top-password-value",
        api_key="top-api-key-value",
        environment={"PATH": "visible-env-path-secret", "TOKEN": "env-secret-value"},
        env={"NOTE": "visible-env-path-secret"},
        tool_arguments={
            "auth": {
                "password": "nested-password-value",
                "api_token": "nested-api-token-value",
                "private_key": "nested-private-key-value",
            }
        },
        notes=[{"password": "list-password-value"}],
    )
    approval_id = _insert("readable_probe", "read", arguments)
    detail = client.get(f"/api/v1/approvals/{approval_id}")
    listed = client.get("/api/v1/approvals")
    assert detail.status_code == 200
    assert listed.status_code == 200
    for body in (detail.text, listed.text):
        for secret in _SECRET_VALUES:
            assert secret not in body
        assert "env-secret-value" not in body
    payload = detail.json()
    assert payload["arguments"] == payload["review"]["arguments"]
    assert payload["arguments"]["private_key"] == "***"
    assert payload["arguments"]["password"] == "***"
    assert payload["arguments"]["api_key"] == "***"
    assert payload["arguments"]["environment"] == "***"
    assert payload["arguments"]["env"] == "***"
    assert payload["arguments"]["tool_arguments"]["auth"]["password"] == "***"
    assert payload["arguments"]["tool_arguments"]["auth"]["api_token"] == "***"
    assert payload["arguments"]["tool_arguments"]["auth"]["private_key"] == "***"
    assert payload["arguments"]["notes"] == [{"password": "***"}]
    with Session(get_engine()) as session:
        stored = session.get(ApprovalRequestRecord, approval_id)
        assert "raw-private-key-value" in stored.arguments_json
        assert "visible-env-path-secret" in stored.arguments_json
        assert "nested-private-key-value" in stored.arguments_json
        assert stored.fingerprint
        assert stored.status == "PENDING"


class PathProbe(Tool):
    name = "path_probe"
    description = "Read one workspace path named by the sealed approval."
    category = "test"
    requires_confirmation = False
    execution_policy = ASYNC_READ_ONLY
    enabled = True
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["workspace_id", "relative_path"],
        "properties": {
            "workspace_id": {"type": "string", "minLength": 36, "maxLength": 36},
            "relative_path": {"type": "string", "minLength": 1, "maxLength": 400},
        },
    }
    permission_manifest = ToolManifest(
        tool_id="path_probe",
        name="path_probe",
        description="Read one workspace path named by the sealed approval.",
        resource_types=("workspace", "file"),
        actions=("read",),
        risk="read",
        approval_required=True,
        read_only=True,
        execution_policy=ASYNC_READ_ONLY,
    )

    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[str] = []

    async def run(self, arguments: dict) -> dict:
        self.calls += 1
        self.seen.append(arguments["relative_path"])
        return {"relative_path": arguments["relative_path"]}


def test_relative_path_column_cannot_retarget_a_sealed_approval(client, tmp_path):
    tool = PathProbe()
    get_tool_registry().register(tool)
    client.put("/api/v1/permissions/mode", json={"mode": "always_ask"})
    folder = tmp_path / "notes"
    folder.mkdir()
    workspace = register(client, folder)
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["path_probe"])
    grant_workspace(client, agent["id"], workspace["id"])
    try:
        proposed = client.post(
            "/api/v1/tools/path_probe/test",
            json={
                "arguments": {"workspace_id": workspace["id"], "relative_path": "public.txt"},
                "agent_id": agent["id"],
            },
        )
        assert proposed.status_code == 200, proposed.text
        approval_id = proposed.json()["output"]["approval_id"]
        with Session(get_engine()) as session:
            stored = session.get(ApprovalRequestRecord, approval_id)
            assert stored.relative_path == "public.txt"
            assert json.loads(stored.arguments_json)["relative_path"] == "public.txt"
            assert stored.status == "PENDING"
        ready = client.get(f"/api/v1/approvals/{approval_id}").json()["review"]
        assert ready["verified"] is True
        assert ready["reviewable"] is True
        approved = client.post(f"/api/v1/approvals/{approval_id}/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "COMPLETED"
        assert tool.calls == 1
        assert tool.seen == ["public.txt"]

        second = client.post(
            "/api/v1/tools/path_probe/test",
            json={
                "arguments": {"workspace_id": workspace["id"], "relative_path": "public.txt"},
                "agent_id": agent["id"],
            },
        )
        tampered_id = second.json()["output"]["approval_id"]
        with Session(get_engine()) as session:
            row = session.get(ApprovalRequestRecord, tampered_id)
            row.relative_path = "also.txt"
            session.commit()
            fingerprint = row.fingerprint
            arguments_json = row.arguments_json
        decision = evaluate_request(
            agent_id=agent["id"],
            tool_name="path_probe",
            action="read",
            resource_type="file",
            resource_id=workspace["id"],
            relative_path="also.txt",
            registry=get_tool_registry(),
        )
        assert decision.decision != "DENY"
        review = client.get(f"/api/v1/approvals/{tampered_id}").json()["review"]
        assert review["verified"] is False
        assert review["reviewable"] is False
        blocked = client.post(f"/api/v1/approvals/{tampered_id}/approve")
        assert blocked.status_code == 200
        assert blocked.json()["status"] == "FAILED"
        assert tool.calls == 1
        assert tool.seen == ["public.txt"]
        with Session(get_engine()) as session:
            row = session.get(ApprovalRequestRecord, tampered_id)
            assert row.relative_path == "also.txt"
            assert row.arguments_json == arguments_json
            assert row.fingerprint == fingerprint
            assert json.loads(row.arguments_json)["relative_path"] == "public.txt"
    finally:
        get_tool_registry().unregister(tool.name)
