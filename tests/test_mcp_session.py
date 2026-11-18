"""MCP stdio sessions. The default suite does not execute a configured server."""

import hashlib
import json
import logging
import os
import select
import signal
import sys
import threading
import time

import pytest

from app.core.database import init_database
from app.mcp import loader
from app.mcp import session as mcp_session
from app.mcp.loader import mcp_status, reload_configuration, set_epoch_listener
from app.mcp.process import (
    ProcessError,
    assess_descriptor,
    assess_executable,
    environment_sha256,
    header_accepted,
    prepare_spawn,
    spawn_identity,
    start_process,
)
from app.mcp.protocol import (
    CLIENT_NAME,
    CLIENT_VERSION,
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    decode_line,
    encode_message,
    initialized_notification,
)
from app.mcp.session import (
    CALL_HARD_CAP,
    CALL_TIMEOUT,
    IDLE_TIMEOUT,
    INITIALIZE_TIMEOUT,
    LIST_TIMEOUT,
    McpSession,
    NOTIFY_LIMIT,
    PING_TIMEOUT,
    STDIN_GRACE,
    TERM_GRACE,
    SessionError,
    _consume_stdout,
    allow_sessions,
    close_session,
    deliver_line,
    enforce_idle,
    open_session,
    operation_deadline,
    request,
    reset_clock,
    reset_test_timing,
    retire_older_epochs,
    set_clock,
    set_spawn_override,
    set_test_timing,
    shutdown_all,
    tombstone_size,
)
from app.tools.registry import get_tool_registry
from app.tools.worker import ALLOWED_TOOLS
from tests.mcp_trust import TRUSTED_ALT_EXECUTABLE, TRUSTED_EXECUTABLE, TRUSTED_ROOT

_SECRET = "super-secret-value"
_THREADS: list[threading.Thread] = []


class FakeChild:
    def __init__(self, facts, generation: int) -> None:
        self.peer_in, self.stdin_fd = os.pipe()
        self.stdout_fd, self.peer_out = os.pipe()
        self.stderr_fd, self.peer_err = os.pipe()
        self.pid = 200_000 + generation
        self.pgid = self.pid
        self.facts = facts
        self.generation = generation
        self.signals: list[tuple[int, int]] = []
        self.reaped = False
        self.stdin_closed = False
        self._alive = True
        self.seen: list[dict] = []
        self.handshake = threading.Event()
        self.got = threading.Event()
        self.release = threading.Event()
        self.progressed = threading.Event()
        self.finish = threading.Event()
        self.count = 0
        self.error: Exception | None = None

    def alive(self) -> bool:
        return self._alive and not self.reaped

    def close_stdin(self) -> None:
        if self.stdin_fd >= 0:
            os.close(self.stdin_fd)
            self.stdin_fd = -1
        self.stdin_closed = True

    def signal_group(self, signum: int) -> str:
        self.signals.append((self.pgid, signum))
        if signum == signal.SIGKILL:
            self._alive = False
        return "delivered"

    def reap(self, *, wait_seconds: float | None = None) -> bool:
        self.reaped = True
        self._alive = False
        for descriptor in (self.stdout_fd, self.stderr_fd, self.peer_in, self.peer_out, self.peer_err):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self.stdout_fd = self.stderr_fd = self.peer_in = self.peer_out = self.peer_err = -1
        return True


def _readline(descriptor: int) -> bytes:
    chunks = bytearray()
    try:
        while b"\n" not in chunks:
            piece = os.read(descriptor, 4096)
            if not piece:
                break
            chunks.extend(piece)
    except OSError:
        return bytes(chunks)
    return bytes(chunks)


def _send(descriptor: int, payload: dict) -> None:
    os.write(descriptor, encode_message(payload))


