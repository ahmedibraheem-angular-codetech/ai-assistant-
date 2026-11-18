"""Registered commands. A shell string is never accepted."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.repository import AgentRepository
from app.agents.service import AgentService
from app.approvals.models import ApprovalRequestRecord
from app.commands.audit import begin_command_audit, finish_command_audit, reconcile_command_audits
from app.commands.faults import controller
from app.commands.execute import _assert_workspace, execute_command
from app.commands.registry import command_ids, spec_for
from app.commands.tools import wait_tool
from app.core.database import get_engine
from app.files.authorization import bind_file_actor, reset_file_actor
from app.files.access import ApprovedRoot
from app.files.grants import AgentWorkspaceGrantRepository
from app.permissions.models import PermissionAuditRecord
from app.tools.exceptions import ToolExecutionError
from sqlalchemy.exc import OperationalError
from app.tools.executor import ToolExecutor
from app.tools.grants import AgentToolGrantRepository
from app.tools.registry import get_tool_registry
from app.tools.runtime import stop_file_workers
from app.tools.service import ToolService
from tests.test_agents import create_agent
from tests.test_file_changes import _paths
from tests.test_files import grant_workspace, register
from tests.test_tools import _alive, _autocommit_session, _wait_for_file, grant


def _events(tool_name: str) -> list[PermissionAuditRecord]:
    with Session(get_engine()) as session:
        return list(
            session.scalars(
                select(PermissionAuditRecord).where(PermissionAuditRecord.tool_name == tool_name)
            ).all()
        )


def _ready(client, tmp_path, *tools: str):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "note.txt").write_text("hello", encoding="utf-8")
    workspace = register(client, folder)
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], list(tools))
    grant_workspace(client, agent["id"], workspace["id"])
    return folder, workspace, agent


def _call(client, agent_id: str, name: str, arguments: dict):
    return client.post(
        f"/api/v1/tools/{name}/test",
        json={"arguments": arguments, "agent_id": agent_id},
    )


def test_registered_directory_and_file_commands(client, tmp_path):
    folder, workspace, agent = _ready(client, tmp_path, "show_directory", "measure_file")
    listed = _call(client, agent["id"], "show_directory", {"workspace_id": workspace["id"]})
    assert listed.status_code == 200, listed.text
    assert listed.json()["status"] == "ok"
    assert "note.txt" in listed.json()["output"]["stdout"]
    assert listed.json()["output"]["data_boundary"] == "untrusted_command_output"
    measured = _call(
        client,
        agent["id"],
        "measure_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt"},
    )
    assert measured.status_code == 200, measured.text
    assert measured.json()["output"]["bytes"] == 5
    with Session(get_engine()) as session:
        rows = list(
            session.scalars(
                select(PermissionAuditRecord).where(PermissionAuditRecord.tool_name == "show_directory")
            ).all()
        )
    events = [row.event for row in rows]
    assert "command_started" in events
    assert "command_finished" in events
    finished = next(row for row in rows if row.event == "command_finished")
    assert finished.agent_id == agent["id"]
    assert finished.action == "execute"
    assert "exit=0" in finished.detail
    assert finished.detail.startswith("exec=")
    assert "note.txt" not in (finished.detail or "")
    assert "DATABASE_URL" not in (finished.detail or "")
    assert "command_reconciled" not in events


def test_unknown_command_and_invalid_arguments_are_rejected(client, tmp_path):
    folder, workspace, agent = _ready(client, tmp_path, "show_directory", "measure_file")
    assert get_tool_registry().get("shell") is None
    assert get_tool_registry().get("show_directory") is not None
    missing = _call(client, agent["id"], "shell", {"command": "ls"})
    assert missing.status_code == 404
    extra = _call(
        client,
        agent["id"],
        "show_directory",
        {"workspace_id": workspace["id"], "command": "ls", "executable": "/bin/sh"},
    )
    assert extra.status_code == 422
    injected = _call(
        client,
        agent["id"],
        "measure_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt; touch pwned"},
    )
    assert injected.status_code == 200
    assert injected.json()["status"] == "error"
    assert not (folder / "pwned").exists()


def test_traversal_symlink_and_ungranted_workspace_are_refused(client, tmp_path):
    folder, workspace, agent = _ready(client, tmp_path, "measure_file", "show_directory")
    link = folder / "link.txt"
    link.symlink_to(folder / "note.txt")
    outside = _call(
        client,
        agent["id"],
        "measure_file",
        {"workspace_id": workspace["id"], "relative_path": "../note.txt"},
    )
    assert outside.status_code == 200
    assert outside.json()["status"] == "error"
    linked = _call(
        client,
        agent["id"],
        "measure_file",
        {"workspace_id": workspace["id"], "relative_path": "link.txt"},
    )
    assert linked.json()["status"] == "error"
    assert "Symlinks" in linked.json()["error"]
    other = tmp_path / "other"
    other.mkdir()
    foreign = register(client, other, name="Other")
    denied = _call(client, agent["id"], "show_directory", {"workspace_id": foreign["id"]})
    assert denied.status_code == 403
    _paths(client, agent["id"], workspace["id"], [("read", "note.txt")])
    narrowed = _call(client, agent["id"], "show_directory", {"workspace_id": workspace["id"]})
    assert narrowed.status_code == 403


def test_preview_requires_approval_and_rejects_reuse_and_changes(client, tmp_path):
    folder, workspace, agent = _ready(client, tmp_path, "preview_text")
    proposed = _call(
        client,
        agent["id"],
        "preview_text",
        {"workspace_id": workspace["id"], "relative_path": "note.txt"},
    )
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["status"] == "confirmation_required"
    approval_id = proposed.json()["output"]["approval_id"]
    approved = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "COMPLETED"
    assert "hello" in approved.json()["output"]["stdout"]
    with Session(get_engine()) as session:
        stored = session.get(ApprovalRequestRecord, approval_id)
        assert stored is not None
        assert "hello" not in (stored.result_text or "")
    again = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert again.status_code == 409

    rejected = _call(
        client,
        agent["id"],
        "preview_text",
        {"workspace_id": workspace["id"], "relative_path": "note.txt"},
    )
    rejected_id = rejected.json()["output"]["approval_id"]
    assert client.post(f"/api/v1/approvals/{rejected_id}/reject").status_code == 200

    expired = _call(
        client,
        agent["id"],
        "preview_text",
        {"workspace_id": workspace["id"], "relative_path": "note.txt"},
    )
    expired_id = expired.json()["output"]["approval_id"]
    with Session(get_engine()) as session:
        row = session.get(ApprovalRequestRecord, expired_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        session.commit()
    assert client.post(f"/api/v1/approvals/{expired_id}/approve").status_code == 409

    changed = _call(
        client,
        agent["id"],
        "preview_text",
        {"workspace_id": workspace["id"], "relative_path": "note.txt"},
    )
    changed_id = changed.json()["output"]["approval_id"]
    with Session(get_engine()) as session:
        row = session.get(ApprovalRequestRecord, changed_id)
        payload = json.loads(row.arguments_json)
        payload["relative_path"] = "other.txt"
        row.arguments_json = json.dumps(payload)
        session.commit()
    moved = client.post(f"/api/v1/approvals/{changed_id}/approve")
    assert moved.json()["status"] == "FAILED"
    assert (folder / "note.txt").read_text(encoding="utf-8") == "hello"


def test_worker_cannot_preview_without_approval_or_bypass_the_parent(client, tmp_path):
    folder, workspace, agent = _ready(client, tmp_path, "preview_text")
    completed = subprocess.run(
        [sys.executable, "-m", "app.tools.worker"],
        input=json.dumps(
            {
                "tool": "preview_text",
                "effect": "side_effect",
                "confirmed": True,
                "agent_id": agent["id"],
                "arguments": {"workspace_id": workspace["id"], "relative_path": "note.txt"},
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "hello" not in completed.stdout
    direct = _call(
        client,
        agent["id"],
        "preview_text",
        {"workspace_id": workspace["id"], "relative_path": "note.txt", "confirmed": True},
    )
    assert direct.status_code == 422


def test_timeout_stops_the_process_and_a_child_does_not_survive(client, tmp_path):
    registry = get_tool_registry()
    registry.register(wait_tool())
    try:
        folder, workspace, agent = _ready(client, tmp_path, "bounded_wait")
        waited = _call(client, agent["id"], "bounded_wait", {"workspace_id": workspace["id"]})
    finally:
        registry.unregister("bounded_wait")
    assert waited.status_code == 200, waited.text
    body = waited.json()
    assert body.get("status") == "ok" and body.get("output", {}).get("timed_out") is True, body
    marker = tmp_path / "child-pid"
    probe = subprocess.run(
        [sys.executable, "-c", _CHILD_PROBE, str(marker)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    pid = int(marker.read_text(encoding="utf-8"))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"child {pid} was still running")


def test_start_audit_failure_does_not_run_the_command(client, tmp_path, monkeypatch):
    folder, workspace, agent = _ready(client, tmp_path, "show_directory")
    calls: list[str] = []

    def refuse(session, event, attempt):
        raise OperationalError("injected", {}, RuntimeError("injected"))

    monkeypatch.setattr("app.commands.audit._commit", refuse)
    monkeypatch.setattr("app.commands.execute.supervise", lambda *args, **kwargs: calls.append("ran"))
    tokens = bind_file_actor(agent["id"], "show_directory", None)
    try:
        with pytest.raises(ToolExecutionError, match="could not be recorded"):
            execute_command(spec_for("show_directory"), {"workspace_id": workspace["id"]})
    finally:
        reset_file_actor(tokens)
    assert calls == []
    assert _events("show_directory") == []
    denied = _call(client, agent["id"], "show_directory", {"workspace_id": workspace["id"]})
    assert denied.status_code == 200
    assert "note.txt" in denied.json()["output"]["stdout"]
    assert (folder / "note.txt").read_text(encoding="utf-8") == "hello"


def test_worker_start_audit_failure_returns_an_error(client, tmp_path):
    _folder, workspace, agent = _ready(client, tmp_path, "show_directory")
    controller.fail_commit("command_started")
    try:
        failed = _call(client, agent["id"], "show_directory", {"workspace_id": workspace["id"]})
    finally:
        controller.clear()
    assert failed.status_code == 200
    assert failed.json()["status"] == "error"
    assert "could not be recorded" in failed.json()["error"]
    assert "note.txt" not in failed.text
    assert _events("show_directory") == []


def test_finish_audit_retries_then_records_completion(client, tmp_path):
    _folder, workspace, agent = _ready(client, tmp_path, "show_directory")
    controller.fail_commit("finished_once")
    try:
        listed = _call(client, agent["id"], "show_directory", {"workspace_id": workspace["id"]})
    finally:
        controller.clear()
    assert listed.status_code == 200, listed.text
    assert listed.json()["status"] == "ok"
    events = _events("show_directory")
    assert [row.event for row in events].count("command_started") == 1
    assert [row.event for row in events].count("command_finished") == 1
    assert [row.event for row in events].count("command_reconciled") == 0


def test_finish_audit_failure_is_not_a_successful_result(client, tmp_path):
    _folder, workspace, agent = _ready(client, tmp_path, "show_directory")
    controller.fail_commit("command_finished")
    try:
        failed = _call(client, agent["id"], "show_directory", {"workspace_id": workspace["id"]})
    finally:
        controller.clear()
    assert failed.status_code == 200
    body = failed.json()
    assert body["status"] == "error"
    assert body["error"] == "The command result could not be recorded."
    assert "note.txt" not in failed.text
    events = _events("show_directory")
    names = [row.event for row in events]
    assert names.count("command_started") == 1
    assert "command_finished" not in names
    assert names.count("command_reconciled") == 1
    reconciled = next(row for row in events if row.event == "command_reconciled")
    assert reconciled.decision == "unresolved"
    assert "note.txt" not in reconciled.detail
    assert "DATABASE_URL" not in reconciled.detail
    assert reconcile_command_audits() == 0
    assert [row.event for row in _events("show_directory")].count("command_reconciled") == 1


def test_crash_before_finish_is_reconciled_once(client, tmp_path):
    _folder, workspace, agent = _ready(client, tmp_path, "show_directory")
    controller.crash("before_finish")
    try:
        failed = _call(client, agent["id"], "show_directory", {"workspace_id": workspace["id"]})
    finally:
        controller.clear()
    assert failed.status_code == 200
    assert failed.json()["status"] == "error"
    assert "note.txt" not in failed.text
    events = _events("show_directory")
    names = [row.event for row in events]
    assert names.count("command_started") == 1
    assert "command_finished" not in names
    assert names.count("command_reconciled") == 1
    assert reconcile_command_audits() == 0
    assert [row.event for row in _events("show_directory")].count("command_reconciled") == 1


def test_startup_reconcile_runs_after_the_database_returns(client, tmp_path):
    _folder, workspace, agent = _ready(client, tmp_path, "show_directory")
    controller.fail_commit("command_finished")
    controller.block_reconcile = True
    try:
        failed = _call(client, agent["id"], "show_directory", {"workspace_id": workspace["id"]})
    finally:
        controller.clear()
    assert failed.status_code == 200
    body = failed.json()
    assert body["status"] == "error"
    assert body["error"] == "The command result could not be recorded."
    assert "output" not in body or body["output"] is None
    assert "note.txt" not in failed.text
    stalled = _events("show_directory")
    stalled_names = [row.event for row in stalled]
    assert stalled_names.count("command_started") == 1
    assert "command_finished" not in stalled_names
    assert "command_reconciled" not in stalled_names
    assert reconcile_command_audits() == 1
    assert reconcile_command_audits() == 0
    restored = _events("show_directory")
    assert [row.event for row in restored].count("command_reconciled") == 1
    reconciled = next(row for row in restored if row.event == "command_reconciled")
    assert reconciled.decision == "unresolved"
    assert "note.txt" not in reconciled.detail
    assert "hello" not in reconciled.detail


def test_environment_cannot_arm_audit_faults(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_COMMAND_AUDIT_FAIL", "command_finished")
    monkeypatch.setenv("ASSISTANT_COMMAND_AUDIT_CRASH", "before_finish")
    assert controller.commit_failure is None
    assert controller.crash_stage is None
    _folder, workspace, agent = _ready(client, tmp_path, "show_directory")
    listed = _call(client, agent["id"], "show_directory", {"workspace_id": workspace["id"]})
    assert listed.status_code == 200, listed.text
    assert listed.json()["status"] == "ok"
    assert "note.txt" in listed.json()["output"]["stdout"]
    names = [row.event for row in _events("show_directory")]
    assert names.count("command_started") == 1
    assert names.count("command_finished") == 1
    assert "command_reconciled" not in names


def test_concurrent_reconcile_commits_one_row(client):
    spec = spec_for("show_directory")
    assert spec is not None
    begin_command_audit(spec, {"workspace_id": "3" * 36})
    barrier = threading.Barrier(4)

    def once() -> int:
        barrier.wait()
        return reconcile_command_audits()

    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(lambda _item: once(), range(4)))
    assert sum(counts) == 1
    names = [row.event for row in _events("show_directory")]
    assert names.count("command_reconciled") == 1
    assert "command_finished" not in names


def test_finish_and_reconcile_leave_one_terminal_row(client):
    spec = spec_for("show_directory")
    assert spec is not None
    execution_id = begin_command_audit(spec, {"workspace_id": "4" * 36})
    barrier = threading.Barrier(2)

    def finish() -> None:
        barrier.wait()
        finish_command_audit(spec, {"workspace_id": "4" * 36}, execution_id, "completed", 0)

    def reconcile() -> int:
        barrier.wait()
        return reconcile_command_audits()

    with ThreadPoolExecutor(max_workers=2) as pool:
        finished = pool.submit(finish)
        reconciled = pool.submit(reconcile)
        finished.result()
        reconciled.result()
    terminal = [
        row.event
        for row in _events("show_directory")
        if row.event in {"command_finished", "command_reconciled"}
    ]
    assert len(terminal) == 1


def test_reconcile_does_not_close_a_finished_command(client):
    spec = spec_for("show_directory")
    assert spec is not None
    execution_id = begin_command_audit(spec, {"workspace_id": "1" * 36})
    finish_command_audit(spec, {"workspace_id": "1" * 36}, execution_id, "completed", 0)
    assert reconcile_command_audits() == 0
    assert reconcile_command_audits() == 0
    assert [row.event for row in _events("show_directory")].count("command_reconciled") == 0


def test_open_audit_is_closed_once_without_output(client):
    spec = spec_for("show_directory")
    assert spec is not None
    begin_command_audit(spec, {"workspace_id": "2" * 36})
    assert reconcile_command_audits() == 1
    assert reconcile_command_audits() == 0
    row = next(item for item in _events("show_directory") if item.event == "command_reconciled")
    assert row.decision == "unresolved"
    assert row.detail.startswith("exec=")
    assert "stdout" not in row.detail
    assert "hello" not in row.detail


def test_environment_cannot_change_the_production_registry(client, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_commands.py::forced")
    monkeypatch.setenv("ASSISTANT_COMMAND_AUDIT_FAIL", "command_started")
    monkeypatch.setenv("ASSISTANT_COMMAND_AUDIT_CRASH", "before_finish")
    assert command_ids() == frozenset({"show_directory", "measure_file", "preview_text"})
    assert get_tool_registry().get("bounded_wait") is None
    listed = client.get("/api/v1/tools")
    assert listed.status_code == 200
    assert "bounded_wait" not in [item["name"] for item in listed.json()["tools"]]
    completed = subprocess.run(
        [sys.executable, "-m", "app.tools.worker"],
        input=json.dumps(
            {
                "tool": "bounded_wait",
                "effect": "blocking_read_only",
                "arguments": {"workspace_id": "1" * 36},
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTEST_CURRENT_TEST": "tests/test_commands.py::forced"},
    )
    assert completed.returncode != 0
    missing = client.post(
        "/api/v1/tools/bounded_wait/test",
        json={"arguments": {"workspace_id": "1" * 36}, "agent_id": "2" * 36},
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_cancelling_a_command_kills_the_worker(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_FILE_TEST_DELAY_SECONDS", "1.5")
    started = tmp_path / "cancel-started.txt"
    monkeypatch.setenv("ASSISTANT_FILE_TEST_STARTED_FILE", str(started))
    registry = get_tool_registry()
    registry.register(wait_tool())
    folder, workspace, agent = _ready(client, tmp_path, "bounded_wait")
    (folder / "note.txt").write_text("hello", encoding="utf-8")
    session = _autocommit_session()
    service = ToolService(
        registry,
        AgentToolGrantRepository(session),
        AgentService(AgentRepository(session)),
        workspace_grants=AgentWorkspaceGrantRepository(session),
    )
    task = asyncio.create_task(
        service.test("bounded_wait", {"workspace_id": workspace["id"]}, agent["id"])
    )
    try:
        await _wait_for_file(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        record = service.executor.reaped_workers[-1]
        assert record.returncode == -signal.SIGKILL
        assert not _alive(record.pid)
    finally:
        if not task.done():
            task.cancel()
        session.close()
        registry.unregister("bounded_wait")


def test_sensitive_directories_are_refused():
    for path in ("/", "/etc", "/usr/bin", "/private/etc"):
        with pytest.raises(ToolExecutionError):
            _assert_workspace(ApprovedRoot(path=path, device=1, inode=1))


def test_output_is_capped_and_the_environment_is_not_inherited(tmp_path):
    marker = tmp_path / "env.txt"
    probe = subprocess.run(
        [sys.executable, "-c", _LIMIT_PROBE, str(marker)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "DATABASE_URL": "secret-database-url", "APPROVAL_KEY": "secret-approval"},
    )
    assert probe.returncode == 0, probe.stderr
    recorded = marker.read_text(encoding="utf-8")
    assert "DATABASE_URL" not in recorded
    assert "secret-database-url" not in recorded
    assert "secret-approval" not in recorded


@pytest.mark.asyncio
async def test_revoking_a_workspace_stops_the_command(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_FILE_TEST_DELAY_SECONDS", "1.5")
    started = tmp_path / "command-started.txt"
    monkeypatch.setenv("ASSISTANT_FILE_TEST_STARTED_FILE", str(started))
    folder, workspace, agent = _ready(client, tmp_path, "show_directory")
    (folder / "note.txt").write_text("hello", encoding="utf-8")
    session = _autocommit_session()
    service = ToolService(
        get_tool_registry(),
        AgentToolGrantRepository(session),
        AgentService(AgentRepository(session)),
        workspace_grants=AgentWorkspaceGrantRepository(session),
    )
    task = asyncio.create_task(
        service.test("show_directory", {"workspace_id": workspace["id"]}, agent["id"])
    )
    try:
        await _wait_for_file(started)
        killed = stop_file_workers(agent_id=agent["id"], workspace_id=workspace["id"])
        outcome = await task
    finally:
        if not task.done():
            task.cancel()
        session.close()
    assert killed
    assert outcome.status == "error"
    assert outcome.output is None
    record = service.executor.reaped_workers[-1]
    assert record.returncode == -signal.SIGKILL
    assert not _alive(record.pid)
    client.put(f"/api/v1/agents/{agent['id']}/workspaces", json={"workspace_ids": []})
    denied = _call(client, agent["id"], "show_directory", {"workspace_id": workspace["id"]})
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_parent_and_child_both_require_permission(client, tmp_path):
    folder, workspace, agent = _ready(client, tmp_path, "preview_text")
    tool = get_tool_registry().get("preview_text")
    outcome = await ToolExecutor().run(
        tool,
        {"workspace_id": workspace["id"], "relative_path": "note.txt"},
        confirmed=True,
        agent_id=agent["id"],
    )
    assert outcome.status == "confirmation_required"
    assert outcome.output is None
    ungranted = create_agent(client, name="Command Child", system_prompt="Be exact.")
    completed = subprocess.run(
        [sys.executable, "-m", "app.tools.worker"],
        input=json.dumps(
            {
                "tool": "show_directory",
                "effect": "blocking_read_only",
                "agent_id": ungranted["id"],
                "arguments": {"workspace_id": workspace["id"]},
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "note.txt" not in completed.stdout
    parent = _call(client, ungranted["id"], "show_directory", {"workspace_id": workspace["id"]})
    assert parent.status_code == 403


@pytest.mark.asyncio
async def test_two_commands_can_run_together(client, tmp_path):
    folder, workspace, agent = _ready(client, tmp_path, "show_directory")
    (folder / "other.txt").write_text("x", encoding="utf-8")

    def service() -> ToolService:
        session = _autocommit_session()
        return ToolService(
            get_tool_registry(),
            AgentToolGrantRepository(session),
            AgentService(AgentRepository(session)),
            workspace_grants=AgentWorkspaceGrantRepository(session),
        )

    first = service()
    second = service()
    try:
        left, right = await asyncio.gather(
            first.test("show_directory", {"workspace_id": workspace["id"]}, agent["id"]),
            second.test("show_directory", {"workspace_id": workspace["id"]}, agent["id"]),
        )
    finally:
        first.grants.session.close()
        second.grants.session.close()
    assert left.status == "ok"
    assert right.status == "ok"
    assert "note.txt" in left.output["stdout"]
    assert "other.txt" in right.output["stdout"]


def test_terminal_capability_does_not_enable_a_shell(client):
    response = client.get("/api/v1/permissions")
    assert response.status_code == 200
    terminal = next(item for item in response.json()["capabilities"] if item["name"] == "terminal")
    assert terminal["executable"] is False
    assert get_tool_registry().get("shell") is None


_CHILD_PROBE = r"""
import os
import sys
from app.commands.runner import supervise

