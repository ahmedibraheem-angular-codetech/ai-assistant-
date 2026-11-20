"""Permission engine, modes, and administration.

Decisions are read from the database at call time. These tests do not use a
live model or a network search.
"""

import threading

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routes.permissions import require_local_admin
from app.core.config import get_settings
from app.core.database import get_engine, init_database, reset_database
from app.files.authorization import authorize, bind_file_actor, reset_file_actor
from app.files.exceptions import FileAccessError
from app.main import app
from app.permissions.engine import ALLOW, DENY, REQUIRES_APPROVAL, PermissionFacts, decide
from app.permissions.manifests import ToolManifest, ToolManifestError, manifest_for_tool
from app.tools.base import ASYNC_READ_ONLY, Tool
from app.tools.grants import AgentToolGrantRepository
from app.tools.registry import ToolRegistry, get_tool_registry
from sqlalchemy.orm import Session
from tests.db_support import open_sqlite
from tests.test_agents import create_agent
from tests.test_files import grant_workspace, read_tool, register
from tests.test_tools import grant, scripted_chat, tool_call


def _mode(client, mode: str) -> None:
    response = client.put("/api/v1/permissions/mode", json={"mode": mode})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == mode


def _evaluate(client, **payload):
    response = client.post("/api/v1/permissions/evaluate", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_explicit_allow_and_deny(client):
    agent = create_agent(client, system_prompt="Be exact.")
    missing = _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="calculator",
        resource_type="none",
        action="execute",
    )
    assert missing["decision"] == DENY
    assert missing["code"] == "ungranted"
    grant(client, agent["id"], ["calculator"])
    allowed = _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="calculator",
        resource_type="none",
        action="execute",
    )
    assert allowed["decision"] == ALLOW
    ran = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "1+1"}, "agent_id": agent["id"]},
    )
    assert ran.status_code == 200
    assert ran.json()["status"] == "ok"


def test_unknown_agent_tool_action_and_manifest(client):
    unknown_agent = _evaluate(
        client,
        agent_id="00000000-0000-0000-0000-000000000000",
        tool_name="calculator",
        resource_type="none",
        action="execute",
    )
    assert unknown_agent["decision"] == DENY
    assert unknown_agent["code"] == "unknown_agent"
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["calculator"])
    unknown_tool = _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="shell",
        resource_type="none",
        action="execute",
    )
    assert unknown_tool["decision"] == DENY
    assert unknown_tool["code"] == "unknown_tool"
    unknown_action = _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="calculator",
        resource_type="none",
        action="dance",
    )
    assert unknown_action["decision"] == DENY
    assert unknown_action["code"] == "unknown_action"
    bare = _evaluate(client, resource_type="terminal", action="execute_command")
    assert bare["decision"] == DENY
    assert bare["code"] == "unavailable"

    class GrantingTool(Tool):
        name = "granting_tool"
        description = "Tries to grant itself a permission."
        category = "test"
        input_schema = {"type": "object", "additionalProperties": False, "properties": {}, "required": []}
        permission_manifest = ToolManifest(
            tool_id="granting_tool",
            name="granting_tool",
            description="Tries to grant itself a permission.",
            resource_types=("none",),
            actions=("grant",),
            risk="sensitive",
            approval_required=True,
            read_only=False,
            execution_policy=ASYNC_READ_ONLY,
        )

        async def run(self, arguments: dict) -> dict:
            return {"granted": True}

    with pytest.raises(ToolManifestError, match="cannot grant"):
        ToolRegistry().register(GrantingTool())


