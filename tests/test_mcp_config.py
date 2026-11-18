"""MCP configuration revisions. These tests do not start a server."""

import json
import logging
import os
import signal
import threading

import pytest

from app.core.config import get_settings
from app.core.database import init_database, reset_database
from app.mcp import loader
from app.mcp.loader import (
    clear_reload_fault,
    config_path,
    current_snapshot,
    handle_sighup,
    mcp_status,
    recover_configuration,
    reload_configuration,
    set_reload_fault,
)
from app.tools.registry import get_tool_registry
from app.tools.worker import ALLOWED_TOOLS
from tests.db_support import open_sqlite


_SECRET = "super-secret-value"


@pytest.fixture
def mcp_db(database_file):
    init_database()
    clear_reload_fault()
    loader._snapshot = None
    yield database_file
    clear_reload_fault()
    loader._snapshot = None


def _document(env=None, executable="/bin/ls", roots=None, extra=None) -> dict:
    server = {
        "id": "notes",
        "display_name": "Local notes",
        "enabled": False,
        "executable": executable,
        "args": [],
        "env": {} if env is None else env,
        "agent_ids": [],
        "tool_names": [],
    }
    if extra:
        server.update(extra)
    return {"executable_roots": ["/bin"] if roots is None else roots, "servers": [server]}


def _write(database, payload: bytes) -> None:
    directory = database.parent / "mcp"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory / "servers.json", flags, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    os.chmod(directory / "servers.json", 0o600)


def _bytes(document: dict) -> bytes:
    return json.dumps(document).encode()


def _row(database):
    with open_sqlite(database) as connection:
        return connection.execute(
            "SELECT revision, epoch FROM mcp_config_revisions WHERE id = 'global'"
        ).fetchone()


def test_valid_configuration_publishes_one_revision(mcp_db):
    _write(mcp_db, _bytes(_document()))
    result = reload_configuration()
    status = mcp_status()
    snapshot = current_snapshot()
    assert result.published is True
    assert result.reason == "ok"
    assert status.available is True
    assert status.epoch == 1
    assert snapshot is not None
    assert snapshot.servers[0].id == "notes"
    assert snapshot.revision == status.revision == _row(mcp_db)[0]
    assert "super-secret" not in repr(snapshot)
    again = reload_configuration()
    assert again.reason == "unchanged"
    assert _row(mcp_db)[1] == 1


def test_invalid_configurations_are_rejected(mcp_db, caplog):
    caplog.set_level(logging.WARNING)
    _write(mcp_db, b"{")
    assert reload_configuration().reason == "invalid"
    _write(mcp_db, _bytes({"executable_roots": ["/bin"]}))
    assert reload_configuration().reason == "invalid"
    malformed = _document()
    del malformed["servers"][0]["display_name"]
    _write(mcp_db, _bytes(malformed))
    assert reload_configuration().reason == "invalid"
    unsupported = _document(extra={"transport": "http"})
    _write(mcp_db, _bytes(unsupported))
    assert reload_configuration().reason == "invalid"
    _write(mcp_db, _bytes(_document(executable="notes", env={"TOKEN": _SECRET})))
    failed = reload_configuration()
    assert failed.reason == "invalid"
    assert _SECRET not in str(failed)
    assert _SECRET not in caplog.text
    assert current_snapshot() is None


def test_unreadable_file_and_symlink_do_not_publish(mcp_db):
    assert reload_configuration().reason == "unreadable"
    _write(mcp_db, _bytes(_document()))
    path = config_path()
    os.chmod(path, 0o640)
    assert reload_configuration().reason == "unreadable"
    os.chmod(path, 0o600)
    os.remove(path)
    os.symlink("/bin/ls", path)
    assert reload_configuration().reason == "unreadable"
    assert current_snapshot() is None


def test_failed_reload_keeps_the_committed_snapshot(mcp_db):
    _write(mcp_db, _bytes(_document()))
    assert reload_configuration().published is True
    committed = _row(mcp_db)[0]
    _write(mcp_db, b"[]")
    assert reload_configuration().reason == "invalid"
    assert _row(mcp_db)[0] == committed
    assert loader._snapshot.revision == committed
    assert mcp_status().available is False

    _write(mcp_db, _bytes(_document(executable="/bin/echo")))

    def change_again():
        _write(mcp_db, _bytes(_document(executable="/usr/bin/true", roots=["/usr/bin"])))

    set_reload_fault("before_reread", change_again)
    assert reload_configuration().reason == "mismatch"
    assert _row(mcp_db)[0] == committed
    assert loader._snapshot.revision == committed


def test_persistence_and_compare_and_swap_failures(mcp_db):
    _write(mcp_db, _bytes(_document()))
    set_reload_fault("before_commit", lambda: (_ for _ in ()).throw(RuntimeError("disk")))
    assert reload_configuration().reason == "failed"
    assert _row(mcp_db) is None
    clear_reload_fault()
    assert reload_configuration().published is True
    original = _row(mcp_db)

    def tamper():
        with open_sqlite(mcp_db) as connection:
            connection.execute(
                "UPDATE mcp_config_revisions SET revision = ? WHERE id = 'global'",
                ("f" * 64,),
            )

    set_reload_fault("before_cas", tamper)
    _write(mcp_db, _bytes(_document(executable="/bin/echo")))
    assert reload_configuration().reason == "conflict"
    assert loader._snapshot.revision == original[0]
    assert loader._snapshot.epoch == original[1]