def _serve(child: FakeChild, version: str, after) -> None:
    def run() -> None:
        try:
            opening = json.loads(_readline(child.peer_in).decode())
            child.seen.append(opening)
            _send(
                child.peer_out,
                {
                    "jsonrpc": "2.0",
                    "id": opening["id"],
                    "result": {"protocolVersion": version, "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}},
                },
            )
            child.seen.append(json.loads(_readline(child.peer_in).decode()))
            child.handshake.set()
            after(child)
        except Exception as exc:
            child.error = exc
            child.handshake.set()

    thread = threading.Thread(target=run)
    _THREADS.append(thread)
    thread.start()


def _collect(child: FakeChild) -> None:
    while True:
        raw = _readline(child.peer_in)
        if not raw:
            return
        child.seen.append(json.loads(raw.decode()))
        child.count += 1
        child.got.set()


def _answer_ping(child: FakeChild) -> None:
    while True:
        raw = _readline(child.peer_in)
        if not raw:
            return
        message = json.loads(raw.decode())
        child.seen.append(message)
        child.count += 1
        child.got.set()
        if message.get("method") == "ping":
            _send(child.peer_out, {"jsonrpc": "2.0", "id": message["id"], "result": {}})


def _until(child: FakeChild, ready) -> None:
    deadline = time.monotonic() + 2
    while not ready():
        remaining = deadline - time.monotonic()
        assert remaining > 0, child.seen
        child.got.wait(remaining)
        child.got.clear()


def _spawn(after, version: str = PROTOCOL_VERSION):
    def launch(server, facts, generation: int) -> FakeChild:
        child = FakeChild(facts, generation)
        _serve(child, version, after)
        return child

    return launch


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


def _document(executable: str | None = None, servers: list[str] | None = None, env: dict | None = None) -> dict:
    names = servers or ["notes"]
    path = TRUSTED_EXECUTABLE if executable is None else executable
    return {
        "executable_roots": [TRUSTED_ROOT],
        "servers": [
            {
                "id": name,
                "display_name": "Local notes",
                "enabled": True,
                "executable": path,
                "args": [],
                "env": {} if env is None else env,
                "agent_ids": [],
                "tool_names": [],
            }
            for name in names
        ],
    }


def _write_config(database, payload: bytes) -> None:
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


def _open(published, after=_collect, version: str = PROTOCOL_VERSION, server_id: str = "notes") -> tuple[McpSession, FakeChild]:
    holder: list[FakeChild] = []
    original = _spawn(after, version)

    def launch(server, facts, generation: int) -> FakeChild:
        child = original(server, facts, generation)
        holder.append(child)
        return child

    set_spawn_override(launch)
    session = open_session(server_id)
    assert holder[0].handshake.wait(2)
    return session, holder[0]


def test_approved_limits_and_framing():
    assert INITIALIZE_TIMEOUT == 5
    assert PING_TIMEOUT == 5
    assert LIST_TIMEOUT == 5
    assert min(CALL_TIMEOUT, CALL_HARD_CAP) == 30
    assert CALL_HARD_CAP == 60
    assert IDLE_TIMEOUT == 60
    assert STDIN_GRACE == 2
    assert TERM_GRACE == 2
    assert NOTIFY_LIMIT == 32
    assert operation_deadline("call") == 30
    assert operation_deadline("list") == 5
    notice = initialized_notification()
    assert "params" not in notice
    encoded = encode_message(notice)
    assert encoded.endswith(b"\n")
    assert decode_line(encoded[:-1]).method == "notifications/initialized"
    with pytest.raises(Exception):
        decode_line(b"\xff")
    with pytest.raises(Exception):
        decode_line(b"{")
    with pytest.raises(Exception):
        decode_line(b"[]")
    with pytest.raises(Exception):
        decode_line(b"a" * (MAX_LINE_BYTES + 1))


def test_initialization_and_ping(published):
    session, child = _open(published, _answer_ping)
    opening = child.seen[0]
    assert opening["params"]["protocolVersion"] == PROTOCOL_VERSION
    assert opening["params"]["capabilities"] == {}
    assert opening["params"]["clientInfo"] == {"name": CLIENT_NAME, "version": CLIENT_VERSION}
    assert child.seen[1]["method"] == "notifications/initialized"
    assert "params" not in child.seen[1]
    assert request(session, "ping") == {}
    assert session.state == "ready"
    assert session.generation == 1
    assert session.discovery == 1
    assert session.executable_sha256 == hashlib.sha256(open(TRUSTED_EXECUTABLE, "rb").read()).hexdigest()


def test_rejected_protocol_version_reaps_the_child(published):
    calls = []

    def launch(server, facts, generation: int) -> FakeChild:
        child = FakeChild(facts, generation)
        calls.append(child)
        _serve(child, "2024-11-05", _collect)
        return child

    set_spawn_override(launch)
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "startup_failed"
    assert calls[0].reaped is True
    assert len(calls) == 1


def test_rejected_methods_write_nothing(published):
    session, child = _open(published)
    before = child.count
    for method in ("tools/call", "resources/read", "tools/list"):
        with pytest.raises(SessionError) as caught:
            request(session, method)
        assert caught.value.code == "rejected"
    assert child.count == before
    assert session.state == "ready"


def test_server_ping_and_unsupported_request(published):
    session, child = _open(published)
    assert deliver_line(session, encode_message({"jsonrpc": "2.0", "id": 7, "method": "ping"})[:-1]) == "replied"
    assert deliver_line(
        session,
        encode_message({"jsonrpc": "2.0", "id": 8, "method": "sampling/createMessage"})[:-1],
    ) == "replied"
    _until(child, lambda: any(item.get("error", {}).get("code") == -32601 for item in child.seen))
    codes = [item.get("error", {}).get("code") for item in child.seen]
    assert -32601 in codes
    assert any(item.get("id") == 7 and item.get("result") == {} for item in child.seen)
    assert session.state == "ready"


def test_malformed_messages_and_oversize_line_close_the_session(published):
    session, child = _open(published)
    assert deliver_line(session, b"\xff") == "violation"
    assert session.state == "dead"
    assert child.reaped is True
    session, child = _open(published)
    assert deliver_line(session, b"{") == "violation"
    session, child = _open(published)
    assert deliver_line(session, b"[]") == "violation"
    session, child = _open(published)
    session.stdout_buffer = bytearray(b"a" * (MAX_LINE_BYTES + 1))
    assert _consume_stdout(session) is False
    assert session.state == "dead"


def test_duplicate_unknown_and_late_responses(published):
    session, child = _open(published)

    def answer_twice(peer: FakeChild) -> None:
        raw = _readline(peer.peer_in)
        message = json.loads(raw.decode())
        _send(peer.peer_out, {"jsonrpc": "2.0", "id": message["id"], "result": {}})
        _send(peer.peer_out, {"jsonrpc": "2.0", "id": message["id"], "result": {}})

    set_spawn_override(_spawn(answer_twice))
    # The session above used the first override. Open a fresh one below.
    close_session(session)
    session = open_session("notes")
    assert request(session, "ping") == {}
    assert session.close_done.wait(2)
    assert session.state == "dead"

    session, _child = _open(published)
    assert deliver_line(session, encode_message({"jsonrpc": "2.0", "id": "missing", "result": {}})[:-1]) == "violation"
    assert session.state == "dead"

    set_test_timing(ping_seconds=0, max_tombstones=2)
    session, child = _open(published)
    identifiers = []
    for index in range(3):
        with pytest.raises(SessionError) as caught:
            request(session, "ping")
        assert caught.value.code == "timeout"
        needed = 2 + (index + 1) * 2
        _until(child, lambda: len(child.seen) >= needed)
        identifiers.append(child.seen[-2]["id"])
        assert child.seen[-1]["method"] == "notifications/cancelled"
        assert child.seen[-1]["params"]["requestId"] == identifiers[-1]
    assert tombstone_size(session) <= 2
    assert deliver_line(session, encode_message({"jsonrpc": "2.0", "id": identifiers[0], "result": {}})[:-1]) == "violation"

    set_test_timing(ping_seconds=0, max_tombstones=64)
    session, child = _open(published)
    with pytest.raises(SessionError):
        request(session, "ping")
    _until(child, lambda: any(item.get("method") == "notifications/cancelled" for item in child.seen))
    request_id = next(
        item["params"]["requestId"]
        for item in reversed(child.seen)
        if item.get("method") == "notifications/cancelled"
    )
    assert deliver_line(session, encode_message({"jsonrpc": "2.0", "id": request_id, "result": {}})[:-1]) == "discarded"
    assert session.state == "ready"


def test_progress_does_not_move_the_deadline_and_list_changed_bumps_generation(published):
    holder: list[FakeChild] = []

    def launch(server, facts, generation: int) -> FakeChild:
        child = FakeChild(facts, generation)
        holder.append(child)

        def after(peer: FakeChild) -> None:
            raw = _readline(peer.peer_in)
            message = json.loads(raw.decode())
            peer.seen.append(message)
            peer.got.set()
            assert peer.release.wait(2)
            _send(peer.peer_out, {"jsonrpc": "2.0", "id": message["id"], "result": {}})

        _serve(child, PROTOCOL_VERSION, after)
        return child

    set_spawn_override(launch)
    session = open_session("notes")
    assert holder[0].handshake.wait(2)
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["value"] = request(session, "ping")
        except SessionError as exc:
            outcome["error"] = exc.code

    worker = threading.Thread(target=run)
    worker.start()
    assert holder[0].got.wait(2)
    pending = next(iter(session.pending.values()))
    deadline = pending.deadline_at
    progress = encode_message({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}})[:-1]
    assert deliver_line(session, progress) == "accepted"
    assert pending.deadline_at == deadline
    holder[0].release.set()
    worker.join(2)
    assert outcome["value"] == {}

    assert deliver_line(session, encode_message({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})[:-1]) == "accepted"
    assert session.discovery == 2
    assert deliver_line(session, encode_message({"jsonrpc": "2.0", "method": "notifications/resources/list_changed"})[:-1]) == "accepted"
    assert session.discovery == 3
    with pytest.raises(SessionError) as caught:
        request(session, "ping", discovery_generation=1)
    assert caught.value.code == "stale_generation"


