"""Crash points in the file-change journal.

Each stage stops the worker after a durable step. Startup reconciliation then
runs twice. The original bytes stay available, and an unfinished change is not
listed as a successful recovery.
"""

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.files.changes import _recovery_dir
from app.files.journal import (
    APPLYING,
    backup_file,
    begin_journal,
    mark_journal,
    reconcile_file_journal,
    write_backup,
)
from app.files.models import FileOperationJournalRecord
from app.tools.registry import get_tool_registry
from tests.test_file_changes import _paths, _prepare, _propose
from tests.test_files import register


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _journals(workspace_id: str | None = None) -> list[tuple[str, str, str, str]]:
    with Session(get_engine()) as session:
        rows = list(session.scalars(select(FileOperationJournalRecord)).all())
        return [
            (row.id, row.state, row.original_sha256, row.proposed_sha256)
            for row in rows
            if workspace_id is None or row.workspace_id == workspace_id
        ]


def _ready(client) -> list[dict]:
    response = client.get("/api/v1/file-recoveries")
    assert response.status_code == 200, response.text
    return response.json()["recoveries"]


def _crash_update(client, monkeypatch, tmp_path, stage: str):
    from tests.test_agents import create_agent
    from tests.test_files import grant_workspace
    from tests.test_tools import grant

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "note.txt").write_text("keep", encoding="utf-8")
    workspace = register(client, folder, name=f"Notes-{stage}")
    agent = create_agent(client, name=f"Agent-{stage}", system_prompt="Be exact.")
    grant(client, agent["id"], ["update_text_file", "search_files"])
    grant_workspace(client, agent["id"], workspace["id"])
    _paths(client, agent["id"], workspace["id"], [("write", "note.txt")])
    proposed = _propose(
        client,
        agent["id"],
        "update_text_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt", "content": "next"},
    )
    assert proposed.status_code == 200, proposed.text
    approval_id = proposed.json()["output"]["approval_id"]
    monkeypatch.setenv("ASSISTANT_FILE_JOURNAL_CRASH", stage)
    try:
        approved = client.post(f"/api/v1/approvals/{approval_id}/approve")
    finally:
        monkeypatch.delenv("ASSISTANT_FILE_JOURNAL_CRASH", raising=False)
    assert approved.status_code == 200, approved.text
    expected = "COMPLETED" if stage in {"after_rename", "before_finalize", "after_finalize"} else "FAILED"
    assert approved.json()["status"] == expected
    if expected == "COMPLETED":
        assert "could not be completed" in (approved.json().get("result") or "")
    return folder, workspace, agent


def test_update_crash_stages_reconcile_without_losing_the_original(client, monkeypatch, tmp_path):
    expectations = {
        "before_journal": (None, "keep", None, 0),
        "after_journal": ("FAILED", "keep", "FAILED", 0),
        "after_backup": ("FAILED", "keep", "FAILED", 0),
        "after_backup_verified": ("FAILED", "keep", "FAILED", 0),
        "after_intent": ("FAILED", "keep", "FAILED", 0),
        "during_temp": ("FAILED", "keep", "FAILED", 0),
        "before_rename": ("FAILED", "keep", "FAILED", 0),
        "after_rename": ("COMMITTED", "next", "COMMITTED", 1),
        "before_finalize": ("COMMITTED", "next", "COMMITTED", 1),
        "after_finalize": ("COMMITTED", "next", "COMMITTED", 1),
    }
    for stage, (interrupted, text, final, ready_count) in expectations.items():
        folder = tmp_path / stage
        folder.mkdir()
        workspace_folder, workspace, agent = _crash_update(client, monkeypatch, folder, stage)
        rows = _journals(workspace["id"])
        if interrupted is None:
            assert rows == []
        else:
            assert len(rows) == 1
            assert rows[0][1] == interrupted
            assert rows[0][2] == _digest("keep")
            assert rows[0][3] == _digest("next")
            if stage not in {"after_journal"}:
                backup = backup_file(rows[0][0])
                assert backup.is_file()
                assert backup.read_bytes() == b"keep"
                assert stat.S_IMODE(backup.stat().st_mode) == 0o600
                assert workspace_folder.resolve() not in backup.resolve().parents
        assert (workspace_folder / "note.txt").read_text(encoding="utf-8") == text
        reconcile_file_journal()
        reconcile_file_journal()
        assert (workspace_folder / "note.txt").read_text(encoding="utf-8") == text
        assert list(workspace_folder.glob(".*.assistant-tmp")) == []
        rows = _journals(workspace["id"])
        if interrupted is None:
            assert rows == []
        else:
            assert rows[0][1] == final
        recoveries = [item for item in _ready(client) if item["workspace_id"] == workspace["id"]]
        assert len(recoveries) == ready_count
        if text == "next":
            search = client.post(
                "/api/v1/tools/search_files/test",
                json={
                    "arguments": {"workspace_id": workspace["id"], "query": "keep"},
                    "agent_id": agent["id"],
                },
            )
            assert search.status_code == 200, search.text
            assert search.json()["output"]["count"] == 0
        if ready_count:
            assert recoveries[0]["content_sha256"] == _digest("keep")
            assert "keep" not in json.dumps(recoveries)
            restored = client.post(f"/api/v1/file-recoveries/{recoveries[0]['id']}/restore")
            assert restored.status_code == 200, restored.text
            assert (workspace_folder / "note.txt").read_text(encoding="utf-8") == "keep"