def test_commit_without_publication_stays_inactive_until_recovery(mcp_db):
    _write(mcp_db, _bytes(_document()))
    set_reload_fault("after_commit", lambda: (_ for _ in ()).throw(RuntimeError("stopped")))
    assert reload_configuration().reason == "unpublished"
    assert loader._snapshot is None
    assert _row(mcp_db)[1] == 1
    assert mcp_status().available is False
    clear_reload_fault()
    first = recover_configuration()
    second = recover_configuration()
    assert first.available is True
    assert second == first
    assert _row(mcp_db)[1] == 1
    assert current_snapshot().revision == _row(mcp_db)[0]


def test_concurrent_reloads_publish_once(mcp_db):
    _write(mcp_db, _bytes(_document()))
    entered = threading.Event()
    release = threading.Event()

    def pause():
        entered.set()
        assert release.wait(5)

    set_reload_fault("before_cas", pause)
    results = []
    first = threading.Thread(target=lambda: results.append(reload_configuration()))
    second = threading.Thread(target=lambda: results.append(reload_configuration()))
    first.start()
    assert entered.wait(5)
    second.start()
    assert second.is_alive()
    release.set()
    first.join(5)
    second.join(5)
    assert [item.published for item in results].count(True) == 2
    assert _row(mcp_db)[1] == 1


def test_startup_recovery_is_idempotent_and_fails_closed(mcp_db):
    _write(mcp_db, _bytes(_document()))
    assert reload_configuration().published is True
    loaded = _row(mcp_db)
    loader._snapshot = None
    assert recover_configuration().available is True
    assert recover_configuration().epoch == loaded[1]
    assert _row(mcp_db) == loaded

    _write(mcp_db, _bytes(_document(executable="/bin/echo")))
    changed = mcp_status()
    assert changed.available is False
    assert changed.reason == "mismatch"
    assert loader._snapshot.revision == loaded[0]
    assert current_snapshot() is None
    _write(mcp_db, _bytes(_document()))
    restored = mcp_status()
    assert restored.available is True
    assert restored.epoch == loaded[1]
    assert _row(mcp_db) == loaded

    _write(mcp_db, _bytes(_document(executable="/bin/echo")))
    loader._snapshot = None
    assert recover_configuration().reason == "mismatch"
    assert recover_configuration().available is False
    assert loader._snapshot is None
    assert _row(mcp_db) == loaded
    with open_sqlite(mcp_db) as connection:
        connection.execute(
            "UPDATE mcp_config_revisions SET revision = ? WHERE id = 'global'",
            ("a" * 64,),
        )
    assert recover_configuration().reason == "mismatch"
    assert _row(mcp_db)[0] == "a" * 64
    assert current_snapshot() is None


def test_sighup_reloads_and_a_file_edit_does_not(mcp_db):
    _write(mcp_db, _bytes(_document()))
    handle_sighup(signal.SIGHUP, None)
    assert mcp_status().epoch == 1
    _write(mcp_db, b"{")
    handle_sighup(signal.SIGHUP, None)
    assert _row(mcp_db)[1] == 1
    assert mcp_status().available is False
    _write(mcp_db, _bytes(_document(executable="/bin/echo")))
    before = _row(mcp_db)
    assert mcp_status().reason == "mismatch"
    assert current_snapshot() is None
    assert _row(mcp_db) == before
    entered = threading.Event()
    release = threading.Event()
    set_reload_fault("before_reread", lambda: entered.set() or release.wait(5))
    _write(mcp_db, _bytes(_document(executable="/usr/bin/true", roots=["/usr/bin"])))
    threads = [threading.Thread(target=lambda: handle_sighup(signal.SIGHUP, None)) for _ in range(2)]
    threads[0].start()
    assert entered.wait(5)
    threads[1].start()
    release.set()
    for thread in threads:
        thread.join(5)
    assert _row(mcp_db)[1] == 2


def test_reload_does_not_spawn_or_register_tools(mcp_db, client):
    _write(mcp_db, _bytes(_document()))

    def refuse(*_args, **_kwargs):
        raise AssertionError("An MCP process was started.")

    import subprocess

    original = subprocess.Popen
    subprocess.Popen = refuse
    try:
        assert reload_configuration().published is True
    finally:
        subprocess.Popen = original
    assert client.get("/api/v1/mcp").status_code == 404
    assert get_tool_registry().get("call_mcp_tool") is not None
    assert get_tool_registry().get("read_mcp_resource") is not None
    assert "call_mcp_tool" not in ALLOWED_TOOLS
    assert "read_mcp_resource" not in ALLOWED_TOOLS


def test_schema_15_upgrade_preserves_existing_rows(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()
    reset_database()
    init_database()
    with open_sqlite(path) as connection:
        connection.execute(
            """
            INSERT INTO agents (
                id, name, name_normalized, description, role, system_prompt, model,
                enabled, created_at, updated_at
            ) VALUES (
                'agent-1', 'Keeper', 'keeper', 'desc', 'role', 'prompt', 'model',
                1, '2026-01-01T00:00:00', '2026-01-01T00:00:00'
            )
            """
        )
        connection.execute("DELETE FROM schema_version")
        connection.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (14, '2026-01-01T00:00:00')"
        )
        connection.execute("DROP TABLE mcp_config_revisions")
    reset_database()
    init_database()
    with open_sqlite(path) as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_version ORDER BY version")]
        name = connection.execute("SELECT name FROM agents").fetchone()[0]
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'mcp_config_revisions'"
        ).fetchone()
        payload = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'mcp_execution_payloads'"
        ).fetchone()
    assert 14 in versions and 15 in versions and 16 in versions
    assert name == "Keeper"
    assert table is not None
    assert payload is not None