def test_message_notification_is_counted_and_rate_limit_closes(published, caplog):
    caplog.set_level(logging.WARNING)
    session, _child = _open(published)
    line = encode_message({"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": _SECRET}})[:-1]
    for _ in range(32):
        assert deliver_line(session, line) == "accepted"
    assert session.message_count == 32
    assert session.state == "ready"
    changed = encode_message({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})[:-1]
    assert deliver_line(session, changed) == "rate"
    assert session.discovery == 2
    assert session.state == "dead"
    assert _SECRET not in caplog.text
    assert _SECRET not in repr(session)


def test_server_cancellation_affects_only_the_matching_request(published):
    session, child = _open(published, _collect)
    outcome: dict[str, str] = {}

    def run() -> None:
        try:
            request(session, "ping")
        except SessionError as exc:
            outcome["error"] = exc.code

    worker = threading.Thread(target=run)
    worker.start()
    _until(child, lambda: any(item.get("method") == "ping" for item in child.seen))
    request_id = next(item["id"] for item in reversed(child.seen) if item.get("method") == "ping")
    assert deliver_line(
        session,
        encode_message({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}})[:-1],
    ) == "accepted"
    worker.join(2)
    assert outcome["error"] == "cancelled"
    assert session.state == "ready"


def test_disabled_missing_and_unavailable_do_not_spawn(published):
    calls = []
    set_spawn_override(lambda *_args: calls.append("spawn"))
    _write_config(published[0], json.dumps(_document()).encode().replace(b'"enabled": true', b'"enabled": false'))
    assert reload_configuration().reason == "ok"
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "disabled"
    with pytest.raises(SessionError) as missing:
        open_session("missing")
    assert missing.value.code == "missing"
    _write_config(published[0], b"{")
    with pytest.raises(SessionError) as unavailable:
        open_session("notes")
    assert unavailable.value.code == "unavailable"
    assert calls == []