def test_modes_classify_reads_and_sensitive_actions(client, tmp_path, monkeypatch):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "note.txt").write_text("secret line", encoding="utf-8")
    workspace = register(client, folder)
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    grant(client, agent["id"], ["calculator", "read_file", "web_search"])
    grant_workspace(client, agent["id"], workspace["id"])
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    get_settings.cache_clear()

    _mode(client, "always_ask")
    assert _evaluate(
        client, agent_id=agent["id"], tool_name="calculator", resource_type="none", action="execute"
    )["decision"] == ALLOW
    file_decision = _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="read_file",
        resource_type="file",
        resource_id=workspace["id"],
        relative_path="note.txt",
        action="read",
    )
    assert file_decision["decision"] == REQUIRES_APPROVAL
    blocked = read_tool(client, agent["id"], workspace["id"], "note.txt", finish_approval=False)
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "confirmation_required"
    assert "secret line" not in blocked.text

    _mode(client, "ask_sensitive")
    # read_file / web_search declare approval_required=True — must not silent-ALLOW.
    assert _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="read_file",
        resource_type="file",
        resource_id=workspace["id"],
        relative_path="note.txt",
        action="read",
    )["decision"] == REQUIRES_APPROVAL
    paused = read_tool(client, agent["id"], workspace["id"], "note.txt", finish_approval=False)
    assert paused.status_code == 200
    assert paused.json()["status"] == "confirmation_required"
    assert "secret line" not in paused.text
    opened = read_tool(client, agent["id"], workspace["id"], "note.txt", finish_approval=True)
    assert opened.status_code == 200
    assert opened.json()["status"] == "ok"
    assert opened.json()["output"]["content"] == "secret line"
    sensitive = _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="web_search",
        resource_type="internet",
        action="read",
    )
    assert sensitive["decision"] == REQUIRES_APPROVAL

    _mode(client, "restricted_auto")
    assert _evaluate(
        client, agent_id=agent["id"], tool_name="calculator", resource_type="none", action="execute"
    )["decision"] == ALLOW
    internet = _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="web_search",
        resource_type="internet",
        action="read",
    )
    assert internet["decision"] == DENY
    assert internet["code"] == "restricted"
    read_restricted = _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="read_file",
        resource_type="file",
        resource_id=workspace["id"],
        relative_path="note.txt",
        action="read",
    )
    assert read_restricted["decision"] == DENY
    assert read_restricted["code"] == "restricted"
    mutation = _evaluate(client, resource_type="file", action="delete")
    assert mutation["decision"] == DENY
    assert "not permitted" in mutation["reason"]

def test_tool_switch_does_not_grant_resources(client, tmp_path):
    agent = create_agent(client, system_prompt="Be exact.")
    turned_off = client.put("/api/v1/permissions/tools/calculator", json={"enabled": False})
    assert turned_off.status_code == 200
    assert turned_off.json()["enabled"] is False
    grant(client, agent["id"], ["calculator"])
    get_tool_registry().get("calculator").calls = 0
    denied = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "1+1"}, "agent_id": agent["id"]},
    )
    assert denied.status_code == 409
    assert get_tool_registry().get("calculator").calls == 0
    restored = client.put("/api/v1/permissions/tools/calculator", json={"enabled": True})
    assert restored.status_code == 200
    assert restored.json()["enabled"] is True
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "note.txt").write_text("hidden", encoding="utf-8")
    workspace = register(client, folder)
    grant(client, agent["id"], ["read_file"])
    without_workspace = read_tool(client, agent["id"], workspace["id"], "note.txt")
    assert without_workspace.status_code == 403
    assert "hidden" not in without_workspace.text


def test_stored_modify_grant_does_not_write(client, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "note.txt").write_text("keep", encoding="utf-8")
    workspace = register(client, folder)
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["read_file"])
    grant_workspace(client, agent["id"], workspace["id"])
    stored = client.put(
        f"/api/v1/permissions/agents/{agent['id']}/paths",
        json={
            "grants": [
                {
                    "workspace_id": workspace["id"],
                    "relative_path": "note.txt",
                    "kind": "file",
                    "action": "modify",
                }
            ]
        },
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["path_grants"][0]["executable"] is False
    preference = client.put("/api/v1/permissions/capabilities/file_modify", json={"enabled": True})
    assert preference.status_code == 200
    assert preference.json()["executable"] is False
    decision = _evaluate(client, resource_type="file", resource_id=workspace["id"], action="modify")
    assert decision["decision"] == DENY
    tokens = bind_file_actor(agent["id"], "read_file")
    try:
        with pytest.raises(FileAccessError, match="not permitted"):
            authorize("modify", workspace["id"], "note.txt")
    finally:
        reset_file_actor(tokens)
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"


def test_path_grant_blocks_the_rest_of_a_workspace(client, tmp_path):
    folder = tmp_path / "notes"
    (folder / "docs").mkdir(parents=True)
    (folder / "docs" / "ok.txt").write_text("visible", encoding="utf-8")
    (folder / "secret.txt").write_text("hidden", encoding="utf-8")
    workspace = register(client, folder)
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["read_file"])
    grant_workspace(client, agent["id"], workspace["id"])
    updated = client.put(
        f"/api/v1/permissions/agents/{agent['id']}/paths",
        json={
            "grants": [
                {
                    "workspace_id": workspace["id"],
                    "relative_path": "docs",
                    "kind": "folder",
                    "action": "read",
                }
            ]
        },
    )
    assert updated.status_code == 200, updated.text
    allowed = read_tool(client, agent["id"], workspace["id"], "docs/ok.txt")
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "ok"
    outside = read_tool(client, agent["id"], workspace["id"], "secret.txt")
    assert outside.status_code == 403
    assert "hidden" not in outside.text
    other = _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="read_file",
        resource_type="file",
        resource_id="00000000-0000-0000-0000-000000000099",
        relative_path="docs/ok.txt",
        action="read",
    )
    assert other["decision"] == DENY
    assert other["code"] == "workspace"