marker = sys.argv[1]
directory = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
code = (
    "import os,sys,time; marker=sys.argv[1]; pid=os.fork(); "
    "(open(marker,'w').write(str(os.getpid())), time.sleep(30)) if pid==0 else time.sleep(30)"
)
result = supervise(
    [sys.executable, "-c", code, marker],
    directory_fd=directory,
    timeout=0.4,
    max_output=100,
    cpu_seconds=2,
    max_file_bytes=100000,
)
sys.exit(0 if result["timed_out"] else 1)
"""


_LIMIT_PROBE = r"""
import os
import sys
from app.commands.runner import supervise

marker = sys.argv[1]
directory = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
flood = supervise(
    [sys.executable, "-c", "import sys; sys.stdout.write('A'*100000)"],
    directory_fd=directory,
    timeout=2,
    max_output=32,
    cpu_seconds=2,
    max_file_bytes=100000,
)
record = supervise(
    [sys.executable, "-c", "import os,sys; open(sys.argv[1],'w').write('\\n'.join(sorted(os.environ)))", marker],
    directory_fd=directory,
    timeout=2,
    max_output=500,
    cpu_seconds=2,
    max_file_bytes=100000,
)
ok = flood["truncated"] and len(flood["stdout"]) <= 32 and record["exit_code"] == 0
sys.exit(0 if ok else 1)
"""