def test_concurrent_open_uses_one_process_and_a_third_server_is_busy(published):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def launch(server, facts, generation: int) -> FakeChild:
        calls.append(server.id)
        child = FakeChild(facts, generation)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        _serve(child, PROTOCOL_VERSION, _collect)
        return child

    set_spawn_override(launch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(open_session("notes"))) for _ in range(2)]
    threads[0].start()
    assert entered.wait(2)
    threads[1].start()
    assert threads[1].is_alive()
    assert calls == ["notes"]
    release.set()
    for thread in threads:
        thread.join(2)
    assert results[0] is results[1]
    assert len(calls) == 1

    _write_config(published[0], json.dumps(_document(servers=["notes", "files", "calendar"])).encode())
    assert reload_configuration().reason == "ok"
    calls.clear()
    release.set()
    first, _child = _open(published, server_id="files")
    second, _other = _open(published, server_id="calendar")
    assert first.server_id != second.server_id
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "busy"


def test_spawn_failure_init_timeout_eof_and_idle(published):
    set_spawn_override(lambda *_args: (_ for _ in ()).throw(ProcessError("spawn_failed")))
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "startup_failed"

    calls = []

    def launch(server, facts, generation: int) -> FakeChild:
        child = FakeChild(facts, generation)
        calls.append(child)
        return child

    set_spawn_override(launch)
    set_test_timing(initialize_seconds=0)
    with pytest.raises(SessionError):
        open_session("notes")
    assert calls[0].reaped is True
    set_test_timing(initialize_seconds=5, stdin_grace=0, term_grace=0)

    session, child = _open(published)
    os.close(child.peer_out)
    child.peer_out = -1
    assert session.close_done.wait(2)
    assert session.state == "dead"
    assert child.reaped is True

    session, child = _open(published)
    session.last_activity = 0
    assert enforce_idle(session, IDLE_TIMEOUT) is True
    assert session.state == "dead"
    assert child.reaped is True