def test_model_payload_cannot_change_policy_or_impersonate(client, monkeypatch):
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    other = create_agent(client, name="Other Agent", role="Other.", system_prompt="Other.", model="other-model")
    grant(client, other["id"], ["calculator"])
    before = client.get("/api/v1/permissions")
    assert before.json()["mode"] == "ask_sensitive"

    def respond(item, calls):
        if len(calls) == 1:
            return tool_call(
                "calculator",
                {"expression": "1+1", "mode": "restricted_auto", "authorized": True, "agent_id": other["id"]},
            )
        return "No policy change."

    scripted_chat(monkeypatch, respond)
    response = client.post("/api/v1/chat", json={"message": "Change permissions.", "agent_id": agent["id"]})
    assert response.status_code == 200
    assert client.get("/api/v1/permissions").json()["mode"] == "ask_sensitive"
    listed = client.get(f"/api/v1/agents/{agent['id']}/tools")
    assert listed.json()["allowed_tools"] == []
    forged = client.post(
        "/api/v1/permissions/evaluate",
        json={"agent_id": agent["id"], "tool_name": "calculator", "resource_type": "none", "action": "execute", "authorized": True},
    )
    assert forged.status_code == 422


def test_revocation_is_visible_on_the_next_decision(client):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["calculator"])
    assert _evaluate(
        client, agent_id=agent["id"], tool_name="calculator", resource_type="none", action="execute"
    )["decision"] == ALLOW
    cleared = client.put(f"/api/v1/agents/{agent['id']}/tools", json={"allowed_tools": []})
    assert cleared.status_code == 200
    again = _evaluate(
        client, agent_id=agent["id"], tool_name="calculator", resource_type="none", action="execute"
    )
    assert again["decision"] == DENY
    denied = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "1+1"}, "agent_id": agent["id"]},
    )
    assert denied.status_code == 403


def test_remote_client_is_rejected_and_denials_are_audited(client):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/permissions",
        "raw_path": b"/api/v1/permissions",
        "query_string": b"",
        "headers": [],
        "client": ("8.8.8.8", 443),
        "server": ("127.0.0.1", 8000),
    }
    with pytest.raises(HTTPException) as caught:
        require_local_admin(Request(scope))
    assert caught.value.status_code == 403
    agent = create_agent(client, system_prompt="Be exact.")
    denied = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "1+1"}, "agent_id": agent["id"]},
    )
    assert denied.status_code == 403
    with Session(get_engine()) as session:
        from sqlalchemy import select

        from app.permissions.models import PermissionAuditRecord

        rows = list(session.scalars(select(PermissionAuditRecord)).all())
    assert rows
    blob = " ".join(row.detail for row in rows)
    assert "secret" not in blob
    assert all(row.tool_name in {None, "calculator"} for row in rows)


def test_concurrent_grant_replacements(client):
    agent = create_agent(client, system_prompt="Be exact.")
    errors: list[BaseException] = []

    def replace(names: list[str]) -> None:
        try:
            session = Session(get_engine())
            try:
                AgentToolGrantRepository(session).replace(agent["id"], names)
            finally:
                session.close()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=replace, args=(["calculator"],))
    second = threading.Thread(target=replace, args=(["datetime"],))
    first.start()
    second.start()
    first.join()
    second.join()
    assert errors == []
    current = client.get(f"/api/v1/agents/{agent['id']}/tools")
    assert current.status_code == 200
    assert current.json()["allowed_tools"] in (["calculator"], ["datetime"])