def test_quarantine_interrupted_after_unlink_stays_recoverable(client, monkeypatch, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "quarantine_file")
    _paths(client, agent["id"], workspace["id"], [("delete", "note.txt")])
    proposed = _propose(
        client,
        agent["id"],
        "quarantine_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt"},
    )
    approval_id = proposed.json()["output"]["approval_id"]
    monkeypatch.setenv("ASSISTANT_FILE_JOURNAL_CRASH", "after_rename")
    try:
        approved = client.post(f"/api/v1/approvals/{approval_id}/approve")
    finally:
        monkeypatch.delenv("ASSISTANT_FILE_JOURNAL_CRASH", raising=False)
    assert approved.json()["status"] == "COMPLETED"
    assert "could not be completed" in (approved.json().get("result") or "")
    assert not (folder / "note.txt").exists()
    assert _journals(workspace["id"])[0][1] == "COMMITTED"
    assert len(_ready(client)) == 1
    reconcile_file_journal()
    reconcile_file_journal()
    assert _journals(workspace["id"])[0][1] == "COMMITTED"
    recoveries = _ready(client)
    assert len(recoveries) == 1
    restored = client.post(f"/api/v1/file-recoveries/{recoveries[0]['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"


def test_quarantine_interrupted_before_unlink_keeps_the_file(client, monkeypatch, tmp_path):
    folder, workspace, agent = _prepare(client, tmp_path, "quarantine_file")
    _paths(client, agent["id"], workspace["id"], [("delete", "note.txt")])
    proposed = _propose(
        client,
        agent["id"],
        "quarantine_file",
        {"workspace_id": workspace["id"], "relative_path": "note.txt"},
    )
    monkeypatch.setenv("ASSISTANT_FILE_JOURNAL_CRASH", "before_rename")
    try:
        approved = client.post(
            f"/api/v1/approvals/{proposed.json()['output']['approval_id']}/approve"
        )
    finally:
        monkeypatch.delenv("ASSISTANT_FILE_JOURNAL_CRASH", raising=False)
    assert approved.json()["status"] == "FAILED"
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"
    reconcile_file_journal()
    reconcile_file_journal()
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"
    assert _journals(workspace["id"])[0][1] == "FAILED"
    assert _ready(client) == []


def test_damaged_backup_missing_target_and_unexpected_hash_fail_closed(client, tmp_path):
    folder, workspace, _agent = _prepare(client, tmp_path, "update_text_file")
    original = _digest("keep")
    proposed = _digest("next")
    cases = ("corrupt", "missing", "absent", "unexpected")
    for name in cases:
        journal_id = begin_journal(
            workspace_id=workspace["id"],
            relative_path="note.txt",
            operation="update",
            original_sha256=original,
            proposed_sha256=proposed,
        )
        write_backup(journal_id, b"keep")
        mark_journal(journal_id, APPLYING)
        target = folder / "note.txt"
        if name == "corrupt":
            target.write_text("next", encoding="utf-8")
            backup_file(journal_id).write_bytes(b"not-the-original")
        elif name == "missing":
            target.write_text("next", encoding="utf-8")
            backup_file(journal_id).unlink()
        elif name == "absent":
            target.unlink()
        else:
            target.write_text("other", encoding="utf-8")
        reconcile_file_journal()
        reconcile_file_journal()
        with Session(get_engine()) as session:
            row = session.get(FileOperationJournalRecord, journal_id)
            assert row is not None
            if name == "absent":
                assert row.state == "RECOVERED"
                assert target.read_text(encoding="utf-8") == "keep"
            elif name == "unexpected":
                assert row.state == "ROLLBACK_REQUIRED"
                assert target.read_text(encoding="utf-8") == "other"
            else:
                assert row.state == "ROLLBACK_REQUIRED"
                assert target.read_text(encoding="utf-8") == "next"
        assert all(item["id"] != journal_id for item in _ready(client))
        target.write_text("keep", encoding="utf-8")


def test_concurrent_journals_for_one_file_do_not_overwrite_each_other(client, tmp_path):
    folder, workspace, _agent = _prepare(client, tmp_path, "update_text_file")
    original = _digest("keep")
    first = begin_journal(
        workspace_id=workspace["id"],
        relative_path="note.txt",
        operation="update",
        original_sha256=original,
        proposed_sha256=_digest("one"),
    )
    second = begin_journal(
        workspace_id=workspace["id"],
        relative_path="note.txt",
        operation="update",
        original_sha256=original,
        proposed_sha256=_digest("two"),
    )
    write_backup(first, b"keep")
    write_backup(second, b"keep")
    mark_journal(first, APPLYING)
    mark_journal(second, APPLYING)
    (folder / "note.txt").write_text("one", encoding="utf-8")
    reconcile_file_journal()
    reconcile_file_journal()
    assert (folder / "note.txt").read_text(encoding="utf-8") == "one"
    states = {row[0]: row[1] for row in _journals(workspace["id"])}
    assert states[first] == "COMMITTED"
    assert states[second] == "ROLLBACK_REQUIRED"
    recoveries = _ready(client)
    assert [item["id"] for item in recoveries] == [first]
    assert backup_file(second).read_bytes() == b"keep"


def test_recovery_directory_stays_outside_the_workspace_and_private(client, tmp_path):
    folder, _workspace, _agent = _prepare(client, tmp_path, "update_text_file")
    directory = _recovery_dir()
    assert folder.resolve() not in directory.resolve().parents
    assert directory.resolve() not in folder.resolve().parents
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_blocking_probe_is_not_a_production_tool(client, tmp_path):
    assert get_tool_registry().get("blocking_probe") is None
    listed = client.get("/api/v1/tools")
    assert listed.status_code == 200
    assert "blocking_probe" not in {item["name"] for item in listed.json()["tools"]}
    denied = client.post(
        "/api/v1/tools/blocking_probe/test",
        json={"arguments": {"marker": "/tmp/marker", "delay": 0, "command": "id"}, "confirmed": True},
    )
    assert denied.status_code == 422
    missing = client.post(
        "/api/v1/tools/blocking_probe/test",
        json={"arguments": {"marker": "/tmp/marker", "delay": 0, "command": "id"}},
    )
    assert missing.status_code == 404
    outside = Path(__file__).resolve().parent / "blocking-probe-marker"
    rejected = subprocess.run(
        [sys.executable, "-m", "app.tools.worker"],
        input=json.dumps(
            {
                "tool": "blocking_probe",
                "effect": "side_effect",
                "confirmed": True,
                "arguments": {"marker": str(outside), "delay": 0, "command": "id"},
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert not outside.exists()
    marker = Path(tempfile.gettempdir()) / f"probe-{os.getpid()}.txt"
    decoy = marker.with_suffix(".decoy")
    marker.unlink(missing_ok=True)
    decoy.unlink(missing_ok=True)
    try:
        accepted = subprocess.run(
            [sys.executable, "-m", "app.tools.worker"],
            input=json.dumps(
                {
                    "tool": "blocking_probe",
                    "effect": "side_effect",
                    "confirmed": True,
                    "arguments": {"marker": str(marker), "delay": 0, "command": f"id > {decoy}"},
                }
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        assert accepted.returncode == 0
        assert marker.read_text(encoding="utf-8") == "done"
        assert not decoy.exists()
        blocked = subprocess.run(
            [sys.executable, "-m", "app.tools.worker"],
            input=json.dumps(
                {
                    "tool": "blocking_probe",
                    "effect": "side_effect",
                    "confirmed": True,
                    "arguments": {"marker": str(marker), "delay": 0, "started": str(outside)},
                }
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        assert blocked.returncode != 0
        assert not outside.exists()
    finally:
        marker.unlink(missing_ok=True)
        decoy.unlink(missing_ok=True)
        outside.unlink(missing_ok=True)