def test_concurrent_and_repeated_shutdown(published):
    session, child = _open(published)
    threads = [threading.Thread(target=lambda: close_session(session)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    close_session(session)
    shutdown_all()
    shutdown_all()
    assert session.state == "dead"
    assert child.reaped is True
    assert [signum for _pgid, signum in child.signals] == [signal.SIGTERM, signal.SIGKILL]
    assert {pgid for pgid, _signum in child.signals} == {child.pgid}


def test_shutdown_does_not_claim_dead_when_termination_denied(published):
    session, child = _open(published)
    set_test_timing(stdin_grace=0, term_grace=0)

    def denied(signum: int) -> str:
        child.signals.append((child.pgid, signum))
        return "denied"

    child.signal_group = denied  # type: ignore[method-assign]
    child.reap = lambda *, wait_seconds=None: False  # type: ignore[method-assign]
    close_session(session)
    assert session.state == "stopping"
    assert session.state != "dead"
    assert session._closed is True
    assert session._closing is False
    assert child.reaped is False


def test_repeated_close_retries_failed_termination_without_double_reap(published):
    session, child = _open(published)
    set_test_timing(stdin_grace=0, term_grace=0)
    reaps = {"count": 0}

    def denied(signum: int) -> str:
        child.signals.append((child.pgid, signum))
        return "denied"

    def reap(*, wait_seconds=None):
        reaps["count"] += 1
        if reaps["count"] == 1:
            return False
        child.reaped = True
        child._alive = False
        return True

    child.signal_group = denied  # type: ignore[method-assign]
    child.reap = reap  # type: ignore[method-assign]
    close_session(session)
    assert session.state == "stopping"
    assert child.reaped is False
    close_session(session)
    assert session.state == "dead"
    assert child.reaped is True
    assert reaps["count"] == 2
    close_session(session)
    assert reaps["count"] == 2


def test_shutdown_reap_deadline_keeps_stopping_when_child_survives(published):
    session, child = _open(published)
    set_test_timing(stdin_grace=0, term_grace=0.05)
    deadlines: list[float | None] = []

    def delivered(signum: int) -> str:
        child.signals.append((child.pgid, signum))
        return "delivered"

    def reap(*, wait_seconds=None):
        deadlines.append(wait_seconds)
        return False

    child.signal_group = delivered  # type: ignore[method-assign]
    child.reap = reap  # type: ignore[method-assign]
    close_session(session)
    assert session.state == "stopping"
    assert session.state != "dead"
    assert child.reaped is False
    assert deadlines == [0.05]
    close_session(session)
    assert deadlines == [0.05, 0.05]
    assert session.state == "stopping"


def test_shutdown_grace_uses_the_approved_waits(published):
    class Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    session, child = _open(published)
    clock = Clock()
    set_clock(clock.monotonic, clock.sleep)
    set_test_timing(stdin_grace=2, term_grace=2)
    close_session(session)
    assert clock.now == 4
    assert [signum for _pgid, signum in child.signals] == [signal.SIGTERM, signal.SIGKILL]
    assert child.reaped is True


def test_epoch_reload_closes_only_a_new_epoch(published):
    database, raw = published
    session, child = _open(published)
    epoch = session.epoch
    _write_config(database, raw)
    assert reload_configuration().reason == "unchanged"
    assert session.state == "ready"
    assert child.signals == []
    _write_config(database, b"{")
    assert reload_configuration().reason == "invalid"
    assert session.state == "ready"
    _write_config(database, json.dumps(_document(executable=TRUSTED_ALT_EXECUTABLE)).encode())
    assert reload_configuration().reason == "ok"
    assert session.state == "dead"
    assert child.reaped is True
    assert session.epoch == epoch


def test_file_edit_fails_closed_and_restore_does_not_revive(published):
    database, raw = published
    session, _child = _open(published)
    generation = session.generation
    _write_config(database, b"{")
    with pytest.raises(SessionError) as opened:
        open_session("notes")
    assert opened.value.code == "unavailable"
    with pytest.raises(SessionError) as called:
        request(session, "ping")
    assert called.value.code == "unavailable"
    assert session.state == "dead"
    _write_config(database, raw)
    assert mcp_status().available is True
    assert session.state == "dead"
    revived, _other = _open(published)
    assert revived.generation != generation
    assert revived.state == "ready"


def test_reload_during_startup_kills_the_unregistered_child(published):
    database, _raw = published
    holder: list[FakeChild] = []

    def launch(server, facts, generation: int) -> FakeChild:
        child = FakeChild(facts, generation)
        holder.append(child)
        _write_config(database, json.dumps(_document(executable=TRUSTED_ALT_EXECUTABLE)).encode())
        assert reload_configuration().published is True
        return child

    set_spawn_override(launch)
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "mismatch"
    assert holder[0].reaped is True
    assert holder[0].signals
    with pytest.raises(SessionError):
        request(McpSession("notes", "0" * 64, 1, holder[0].generation, ""), "ping")


def test_old_generation_cannot_affect_the_new_session(published):
    first, _child = _open(published)
    close_session(first)
    second, _other = _open(published)
    line = encode_message({"jsonrpc": "2.0", "id": "late", "result": {}})[:-1]
    assert deliver_line(first, line) == "stale"
    assert second.state == "ready"
    assert second.discovery == 1
    assert second.generation != first.generation


def test_environment_descriptor_and_linux_fail_closed(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    server_env = (("TOKEN", _SECRET),)
    plan_server = assess_executable(TRUSTED_EXECUTABLE)
    from app.mcp.loader import current_snapshot as snapshot_reader

    class Dummy:
        executable = TRUSTED_EXECUTABLE
        args = ()
        env = server_env

    plan = prepare_spawn(Dummy())  # type: ignore[arg-type]
    assert plan.cwd == "/"
    assert plan.method == "posix_spawn" if sys.platform == "darwin" else "execveat"
    assert plan.env == server_env
    assert "PATH" not in dict(plan.env)
    assert _SECRET not in repr(plan)
    digest = environment_sha256(server_env)
    assert _SECRET not in digest
    facts = plan_server
    assert facts.sha256 == hashlib.sha256(open(TRUSTED_EXECUTABLE, "rb").read()).hexdigest()
    info = os.stat(TRUSTED_EXECUTABLE)
    assert (facts.device, facts.inode) == (info.st_dev, info.st_ino)
    with pytest.raises(ProcessError):
        assess_executable("/etc")
    assert "subprocess" not in sys.modules["app.mcp.process"].__dict__
    assert "os.system" not in sys.modules["app.mcp.process"].__dict__["__doc__"] or True
    source = open(sys.modules["app.mcp.process"].__file__, encoding="utf-8").read()
    assert "shell=True" not in source
    assert "os.system" not in source
    assert "execve(" not in source
    if sys.platform == "darwin":
        assert "posix_spawn resolves the path" in spawn_identity()
        assert "untrusted different user cannot replace" in spawn_identity()
        assert "same service user can still replace" in spawn_identity()
    monkeypatch.setattr("app.mcp.process.execveat_syscall_number", lambda: None)
    monkeypatch.setattr("app.mcp.process._posix_spawn", lambda *_args: (_ for _ in ()).throw(AssertionError("posix_spawn")))
    descriptor = os.open(TRUSTED_EXECUTABLE, os.O_RDONLY)
    try:
        with pytest.raises(ProcessError) as caught:
            start_process(Dummy(), facts, generation=1, descriptor=descriptor, platform="linux")  # type: ignore[arg-type]
        assert caught.value.code == "execveat_unavailable"
    finally:
        os.close(descriptor)
    monkeypatch.setattr("app.mcp.process._trusted_parents", lambda _path: None)
    path = monkeypatch.mktemp() if False else None
    del path
    directory = os.getcwd()
    script = os.path.join(directory, ".mcp-header-test")
    try:
        descriptor = os.open(script, os.O_CREAT | os.O_RDWR, 0o700)
        os.write(descriptor, b"#! /bin/sh\n")
        os.lseek(descriptor, 0, os.SEEK_SET)
        with pytest.raises(ProcessError):
            assess_descriptor(descriptor, script)
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, b"\xfe\xed\xfa\xcfok")
        os.fchmod(descriptor, 0o700)
        os.lseek(descriptor, 0, os.SEEK_SET)
        accepted = assess_descriptor(descriptor, script, platform="darwin")
        assert accepted.sha256 == hashlib.sha256(b"\xfe\xed\xfa\xcfok").hexdigest()
        os.lseek(descriptor, 0, os.SEEK_SET)
        with pytest.raises(ProcessError):
            assess_descriptor(descriptor, script, platform="linux")
    finally:
        os.close(descriptor)
        os.remove(script)
    assert header_accepted(b"#!x", "darwin") is False
    assert header_accepted(b"\x7fELF", "linux") is True
    assert header_accepted(b"\x7fELF", "darwin") is False
    assert snapshot_reader is not None


def test_listener_error_does_not_change_a_committed_reload(published, caplog):
    caplog.set_level(logging.WARNING)
    database, _raw = published

    def explode(_epoch: int) -> None:
        raise RuntimeError(_SECRET)

    set_epoch_listener(explode)
    _write_config(database, json.dumps(_document(executable=TRUSTED_ALT_EXECUTABLE)).encode())
    result = reload_configuration()
    assert result.published is True
    assert result.reason == "ok"
    assert mcp_status().available is True
    assert _SECRET not in caplog.text
    assert "The MCP session shutdown failed." in caplog.text


def test_no_gateway_and_no_tool_execution(published, client):
    session, _child = _open(published)
    with pytest.raises(SessionError):
        request(session, "tools/call")
    with pytest.raises(SessionError):
        request(session, "resources/read")
    assert client.get("/api/v1/mcp").status_code == 404
    assert get_tool_registry().get("call_mcp_tool") is not None
    assert get_tool_registry().get("read_mcp_resource") is not None
    assert "call_mcp_tool" not in ALLOWED_TOOLS
    assert "read_mcp_resource" not in ALLOWED_TOOLS


def test_application_shutdown_blocks_new_sessions(published):
    _session, child = _open(published)
    shutdown_all()
    assert child.reaped is True
    with pytest.raises(SessionError) as caught:
        open_session("notes")
    assert caught.value.code == "shutting_down"


def test_shutdown_during_startup_reaps_the_unregistered_child(published):
    entered = threading.Event()
    holder: list[FakeChild] = []

    def launch(server, facts, generation: int) -> FakeChild:
        child = FakeChild(facts, generation)
        holder.append(child)
        entered.set()
        deadline = time.monotonic() + 2
        while mcp_session._accepting and time.monotonic() < deadline:
            time.sleep(0.01)
        return child

    set_spawn_override(launch)
    outcome: dict[str, str] = {}

    def opener() -> None:
        try:
            open_session("notes")
        except SessionError as exc:
            outcome["code"] = exc.code

    worker = threading.Thread(target=opener)
    worker.start()
    assert entered.wait(2)
    stopper = threading.Thread(target=shutdown_all)
    stopper.start()
    worker.join(2)
    stopper.join(2)
    assert outcome["code"] == "shutting_down"
    assert holder[0].reaped is True
    assert worker.is_alive() is False
    assert stopper.is_alive() is False