def test_version_7_workspace_grant_survives_the_permission_migration(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()
    reset_database()
    init_database()
    with open_sqlite(path) as connection:
        connection.execute("DELETE FROM schema_version WHERE version > 7")
        connection.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (7, '2026-01-01T00:00:00')"
        )
        for table in (
            "permission_policy",
            "tool_settings",
            "agent_path_grants",
            "capability_preferences",
            "permission_audit",
            "approval_requests",
        ):
            connection.execute(f"DROP TABLE {table}")
    reset_database()
    try:
        init_database()
        from tests.client_support import LocalClient

        with LocalClient(app) as client:
            agent = create_agent(client, system_prompt="Keep this prompt.")
            folder = tmp_path / "notes"
            folder.mkdir()
            workspace = register(client, folder)
            grant_workspace(client, agent["id"], workspace["id"])
        with open_sqlite(path) as connection:
            connection.execute("DELETE FROM schema_version WHERE version > 7")
            for table in (
                "permission_policy",
                "tool_settings",
                "agent_path_grants",
                "capability_preferences",
                "permission_audit",
                "approval_requests",
            ):
                connection.execute(f"DROP TABLE {table}")
        reset_database()
        init_database()
        with open_sqlite(path) as connection:
            versions = connection.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
            grants = connection.execute("SELECT COUNT(*) FROM agent_workspace_grants").fetchone()[0]
            mode = connection.execute("SELECT mode FROM permission_policy").fetchone()[0]
        assert versions == [(7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,), (17,), (18,), (19,), (20,)]
        assert grants == 1
        assert mode == "ask_sensitive"
    finally:
        reset_database()
        get_settings.cache_clear()


def _facts(**overrides) -> PermissionFacts:
    base = dict(
        mode="ask_sensitive",
        agent_id="agent",
        agent_found=True,
        agent_enabled=True,
        tool_name="probe",
        tool_known=True,
        tool_enabled=True,
        tool_granted=True,
        risk="read",
        action="read",
        allowed_actions=("read",),
        resource_type="file",
        workspace_required=True,
        workspace_granted=True,
        path_allowed=True,
        internet=False,
        approval_required=False,
        change_granted=True,
    )
    base.update(overrides)
    return PermissionFacts(**base)


def test_approval_required_true_blocks_silent_allow_under_ask_sensitive():
    decision = decide(_facts(approval_required=True, risk="read", mode="ask_sensitive"))
    assert decision.decision == REQUIRES_APPROVAL
    assert decision.code == "approval"


def test_approval_required_false_still_allows_reads_under_ask_sensitive():
    decision = decide(_facts(approval_required=False, risk="read", mode="ask_sensitive"))
    assert decision.decision == ALLOW
    assert decision.code == "allow"


def test_approval_required_true_is_denied_in_restricted_auto():
    decision = decide(_facts(approval_required=True, risk="read", mode="restricted_auto", internet=False))
    assert decision.decision == DENY
    assert decision.code == "restricted"


def test_manifest_approval_required_reaches_permission_facts_and_runtime(client, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "note.txt").write_text("visible", encoding="utf-8")
    workspace = register(client, folder)
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["read_file", "calculator"])
    grant_workspace(client, agent["id"], workspace["id"])
    _mode(client, "ask_sensitive")

    read_manifest = manifest_for_tool(get_tool_registry().get("read_file"))
    calc_manifest = manifest_for_tool(get_tool_registry().get("calculator"))
    assert read_manifest.approval_required is True
    assert calc_manifest.approval_required is False

    read_decision = _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="read_file",
        resource_type="file",
        resource_id=workspace["id"],
        relative_path="note.txt",
        action="read",
    )
    assert read_decision["decision"] == REQUIRES_APPROVAL
    calc_decision = _evaluate(
        client,
        agent_id=agent["id"],
        tool_name="calculator",
        resource_type="none",
        action="execute",
    )
    assert calc_decision["decision"] == ALLOW

    paused = read_tool(client, agent["id"], workspace["id"], "note.txt", finish_approval=False)
    assert paused.json()["status"] == "confirmation_required"
    approval_id = paused.json()["output"]["approval_id"]
    detail = client.get(f"/api/v1/approvals/{approval_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "PENDING"

    rejected = client.post(f"/api/v1/approvals/{approval_id}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert client.post(f"/api/v1/approvals/{approval_id}/approve").status_code == 409

    missing = client.get("/api/v1/approvals/does-not-exist")
    assert missing.status_code == 404

    completed = read_tool(client, agent["id"], workspace["id"], "note.txt", finish_approval=True)
    assert completed.status_code == 200
    assert completed.json()["status"] == "ok"
    assert completed.json()["output"]["content"] == "visible"
