"""Controlled text create, update, and quarantine.

These tests stay on the local database and do not call a model.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.approvals.fingerprint import operation_fingerprint
from app.approvals.models import ApprovalRequestRecord
from app.core.database import get_engine
from app.files.authorization import authorize, bind_file_actor, reset_file_actor
from app.files.exceptions import FileAccessError
from app.tools.executor import ToolExecutor
from app.tools.registry import get_tool_registry
from tests.test_agents import create_agent
from tests.test_files import grant_workspace, register
from tests.test_tools import grant

import pytest


def _paths(client, agent_id: str, workspace_id: str, grants: list[tuple[str, str]]):
    response = client.put(
        f"/api/v1/permissions/agents/{agent_id}/paths",
        json={
            "grants": [
                {
                    "workspace_id": workspace_id,
                    "relative_path": path,
                    "kind": "file",
                    "action": action,
                }
                for action, path in grants
            ]
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _prepare(client, tmp_path, *actions: str):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "note.txt").write_text("keep", encoding="utf-8")
    workspace = register(client, folder)
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], list(actions))
    grant_workspace(client, agent["id"], workspace["id"])
    return folder, workspace, agent


def _propose(client, agent_id: str, tool: str, arguments: dict):
    response = client.post(
        f"/api/v1/tools/{tool}/test",
        json={"arguments": arguments, "agent_id": agent_id},
    )
    return response


def test_distinct_secrets_and_long_text_do_not_share_a_fingerprint():
    first = operation_fingerprint(
        agent_id="agent",
        tool_name="create_text_file",
        action="create",
        resource_type="file",
        resource_id="workspace",
        arguments={"password": "alpha", "content": "a" * 500 + "X"},
    )
    second = operation_fingerprint(
        agent_id="agent",
        tool_name="create_text_file",
        action="create",
        resource_type="file",
        resource_id="workspace",
        arguments={"password": "beta", "content": "a" * 500 + "Y"},
    )
    assert first != second


def test_read_grant_cannot_write_or_delete(client, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "create_text_file", "update_text_file", "quarantine_file")
    _paths(client, agent["id"], workspace["id"], [("read", "note.txt")])
    for tool, arguments in (
        ("update_text_file", {"workspace_id": workspace["id"], "relative_path": "note.txt", "content": "changed"}),
        ("quarantine_file", {"workspace_id": workspace["id"], "relative_path": "note.txt"}),
        ("create_text_file", {"workspace_id": workspace["id"], "relative_path": "new.txt", "content": "new"}),
    ):
        response = _propose(client, agent["id"], tool, arguments)
        assert response.status_code == 403, response.text
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"
    assert not (folder / "new.txt").exists()


def test_write_and_delete_do_not_imply_each_other(client, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "update_text_file", "quarantine_file")
    _paths(client, agent["id"], workspace["id"], [("write", "note.txt")])
    denied = _propose(
        client,
        agent["id"],
        "quarantine_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt"},
    )
    assert denied.status_code == 403
    _paths(client, agent["id"], workspace["id"], [("delete", "note.txt")])
    denied = _propose(
        client,
        agent["id"],
        "update_text_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt", "content": "changed"},
    )
    assert denied.status_code == 403
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"


def test_workspace_grant_alone_cannot_modify(client, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "update_text_file")
    denied = _propose(
        client,
        agent["id"],
        "update_text_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt", "content": "changed"},
    )
    assert denied.status_code == 403
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"
    tokens = bind_file_actor(agent["id"], "update_text_file")
    try:
        with pytest.raises(FileAccessError, match="not permitted"):
            authorize("write", workspace["id"], "note.txt")
    finally:
        reset_file_actor(tokens)


def test_create_update_and_recovery_round_trip(client, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "create_text_file", "update_text_file", "quarantine_file")
    _paths(
        client,
        agent["id"],
        workspace["id"],
        [("create", "new.txt"), ("write", "note.txt"), ("delete", "note.txt")],
    )
    created = _propose(
        client,
        agent["id"],
        "create_text_file",
        {"workspace_id": workspace["id"], "relative_path": "new.txt", "content": "hello"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "confirmation_required"
    assert not (folder / "new.txt").exists()
    approval_id = created.json()["output"]["approval_id"]
    detail = client.get(f"/api/v1/approvals/{approval_id}")
    assert detail.json()["change"]["operation"] == "create"
    assert "hello" in detail.json()["change"]["diff"]
    assert "password" not in json.dumps(detail.json()["arguments"])
    approved = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "COMPLETED"
    assert (folder / "new.txt").read_text(encoding="utf-8") == "hello"
    again = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert again.status_code == 409
    assert (folder / "new.txt").read_text(encoding="utf-8") == "hello"

    collision = _propose(
        client,
        agent["id"],
        "create_text_file",
        {"workspace_id": workspace["id"], "relative_path": "new.txt", "content": "other"},
    )
    assert collision.status_code == 403
    assert (folder / "new.txt").read_text(encoding="utf-8") == "hello"

    updated = _propose(
        client,
        agent["id"],
        "update_text_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt", "content": "next"},
    )
    assert updated.status_code == 200, updated.text
    update_id = updated.json()["output"]["approval_id"]
    finished = client.post(f"/api/v1/approvals/{update_id}/approve")
    assert finished.status_code == 200, finished.text
    assert (folder / "note.txt").read_text(encoding="utf-8") == "next"
    recoveries = client.get("/api/v1/file-recoveries")
    assert recoveries.status_code == 200
    assert recoveries.json()["count"] == 1
    restored = client.post(f"/api/v1/file-recoveries/{recoveries.json()['recoveries'][0]['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"


def test_rejected_expired_and_changed_operations_do_not_write(client, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "update_text_file", "create_text_file")
    _paths(client, agent["id"], workspace["id"], [("write", "note.txt"), ("create", "fresh.txt")])
    rejected = _propose(
        client,
        agent["id"],
        "create_text_file",
        {"workspace_id": workspace["id"], "relative_path": "fresh.txt", "content": "nope"},
    )
    rejected_id = rejected.json()["output"]["approval_id"]
    assert client.post(f"/api/v1/approvals/{rejected_id}/reject").status_code == 200
    assert not (folder / "fresh.txt").exists()

    expired = _propose(
        client,
        agent["id"],
        "create_text_file",
        {"workspace_id": workspace["id"], "relative_path": "fresh.txt", "content": "later"},
    )
    expired_id = expired.json()["output"]["approval_id"]
    with Session(get_engine()) as session:
        row = session.get(ApprovalRequestRecord, expired_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        session.commit()
    assert client.post(f"/api/v1/approvals/{expired_id}/approve").status_code == 409
    assert not (folder / "fresh.txt").exists()

    changed = _propose(
        client,
        agent["id"],
        "update_text_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt", "content": "approved"},
    )
    changed_id = changed.json()["output"]["approval_id"]
    (folder / "note.txt").write_text("edited", encoding="utf-8")
    result = client.post(f"/api/v1/approvals/{changed_id}/approve")
    assert result.status_code == 200
    assert result.json()["status"] == "FAILED"
    assert (folder / "note.txt").read_text(encoding="utf-8") == "edited"

    tampered = _propose(
        client,
        agent["id"],
        "update_text_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt", "content": "safe"},
    )
    tampered_id = tampered.json()["output"]["approval_id"]
    proposal = None
    from app.approvals.fingerprint import _key_path

    proposal = _key_path().parent / "file-proposals" / tampered_id
    proposal.write_bytes(b"different")
    failed = client.post(f"/api/v1/approvals/{tampered_id}/approve")
    assert failed.json()["status"] == "FAILED"
    assert (folder / "note.txt").read_text(encoding="utf-8") == "edited"

    moved = _propose(
        client,
        agent["id"],
        "update_text_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt", "content": "safe"},
    )
    moved_id = moved.json()["output"]["approval_id"]
    with Session(get_engine()) as session:
        row = session.get(ApprovalRequestRecord, moved_id)
        row.relative_path = "other.txt"
        session.commit()
    moved_result = client.post(f"/api/v1/approvals/{moved_id}/approve")
    assert moved_result.json()["status"] == "FAILED"
    assert (folder / "note.txt").read_text(encoding="utf-8") == "edited"


def test_forged_confirmation_and_worker_payload_cannot_write(client, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "create_text_file")
    _paths(client, agent["id"], workspace["id"], [("create", "new.txt")])
    tool = get_tool_registry().get("create_text_file")

    async def run():
        return await ToolExecutor().run(
            tool,
            {
                "workspace_id": workspace["id"],
                "relative_path": "new.txt",
                "operation": "create",
                "content": "forged",
            },
            confirmed=True,
        )

    import asyncio

    outcome = asyncio.run(run())
    assert outcome.status == "confirmation_required"
    assert not (folder / "new.txt").exists()
    completed = subprocess.run(
        [sys.executable, "-m", "app.tools.worker"],
        input=json.dumps(
            {
                "tool": "create_text_file",
                "effect": "side_effect",
                "confirmed": True,
                "agent_id": agent["id"],
                "arguments": {
                    "workspace_id": workspace["id"],
                    "relative_path": "new.txt",
                    "operation": "create",
                    "content": "forged",
                },
            }
        ).encode(),
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode != 0
    assert not (folder / "new.txt").exists()


def test_path_escapes_and_protected_names_are_rejected(client, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "create_text_file", "update_text_file")
    _paths(client, agent["id"], workspace["id"], [("create", "new.txt"), ("write", "note.txt")])
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = folder / "link.txt"
    link.symlink_to(outside)
    cases = [
        {"workspace_id": workspace["id"], "relative_path": "../outside.txt", "content": "x"},
        {"workspace_id": workspace["id"], "relative_path": "/etc/passwd", "content": "x"},
        {"workspace_id": workspace["id"], "relative_path": "link.txt", "content": "x"},
        {"workspace_id": workspace["id"], "relative_path": ".env", "content": "TOKEN=1"},
    ]
    for arguments in cases:
        response = _propose(client, agent["id"], "create_text_file", arguments)
        assert response.status_code in {403, 422}, response.text
    assert outside.read_text(encoding="utf-8") == "secret"
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"
    assert not (folder / ".env").exists()


def test_failed_update_preserves_the_original(client, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "update_text_file")
    _paths(client, agent["id"], workspace["id"], [("write", "note.txt")])
    proposed = _propose(
        client,
        agent["id"],
        "update_text_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt", "content": "next"},
    )
    approval_id = proposed.json()["output"]["approval_id"]
    os.chmod(folder, 0o500)
    try:
        result = client.post(f"/api/v1/approvals/{approval_id}/approve")
    finally:
        os.chmod(folder, 0o700)
    assert result.status_code == 200
    assert result.json()["status"] == "FAILED"
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"
    assert not list(folder.glob(".*.assistant-tmp"))


def test_quarantine_is_reversible_and_absent_from_workspace_search(client, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "quarantine_file", "search_files")
    _paths(client, agent["id"], workspace["id"], [("delete", "note.txt")])
    proposed = _propose(
        client,
        agent["id"],
        "quarantine_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt"},
    )
    approval_id = proposed.json()["output"]["approval_id"]
    assert "note.txt" in client.get(f"/api/v1/approvals/{approval_id}").json()["change"]["diff"]
    finished = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "COMPLETED"
    assert not (folder / "note.txt").exists()
    listed = client.get("/api/v1/file-recoveries")
    recovery_id = listed.json()["recoveries"][0]["id"]
    assert "keep" not in json.dumps(listed.json())
    search = client.post(
        "/api/v1/tools/search_files/test",
        json={
            "arguments": {"workspace_id": workspace["id"], "query": "keep"},
            "agent_id": agent["id"],
        },
    )
    assert search.status_code == 200, search.text
    assert search.json()["output"]["count"] == 0
    restored = client.post(f"/api/v1/file-recoveries/{recovery_id}/restore")
    assert restored.status_code == 200, restored.text
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"


def test_revoked_path_grant_blocks_execution(client, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "create_text_file")
    _paths(client, agent["id"], workspace["id"], [("create", "new.txt")])
    proposed = _propose(
        client,
        agent["id"],
        "create_text_file",
        {"workspace_id": workspace["id"], "relative_path": "new.txt", "content": "hello"},
    )
    approval_id = proposed.json()["output"]["approval_id"]
    _paths(client, agent["id"], workspace["id"], [])
    blocked = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert blocked.status_code == 409
    assert not (folder / "new.txt").exists()
