"""WP-02 lifecycle remediation: stopping-session ownership and process accounting."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time

import pytest

from app.core.database import init_database
from app.mcp import loader
from app.mcp import session as mcp_session
from app.mcp.loader import reload_configuration, set_epoch_listener
from app.mcp.process import ProcessError, assess_executable
from app.mcp.session import (
    SessionError,
    allow_sessions,
    close_session,
    open_session,
    reset_clock,
    reset_test_timing,
    retire_older_epochs,
    set_spawn_override,
    set_test_timing,
    shutdown_all,
)
from tests.mcp_trust import TRUSTED_ALT_EXECUTABLE, TRUSTED_EXECUTABLE
from tests.test_mcp_session import FakeChild, _THREADS, _document, _open, _write_config


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("A process signal escaped the session.")

    monkeypatch.setattr(os, "killpg", refuse)
    monkeypatch.setattr(os, "fork", refuse)
    shutdown_all()
    allow_sessions()
    mcp_session._generations.clear()
    mcp_session._stopping.clear()
    mcp_session._sessions.clear()
    mcp_session._inflight.clear()
    reset_test_timing()
    reset_clock()
    set_test_timing(stdin_grace=0, term_grace=0)
    set_spawn_override(None)
    yield
    shutdown_all()
    allow_sessions()
    mcp_session._generations.clear()
    mcp_session._stopping.clear()
    mcp_session._sessions.clear()
    mcp_session._inflight.clear()
    set_epoch_listener(retire_older_epochs)
    reset_test_timing()
    reset_clock()
    set_spawn_override(None)
    loader._snapshot = None
    for thread in _THREADS:
        thread.join(timeout=2)
    _THREADS.clear()


@pytest.fixture
def published(database_file):
    init_database()
    document = _document()
    raw = json.dumps(document).encode()
    _write_config(database_file, raw)
    assert reload_configuration().published is True
    return database_file, raw


def _deny_reap(child: FakeChild, *, succeed_after: int | None = None):
    """Make termination fail until succeed_after reap attempts (None = forever)."""
    attempts = {"count": 0}

    def denied(signum: int) -> str:
        child.signals.append((child.pgid, signum))
        return "denied"

    def reap(*, wait_seconds=None):
        attempts["count"] += 1
        if succeed_after is not None and attempts["count"] >= succeed_after:
            child.reaped = True
            child._alive = False
            return True
        return False

    child.signal_group = denied  # type: ignore[method-assign]
    child.reap = reap  # type: ignore[method-assign]
    return attempts


def _dead_unreaped(child: FakeChild) -> None:
    """Child is no longer alive but waitpid/reap has not confirmed."""

    def gone(signum: int) -> str:
        child.signals.append((child.pgid, signum))
        child._alive = False
        return "gone"

    def reap(*, wait_seconds=None):
        return False

    child.signal_group = gone  # type: ignore[method-assign]
    child.reap = reap  # type: ignore[method-assign]


def test_child_remains_alive_after_failed_termination(published):
    session, child = _open(published)
    _deny_reap(child)
    close_session(session)
    assert child.alive() is True
    assert child.reaped is False
    assert session.state == "stopping"
    assert mcp_session._stopping.get("notes") is session
    assert "notes" not in mcp_session._sessions


def test_child_dead_but_unreaped_stays_tracked(published):
    session, child = _open(published)
    _dead_unreaped(child)
    close_session(session)
    assert child.alive() is False
    assert child.reaped is False
    assert session.state == "stopping"
    assert mcp_session._stopping.get("notes") is session


def test_session_remains_tracked_after_failed_close(published):
    session, child = _open(published)
    _deny_reap(child)
    close_session(session)
    assert session.state == "stopping"
    assert mcp_session._stopping["notes"] is session
    assert mcp_session._sessions.get("notes") is None


def test_repeated_close_eventually_reaps_and_clears_registry(published):
    session, child = _open(published)
    attempts = _deny_reap(child, succeed_after=2)
    close_session(session)
    assert session.state == "stopping"
    assert mcp_session._stopping.get("notes") is session
    close_session(session)
    assert session.state == "dead"
    assert child.reaped is True
    assert attempts["count"] == 2
    assert "notes" not in mcp_session._stopping
    assert "notes" not in mcp_session._sessions


def test_open_session_cannot_launch_while_old_child_is_stopping(published):
    session, child = _open(published)
    launches = {"count": 0}
    original = mcp_session._spawn_override

    def counting(server, facts, generation: int):
        launches["count"] += 1
        assert original is not None
        return original(server, facts, generation)

    _deny_reap(child)
    close_session(session)
    set_spawn_override(counting)
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "busy"
    assert launches["count"] == 0
    assert mcp_session._stopping.get("notes") is session
    assert child.reaped is False


def test_max_processes_includes_stopping_children(published):
    database, _raw = published
    _write_config(database, json.dumps(_document(servers=["notes", "mail", "chat"])).encode())
    assert reload_configuration().published is True
    set_test_timing(max_processes=2, stdin_grace=0, term_grace=0)

    first, child_a = _open(published, server_id="notes")
    second, _child_b = _open(published, server_id="mail")
    _deny_reap(child_a)
    close_session(first)
    assert mcp_session._stopping.get("notes") is first
    assert "mail" in mcp_session._sessions

    with pytest.raises(SessionError) as caught:
        open_session("chat")
    assert caught.value.code == "busy"
    assert second.state == "ready"


def test_shutdown_all_finds_registry_retained_stopping_sessions(published, caplog):
    caplog.set_level(logging.WARNING)
    session, child = _open(published)
    _deny_reap(child)
    close_session(session)
    assert mcp_session._stopping.get("notes") is session
    shutdown_all()
    assert mcp_session._stopping.get("notes") is session
    assert child.reaped is False
    assert any(
        "MCP unreaped child remains after shutdown_all" in record.message and "notes" in record.message
        for record in caplog.records
    )


def test_epoch_retirement_finds_stopping_sessions(published):
    session, child = _open(published)
    epoch = session.epoch
    _deny_reap(child)
    close_session(session)
    assert mcp_session._stopping.get("notes") is session

    calls: list[object] = []
    original = mcp_session.close_session

    def tracking(target):
        calls.append(target)
        return original(target)

    mcp_session.close_session = tracking  # type: ignore[assignment]
    try:
        retire_older_epochs(epoch + 1)
    finally:
        mcp_session.close_session = original  # type: ignore[assignment]
    assert session in calls
    assert mcp_session._stopping.get("notes") is session
    assert child.reaped is False


def test_failed_startup_with_unreaped_child_remains_tracked(published):
    """Failed bring-up before the reader starts must retain ownership via `_abandon`."""
    database, _raw = published
    holder: list[FakeChild] = []

    def launch(server, facts, generation: int) -> FakeChild:
        child = FakeChild(facts, generation)
        holder.append(child)
        _deny_reap(child)
        _write_config(database, json.dumps(_document(executable=TRUSTED_ALT_EXECUTABLE)).encode())
        assert reload_configuration().published is True
        return child

    set_spawn_override(launch)
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "mismatch"
    assert len(holder) == 1
    assert holder[0].reaped is False
    assert "notes" in mcp_session._stopping
    assert mcp_session._stopping["notes"].child is holder[0]
    assert "notes" not in mcp_session._sessions
    assert "notes" not in mcp_session._inflight

    with pytest.raises(SessionError) as again:
        open_session("notes")
    assert again.value.code == "busy"


def test_concurrent_open_and_close_no_duplicate_child(published):
    session, child = _open(published)
    _deny_reap(child)
    close_session(session)
    assert mcp_session._stopping.get("notes") is session

    barrier = threading.Barrier(3)
    launches: list[int] = []
    lock = threading.Lock()
    original = mcp_session._spawn_override
    errors: list[str] = []

    def counting(server, facts, generation: int):
        with lock:
            launches.append(generation)
        assert original is not None
        return original(server, facts, generation)

    set_spawn_override(counting)

    def closer():
        barrier.wait(2)
        close_session(session)

    def opener():
        barrier.wait(2)
        try:
            open_session("notes")
        except SessionError as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=closer), threading.Thread(target=opener)]
    for thread in threads:
        thread.start()
    barrier.wait(2)
    for thread in threads:
        thread.join(2)
        assert thread.is_alive() is False
    assert launches == []
    assert mcp_session._stopping.get("notes") is session
    assert errors == ["busy"]
    assert child.reaped is False


def test_concurrent_shutdown_all_and_close_session(published):
    session, child = _open(published)
    _deny_reap(child)
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def closer():
        barrier.wait(2)
        close_session(session)
        outcomes.append("close")

    def stopper():
        barrier.wait(2)
        shutdown_all()
        outcomes.append("shutdown")

    threads = [threading.Thread(target=closer), threading.Thread(target=stopper)]
    for thread in threads:
        thread.start()
    barrier.wait(2)
    for thread in threads:
        thread.join(2)
        assert thread.is_alive() is False
    assert set(outcomes) == {"close", "shutdown"}
    assert mcp_session._stopping.get("notes") is session
    assert child.reaped is False
    assert session.state == "stopping"


def test_no_duplicate_child_for_same_server_identity(published):
    session, child = _open(published)
    generation = session.generation
    _deny_reap(child)
    close_session(session)
    launches: list[int] = []

    def launch(server, facts, gen: int) -> FakeChild:
        launches.append(gen)
        return FakeChild(facts, gen)

    set_spawn_override(launch)
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "busy"
    assert launches == []
    assert session.generation == generation
    assert mcp_session._generations.get("notes") == generation


def test_retry_ownership_is_exclusive_and_bounded(published):
    session, child = _open(published)
    entered = threading.Event()
    release = threading.Event()
    reaps = {"count": 0}

    def denied(signum: int) -> str:
        child.signals.append((child.pgid, signum))
        return "denied"

    def reap(*, wait_seconds=None):
        reaps["count"] += 1
        entered.set()
        assert release.wait(2)
        return False

    child.signal_group = denied  # type: ignore[method-assign]
    child.reap = reap  # type: ignore[method-assign]

    first = threading.Thread(target=lambda: close_session(session))
    first.start()
    assert entered.wait(2)
    second = threading.Thread(target=lambda: close_session(session))
    second.start()
    time.sleep(0.05)
    assert reaps["count"] == 1
    assert session._closing is True
    release.set()
    first.join(2)
    second.join(2)
    assert first.is_alive() is False
    assert second.is_alive() is False
    assert reaps["count"] == 1
    assert session.state == "stopping"
    assert mcp_session._stopping.get("notes") is session
    assert session._closing is False


def test_registry_cleanup_only_after_confirmed_reap(published):
    session, child = _open(published)
    attempts = _deny_reap(child, succeed_after=3)
    close_session(session)
    assert "notes" in mcp_session._stopping
    close_session(session)
    assert "notes" in mcp_session._stopping
    assert child.reaped is False
    close_session(session)
    assert child.reaped is True
    assert attempts["count"] == 3
    assert "notes" not in mcp_session._stopping
    assert session.state == "dead"


def test_permanently_unresolved_survives_shutdown_without_false_cleanup(published, caplog):
    caplog.set_level(logging.WARNING)
    session, child = _open(published)
    _deny_reap(child)
    close_session(session)
    shutdown_all()
    allow_sessions()
    assert child.reaped is False
    assert mcp_session._stopping.get("notes") is session
    assert session.state == "stopping"
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "busy"
    assert "MCP unreaped child remains after shutdown_all" in caplog.text


# --- F9: _abandon exception-safe registry publication ---


def _session_with_child(published, *, generation: int = 1) -> tuple[mcp_session.McpSession, FakeChild]:
    """Build an unpublished startup session that still owns a launched child."""
    del published
    facts = assess_executable(TRUSTED_EXECUTABLE)
    child = FakeChild(facts, generation)
    session = mcp_session.McpSession("notes", "a" * 64, 1, generation, "b" * 64)
    session.child = child
    session.facts = facts
    return session, child


def _assert_unresolved_ownership(session: mcp_session.McpSession, child: FakeChild) -> None:
    assert mcp_session._stopping.get("notes") is session
    assert session.child is child
    assert child.reaped is False
    assert session.state == "stopping"
    assert session.state != "dead"
    assert "notes" not in mcp_session._sessions
    assert "notes" not in mcp_session._inflight


def test_abandon_oserror_before_signaling_retains_stopping(published, monkeypatch):
    session, child = _session_with_child(published)
    raised = {"hit": False}

    def boom(_session):
        raised["hit"] = True
        raise OSError(4, "Interrupted system call")

    monkeypatch.setattr(mcp_session, "_shutdown_child", boom)
    mcp_session._abandon(session)
    assert raised["hit"] is True
    _assert_unresolved_ownership(session, child)


def test_abandon_interrupted_error_before_signaling_retains_stopping(published, monkeypatch):
    session, child = _session_with_child(published)
    raised = {"hit": False}

    def boom(_session):
        raised["hit"] = True
        raise InterruptedError("waitpid interrupted before signal")

    monkeypatch.setattr(mcp_session, "_shutdown_child", boom)
    mcp_session._abandon(session)
    assert raised["hit"] is True
    _assert_unresolved_ownership(session, child)


def test_abandon_exception_after_signaling_before_reap_retains_stopping(published, monkeypatch):
    session, child = _session_with_child(published)
    signaled = {"term": False}

    def boom(target):
        outcome = target.child.signal_group(signal.SIGTERM)
        assert outcome == "delivered"
        signaled["term"] = True
        raise InterruptedError("interrupted after SIGTERM before reap")

    monkeypatch.setattr(mcp_session, "_shutdown_child", boom)
    mcp_session._abandon(session)
    assert signaled["term"] is True
    assert child.signals == [(child.pgid, signal.SIGTERM)]
    _assert_unresolved_ownership(session, child)


def test_abandon_alive_oserror_during_pre_signal_grace_retains_stopping(published):
    """Real `_shutdown_child` path: alive() raises before any signal_group call."""
    session, child = _session_with_child(published)
    probes = {"alive": 0}

    def exploding_alive():
        probes["alive"] += 1
        raise OSError("waitpid failed before signal")

    child.alive = exploding_alive  # type: ignore[method-assign]
    set_test_timing(stdin_grace=0.05, term_grace=0)
    mcp_session._abandon(session)
    assert probes["alive"] >= 1
    assert child.signals == []
    _assert_unresolved_ownership(session, child)


def test_abandon_exception_blocks_duplicate_open_and_counts_toward_max(published, monkeypatch):
    database, _raw = published
    _write_config(database, json.dumps(_document(servers=["notes", "mail"])).encode())
    assert reload_configuration().published is True
    set_test_timing(max_processes=1, stdin_grace=0, term_grace=0)

    session, child = _session_with_child(published)

    def boom(_session):
        raise OSError("shutdown exploded")

    # Leave the boom in place: open_session → close_session must still keep
    # ownership and return busy instead of leaking the OSError or spawning.
    monkeypatch.setattr(mcp_session, "_shutdown_child", boom)
    mcp_session._abandon(session)
    _assert_unresolved_ownership(session, child)

    launches: list[int] = []

    def launch(server, facts, generation: int) -> FakeChild:
        launches.append(generation)
        return FakeChild(facts, generation)

    set_spawn_override(launch)
    with pytest.raises(SessionError) as notes:
        open_session("notes")
    assert notes.value.code == "busy"
    with pytest.raises(SessionError) as mail:
        open_session("mail")
    assert mail.value.code == "busy"
    assert launches == []
    assert mcp_session._stopping.get("notes") is session
    assert session.child is child


def test_abandon_exception_shutdown_all_discovers_and_later_close_clears(published, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    session, child = _session_with_child(published)
    real_shutdown = mcp_session._shutdown_child

    def boom(_session):
        raise InterruptedError("abandon shutdown boom")

    monkeypatch.setattr(mcp_session, "_shutdown_child", boom)
    mcp_session._abandon(session)
    _assert_unresolved_ownership(session, child)

    shutdown_all()
    assert mcp_session._stopping.get("notes") is session
    assert any("MCP unreaped child remains after shutdown_all" in record.message for record in caplog.records)

    allow_sessions()
    monkeypatch.setattr(mcp_session, "_shutdown_child", real_shutdown)
    attempts = _deny_reap(child, succeed_after=1)
    close_session(session)
    assert attempts["count"] == 1
    assert child.reaped is True
    assert session.state == "dead"
    assert "notes" not in mcp_session._stopping


def test_bring_up_preserves_startup_error_when_abandon_shutdown_raises(published, monkeypatch):
    """`_abandon` must retain ownership without replacing the original SessionError."""
    database, _raw = published
    holder: list[FakeChild] = []
    real_shutdown = mcp_session._shutdown_child

    def launch(server, facts, generation: int) -> FakeChild:
        child = FakeChild(facts, generation)
        holder.append(child)
        _write_config(database, json.dumps(_document(executable=TRUSTED_ALT_EXECUTABLE)).encode())
        assert reload_configuration().published is True
        return child

    def boom(_session):
        raise OSError("abandon path exploded")

    set_spawn_override(launch)
    monkeypatch.setattr(mcp_session, "_shutdown_child", boom)
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "mismatch"
    assert len(holder) == 1
    assert mcp_session._stopping["notes"].child is holder[0]
    assert holder[0].reaped is False

    monkeypatch.setattr(mcp_session, "_shutdown_child", real_shutdown)
    _deny_reap(holder[0])
    with pytest.raises(SessionError) as again:
        open_session("notes")
    assert again.value.code == "busy"


def test_unreaped_spawn_failure_is_tracked_and_blocks_admission(published):
    """ProcessError.unreaped is adopted into `_stopping` and blocks a new generation."""
    holder: list[FakeChild] = []

    def launch(server, facts, generation: int):
        child = FakeChild(facts, generation)
        _deny_reap(child)
        holder.append(child)
        raise ProcessError("spawn_failed", unreaped=child)

    set_spawn_override(launch)
    set_test_timing(max_processes=2, stdin_grace=0, term_grace=0)
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "startup_failed"
    assert len(holder) == 1
    assert mcp_session._stopping.get("notes") is not None
    assert mcp_session._stopping["notes"].child is holder[0]
    assert holder[0].reaped is False

    with pytest.raises(SessionError) as blocked:
        open_session("notes")
    assert blocked.value.code == "busy"


def test_unreaped_spawn_failure_counts_toward_max_processes(published):
    database, _raw = published
    _write_config(database, json.dumps(_document(servers=["notes", "mail"])).encode())
    assert reload_configuration().published is True
    holder: list[FakeChild] = []

    def launch(server, facts, generation: int):
        child = FakeChild(facts, generation)
        _deny_reap(child)
        holder.append(child)
        raise ProcessError("spawn_failed", unreaped=child)

    set_spawn_override(launch)
    set_test_timing(max_processes=1, stdin_grace=0, term_grace=0)
    with pytest.raises(SessionError):
        open_session("notes")
    assert mcp_session._stopping.get("notes") is not None
    with pytest.raises(SessionError) as caught:
        open_session("mail")
    assert caught.value.code == "busy"
    assert holder[0].reaped is False


def test_shutdown_all_inflight_timeout_reports_and_retains_ownership(published, caplog, monkeypatch):
    """When inflight outlives the drain wait, shutdown must not claim a clean slate."""
    entered = threading.Event()
    hold = threading.Event()
    holder: list[FakeChild] = []

    def launch(server, facts, generation: int) -> FakeChild:
        child = FakeChild(facts, generation)
        holder.append(child)
        entered.set()
        assert hold.wait(timeout=5)
        return child

    set_spawn_override(launch)
    monkeypatch.setattr(mcp_session, "_inflight_drain_seconds", lambda: 0.0)
    outcome: dict[str, str] = {}

    def opener() -> None:
        try:
            open_session("notes")
        except SessionError as exc:
            outcome["code"] = exc.code

    worker = threading.Thread(target=opener)
    worker.start()
    assert entered.wait(2)
    _deny_reap(holder[0])
    with caplog.at_level(logging.WARNING):
        shutdown_all()
    assert any("MCP inflight startup remains after shutdown_all" in record.message for record in caplog.records)
    assert "notes" in mcp_session._inflight
    hold.set()
    worker.join(2)
    assert worker.is_alive() is False
    assert outcome.get("code") == "shutting_down"
    assert mcp_session._stopping.get("notes") is not None
    assert mcp_session._stopping["notes"].child is holder[0]
    assert holder[0].reaped is False

    allow_sessions()
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "busy"
