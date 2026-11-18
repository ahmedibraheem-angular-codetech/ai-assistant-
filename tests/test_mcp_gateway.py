"""Milestone 4 gateway races and security checks. No real MCP server is started."""

import json
import os
import stat
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.approvals.models import ApprovalRequestRecord
from app.approvals.payloads import McpExecutionPayloadRecord
from app.approvals.service import (
    ApprovalConflict,
    ApprovalNotReviewable,
    ApprovalService,
    set_after_pin,
    set_before_pin,
    set_commit_fault,
)
from app.core.database import get_engine, init_database
from app.mcp import loader
from app.mcp.gateway import set_before_permit, set_before_write
from app.mcp.loader import reload_configuration, set_permit_hook, set_reload_fault
from app.mcp.payload import (
    PayloadError,
    _accepted,
    cleanup_abandoned_payload_keys,
    load_payload_key,
    set_stop_before_link,
)
from app.mcp.protocol import PROTOCOL_VERSION, encode_message
from app.mcp import session as mcp_session
from app.mcp.session import (
    SessionError,
    allow_sessions,
    deliver_line,
    open_session,
    published_session,
    request,
    reset_test_timing,
    set_spawn_override,
    set_test_timing,
    shutdown_all,
)
from app.mcp.session import retire_older_epochs, set_epoch_listener
from app.tools.executor import ToolExecutor
from app.tools.registry import get_tool_registry
from app.tools.worker import ALLOWED_TOOLS
from tests.mcp_trust import TRUSTED_ALT_EXECUTABLE, TRUSTED_EXECUTABLE, TRUSTED_ROOT
from tests.test_agents import create_agent
from tests.test_tools import grant

_SECRET = "super-secret-value"
_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 40},
        "api_key": {"type": "string", "minLength": 1, "maxLength": 80},
    },
}


class FakeChild:
    def __init__(self, facts, generation: int) -> None:
        self.peer_in, self.stdin_fd = os.pipe()
        self.stdout_fd, self.peer_out = os.pipe()
        self.stderr_fd, self.peer_err = os.pipe()
        self.pid = 200_000 + generation
        self.pgid = self.pid
        self.facts = facts
        self.generation = generation
        self.signals = []
        self.reaped = False
        self.stdin_closed = False
        self._alive = True
        self.seen = []
        self.handshake = threading.Event()
        self.calls = []

    def alive(self) -> bool:
        return self._alive and not self.reaped

    def close_stdin(self) -> None:
        if self.stdin_fd >= 0:
            os.close(self.stdin_fd)
            self.stdin_fd = -1
        self.stdin_closed = True

    def signal_group(self, signum: int) -> str:
        self.signals.append(signum)
        if signum == 9:
            self._alive = False
        return "delivered"

    def reap(self, *, wait_seconds: float | None = None) -> bool:
        self.reaped = True
        self._alive = False
        return True


def _readline(descriptor: int) -> bytes:
    chunks = bytearray()
    while b"\n" not in chunks:
        try:
            piece = os.read(descriptor, 4096)
        except OSError:
            break
        if not piece:
            break
        chunks.extend(piece)
    return bytes(chunks)


def _send(descriptor: int, payload: dict) -> None:
    os.write(descriptor, encode_message(payload))


def _serve(child: FakeChild, responder) -> None:
    def run() -> None:
        try:
            opening = json.loads(_readline(child.peer_in).decode())
            child.seen.append(opening)
            _send(
                child.peer_out,
                {
                    "jsonrpc": "2.0",
                    "id": opening["id"],
                    "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}},
                },
            )
            child.seen.append(json.loads(_readline(child.peer_in).decode()))
            child.handshake.set()
            responder(child)
        except Exception:
            child.handshake.set()

    threading.Thread(target=run, daemon=True).start()


def _respond(child: FakeChild) -> None:
    while True:
        raw = _readline(child.peer_in)
        if not raw:
            return
        try:
            message = json.loads(raw.decode())
        except json.JSONDecodeError:
            return
        child.seen.append(message)
        method = message.get("method")
        if method in {"tools/call", "resources/read"}:
            child.calls.append(message)
        if method == "tools/list":
            _send(child.peer_out, {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": [{"name": "lookup", "inputSchema": _SCHEMA}]}})
        elif method == "resources/list":
            _send(
                child.peer_out,
                {"jsonrpc": "2.0", "id": message["id"], "result": {"resources": [{"uri": "notes://item", "name": "Item", "text": _SECRET}]}},
            )
        elif method == "tools/call":
            _send(child.peer_out, {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": "ok"}]}})
        elif method == "resources/read":
            _send(child.peer_out, {"jsonrpc": "2.0", "id": message["id"], "result": {"contents": [{"uri": "notes://item", "text": "body"}]}})


def _install(responder=_respond) -> list[FakeChild]:
    holder: list[FakeChild] = []

    def launch(_server, facts, generation: int) -> FakeChild:
        child = FakeChild(facts, generation)
        holder.append(child)
        _serve(child, responder)
        return child

    set_spawn_override(launch)
    return holder


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
    set_spawn_override(None)
    set_before_pin(None)
    set_after_pin(None)
    set_commit_fault(None)
    set_before_write(None)
    set_permit_hook(None)
    set_reload_fault(None, None)
    set_stop_before_link(False)
    set_before_permit(None)
    set_test_timing(stdin_grace=0, term_grace=0, list_seconds=2, call_seconds=2)
    yield
    shutdown_all()
    allow_sessions()
    mcp_session._generations.clear()
    mcp_session._stopping.clear()
    mcp_session._sessions.clear()
    mcp_session._inflight.clear()
    set_spawn_override(None)
    set_before_pin(None)
    set_after_pin(None)
    set_commit_fault(None)
    set_before_write(None)
    set_permit_hook(None)
    set_reload_fault(None, None)
    set_stop_before_link(False)
    set_before_permit(None)
    reset_test_timing()
    set_epoch_listener(retire_older_epochs)
    loader._snapshot = None


def _document(agent_id: str, executable: str | None = None) -> dict:
    path = TRUSTED_EXECUTABLE if executable is None else executable
    return {
        "executable_roots": [TRUSTED_ROOT],
        "servers": [
            {
                "id": "notes",
                "display_name": "Local notes",
                "enabled": True,
                "executable": path,
                "args": [],
                "env": {},
                "agent_ids": [agent_id],
                "tool_names": ["lookup"],
                "resource_uris": ["notes://item"],
            }
        ],
    }


def _write_config(database, document: dict) -> None:
    directory = database.parent / "mcp"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    os.chmod(database.parent, 0o700)
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory / "servers.json", flags, 0o600)
    try:
        os.write(descriptor, json.dumps(document).encode())
    finally:
        os.close(descriptor)


def _prepare(client, database_file):
    agent = create_agent(client)
    grant(client, agent["id"], ["list_mcp_tools", "list_mcp_resources", "call_mcp_tool", "read_mcp_resource"])
    client.put("/api/v1/permissions/mode", json={"mode": "ask_sensitive"})
    _write_config(database_file, _document(agent["id"]))
    assert reload_configuration().published is True
    return agent


def _service() -> ApprovalService:
    return ApprovalService(Session(get_engine()), get_tool_registry(), ToolExecutor())


def _propose(client, agent_id: str, *, tool: str = "call_mcp_tool", arguments: dict | None = None) -> dict:
    body = arguments or {
        "server_id": "notes",
        "remote_tool_name": "lookup",
        "tool_arguments": {"query": "price", "api_key": _SECRET},
    }
    response = client.post(f"/api/v1/tools/{tool}/test", json={"arguments": body, "agent_id": agent_id})
    assert response.status_code == 200, response.text
    return response.json()


def _rows() -> tuple[int, int]:
    with Session(get_engine()) as session:
        approvals = session.scalar(select(ApprovalRequestRecord).where(ApprovalRequestRecord.tool_name == "call_mcp_tool"))
        count = len(list(session.scalars(select(ApprovalRequestRecord).where(ApprovalRequestRecord.tool_name.in_(["call_mcp_tool", "read_mcp_resource"])))))
        payloads = len(list(session.scalars(select(McpExecutionPayloadRecord))))
        del approvals
    return count, payloads


def test_discovery_change_before_pin_leaves_no_rows(client, database_file):
    agent = _prepare(client, database_file)
    holder = _install()

    def hook(package):
        deliver_line(package.session, _notice())

    set_before_pin(hook)
    with pytest.raises(Exception):
        _service().create_mcp(
            agent_id=agent["id"],
            tool_name="call_mcp_tool",
            arguments={"server_id": "notes", "remote_tool_name": "lookup", "tool_arguments": {"query": "price"}},
            reason="MCP execution needs approval.",
            mode="ask_sensitive",
        )
    assert holder[0].handshake.wait(2)
    assert _rows() == (0, 0)
    assert not any(item.get("method") == "tools/call" for item in holder[0].seen)


def test_discovery_change_blocks_until_the_pin_is_released(client, database_file):
    agent = _prepare(client, database_file)
    holder = _install()
    pinned = {}
    released = threading.Event()

    def hook(pin):
        session = pin._session
        pinned["generation"] = session.discovery
        assert session.send_lock.acquire(blocking=False) is False

        def bump() -> None:
            deliver_line(session, _notice())
            released.set()

        threading.Thread(target=bump, daemon=True).start()

    set_after_pin(hook)
    service = _service()
    created = service.create_mcp(
        agent_id=agent["id"],
        tool_name="call_mcp_tool",
        arguments={"server_id": "notes", "remote_tool_name": "lookup", "tool_arguments": {"query": "price"}},
        reason="MCP execution needs approval.",
        mode="ask_sensitive",
    )
    created_id = created.id
    assert released.wait(2)
    with Session(get_engine()) as session:
        stored = json.loads(session.get(ApprovalRequestRecord, created_id).arguments_json)
    assert stored["discovery_generation"] == pinned["generation"]
    assert published_session("notes").discovery == pinned["generation"] + 1
    assert holder[0].handshake.wait(2)


def test_commit_failure_while_pinned_rolls_back_and_releases_the_lock(client, database_file):
    agent = _prepare(client, database_file)
    _install()

    def fault() -> None:
        raise RuntimeError("commit failed")

    set_commit_fault(fault)
    with pytest.raises(RuntimeError):
        _service().create_mcp(
            agent_id=agent["id"],
            tool_name="call_mcp_tool",
            arguments={"server_id": "notes", "remote_tool_name": "lookup", "tool_arguments": {"query": "price"}},
            reason="MCP execution needs approval.",
            mode="ask_sensitive",
        )
    session = published_session("notes")
    assert session is not None
    assert session.send_lock.acquire(blocking=False) is True
    session.send_lock.release()
    before = session.discovery
    deliver_line(session, _notice())
    assert session.discovery == before + 1
    assert _rows() == (0, 0)


def test_reload_before_permit_writes_no_frame(client, database_file):
    agent = _prepare(client, database_file)
    holder = _install()
    proposed = _propose(client, agent["id"])

    def publish_first() -> None:
        _write_config(database_file, _document(agent["id"], executable=TRUSTED_ALT_EXECUTABLE))
        assert reload_configuration().published is True

    set_before_permit(publish_first)
    decision = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")
    assert decision.status_code == 200
    assert decision.json()["execution_status"] == "not_dispatched"
    assert holder[0].calls == []


def test_reload_blocks_while_the_permit_is_held(client, database_file):
    agent = _prepare(client, database_file)
    holder = _install()
    proposed = _propose(client, agent["id"])
    order = []

    def hook() -> None:
        order.append("permit")

        def reload() -> None:
            _write_config(database_file, _document(agent["id"], executable=TRUSTED_ALT_EXECUTABLE))
            reload_configuration()

        threading.Thread(target=reload, daemon=True).start()

    def record_lock() -> None:
        order.append("reload")

    set_permit_hook(hook)
    set_reload_fault("lock_acquired", record_lock)
    real_write = os.write

    def spy(descriptor, data):
        if b"tools/call" in bytes(data):
            order.append("write")
        return real_write(descriptor, data)

    os.write = spy
    try:
        decision = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")
    finally:
        os.write = real_write
    assert decision.status_code == 200
    assert decision.json()["status"] in {"COMPLETED", "FAILED"}
    assert order.index("permit") < order.index("write") < order.index("reload")
    assert len(holder[0].calls) == 1


def test_process_generation_change_writes_no_frame(client, database_file):
    agent = _prepare(client, database_file)
    holder = _install()
    proposed = _propose(client, agent["id"])

    def hook(session) -> None:
        session.generation += 1

    set_before_write(hook)
    decision = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")
    assert decision.json()["execution_status"] == "not_dispatched"
    assert holder[0].calls == []


def test_discovery_generation_change_writes_no_frame(client, database_file):
    agent = _prepare(client, database_file)
    holder = _install()
    proposed = _propose(client, agent["id"])

    def hook(session) -> None:
        with session.send_lock:
            session.discovery += 1

    set_before_write(hook)
    decision = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")
    assert decision.json()["execution_status"] == "not_dispatched"
    assert holder[0].calls == []


def test_concurrent_key_publication_shares_one_key(database_file):
    init_database()
    os.chmod(database_file.parent, 0o700)
    barrier = threading.Barrier(2)
    found = []

    def publish() -> None:
        barrier.wait()
        found.append(load_payload_key())

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(found) == 2
    assert found[0] == found[1]
    assert len(found[0]) == 32
    final = database_file.parent / "mcp-payload.key"
    assert final.stat().st_size == 32
    assert not list(database_file.parent.glob("mcp-payload.key.*"))


def test_crash_before_link_leaves_only_a_temporary_file(database_file):
    init_database()
    os.chmod(database_file.parent, 0o700)
    set_stop_before_link(True)
    with pytest.raises(PayloadError):
        load_payload_key()
    temps = list(database_file.parent.glob("mcp-payload.key.*"))
    assert len(temps) == 1
    assert not (database_file.parent / "mcp-payload.key").exists()
    cleanup_abandoned_payload_keys()
    assert list(database_file.parent.glob("mcp-payload.key.*")) == []
    set_stop_before_link(False)
    created = load_payload_key()
    assert len(created) == 32
    assert (database_file.parent / "mcp-payload.key").exists()


def test_existing_payload_row_does_not_create_a_missing_key(database_file):
    init_database()
    os.chmod(database_file.parent, 0o700)
    with Session(get_engine()) as session:
        session.add(McpExecutionPayloadRecord(approval_id="a" * 36, nonce=b"123456789012", ciphertext=b"ciphertext-tag-value!!"))
        session.commit()
    with pytest.raises(PayloadError):
        load_payload_key()
    assert not (database_file.parent / "mcp-payload.key").exists()


def test_invalid_final_key_fails_closed(database_file, monkeypatch):
    init_database()
    os.chmod(database_file.parent, 0o700)
    final = database_file.parent / "mcp-payload.key"
    os.symlink("elsewhere", final)
    with pytest.raises(PayloadError):
        load_payload_key()
    final.unlink()
    final.write_bytes(b"short")
    os.chmod(final, 0o600)
    with pytest.raises(PayloadError):
        load_payload_key()
    final.write_bytes(os.urandom(32))
    os.chmod(final, 0o644)
    with pytest.raises(PayloadError):
        load_payload_key()
    final.unlink()
    os.mkdir(final)
    with pytest.raises(PayloadError):
        load_payload_key()
    os.rmdir(final)
    descriptor = os.open(final, os.O_CREAT | os.O_WRONLY, 0o600)
    os.write(descriptor, os.urandom(32))
    info = os.fstat(descriptor)
    os.close(descriptor)
    assert _accepted(info, os.getuid()) is True
    assert _accepted(info, os.getuid() + 1) is False
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(PayloadError):
        load_payload_key()


def test_existing_valid_key_is_reused(database_file):
    init_database()
    os.chmod(database_file.parent, 0o700)
    first = load_payload_key()
    inode = (database_file.parent / "mcp-payload.key").stat().st_ino
    second = load_payload_key()
    assert first == second
    assert (database_file.parent / "mcp-payload.key").stat().st_ino == inode


def test_startup_executing_row_becomes_unknown_without_a_frame(database_file):
    init_database()
    _insert_terminal("EXECUTING")
    calls = []
    set_spawn_override(lambda *_args, **_kwargs: calls.append("spawn"))
    from app.approvals.mcp_policy import settle_mcp_approvals

    settle_mcp_approvals()
    with Session(get_engine()) as session:
        record = session.scalars(select(ApprovalRequestRecord)).one()
        assert record.status == "FAILED"
        assert record.execution_status == "unknown"
        assert session.get(McpExecutionPayloadRecord, record.id) is None
    assert calls == []


def test_startup_approved_row_becomes_not_dispatched_without_a_frame(database_file):
    init_database()
    _insert_terminal("APPROVED")
    calls = []
    set_spawn_override(lambda *_args, **_kwargs: calls.append("spawn"))
    from app.approvals.mcp_policy import settle_mcp_approvals

    settle_mcp_approvals()
    with Session(get_engine()) as session:
        record = session.scalars(select(ApprovalRequestRecord)).one()
        assert record.status == "FAILED"
        assert record.execution_status == "not_dispatched"
    assert calls == []


def test_write_failure_before_a_byte_is_not_dispatched(client, database_file, monkeypatch):
    agent = _prepare(client, database_file)
    holder = _install()
    proposed = _propose(client, agent["id"])
    real = os.write

    def fail(descriptor, data):
        if b"tools/call" in bytes(data):
            raise OSError("closed")
        return real(descriptor, data)

    monkeypatch.setattr(os, "write", fail)
    decision = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")
    assert decision.json()["execution_status"] == "not_dispatched"
    assert holder[0].calls == []


def test_partial_write_is_unknown_and_is_not_retried(client, database_file, monkeypatch):
    agent = _prepare(client, database_file)
    holder = _install()
    proposed = _propose(client, agent["id"])
    real = os.write
    writes = []

    def partial(descriptor, data):
        raw = bytes(data)
        if b"tools/call" in raw:
            if not writes:
                writes.append(raw)
                return real(descriptor, raw[:1])
            raise OSError("short")
        return real(descriptor, data)

    monkeypatch.setattr(os, "write", partial)
    approval_id = proposed["output"]["approval_id"]
    decision = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert decision.json()["status"] == "FAILED"
    assert decision.json()["execution_status"] == "unknown"
    assert decision.json()["outcome"] == "unknown"
    assert "will not be retried" in decision.json()["result"]
    again = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert again.status_code == 409
    assert len(writes) == 1
    assert holder[0].calls == []


def test_remote_error_is_remote_failed(client, database_file):
    agent = _prepare(client, database_file)

    def responder(child: FakeChild) -> None:
        while True:
            raw = _readline(child.peer_in)
            if not raw:
                return
            message = json.loads(raw.decode())
            child.seen.append(message)
            if message.get("method") == "tools/list":
                _send(child.peer_out, {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": [{"name": "lookup", "inputSchema": _SCHEMA}]}})
            elif message.get("method") == "tools/call":
                child.calls.append(message)
                _send(child.peer_out, {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": "no"}})

    holder = _install(responder)
    proposed = _propose(client, agent["id"])
    decision = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")
    assert decision.json()["status"] == "FAILED"
    assert decision.json()["execution_status"] == "remote_failed"
    assert len(holder[0].calls) == 1
    with Session(get_engine()) as session:
        assert session.scalars(select(McpExecutionPayloadRecord)).all() == []


def test_successful_response_completes_and_deletes_the_payload(client, database_file):
    agent = _prepare(client, database_file)
    holder = _install()
    proposed = _propose(client, agent["id"])
    approval_id = proposed["output"]["approval_id"]
    decision = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert decision.json()["status"] == "COMPLETED"
    assert _SECRET not in decision.text
    again = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert again.status_code == 409
    assert len(holder[0].calls) == 1
    with Session(get_engine()) as session:
        assert session.get(McpExecutionPayloadRecord, approval_id) is None


def test_permission_changes_cancel_pending_mcp_approvals(client, database_file):
    agent = _prepare(client, database_file)
    _install()
    proposed = _propose(client, agent["id"])
    approval_id = proposed["output"]["approval_id"]
    client.put("/api/v1/permissions/mode", json={"mode": "restricted_auto"})
    with Session(get_engine()) as session:
        record = session.get(ApprovalRequestRecord, approval_id)
        assert record.status == "CANCELLED"
        assert session.get(McpExecutionPayloadRecord, approval_id) is None
    denied = client.post(
        "/api/v1/tools/call_mcp_tool/test",
        json={"arguments": {"server_id": "notes", "remote_tool_name": "lookup", "tool_arguments": {"query": "price"}}, "agent_id": agent["id"]},
    )
    assert denied.status_code == 403


def test_agent_disable_cancels_pending_mcp_approvals(client, database_file):
    agent = _prepare(client, database_file)
    _install()
    proposed = _propose(client, agent["id"])
    approval_id = proposed["output"]["approval_id"]
    client.patch(f"/api/v1/agents/{agent['id']}", json={"enabled": False})
    with Session(get_engine()) as session:
        assert session.get(ApprovalRequestRecord, approval_id).status == "CANCELLED"
        assert session.get(McpExecutionPayloadRecord, approval_id) is None


def test_grant_removal_cancels_pending_mcp_approvals(client, database_file):
    agent = _prepare(client, database_file)
    _install()
    proposed = _propose(client, agent["id"])
    approval_id = proposed["output"]["approval_id"]
    grant(client, agent["id"], ["list_mcp_tools"])
    with Session(get_engine()) as session:
        assert session.get(ApprovalRequestRecord, approval_id).status == "CANCELLED"
        assert session.get(McpExecutionPayloadRecord, approval_id) is None


def test_security_boundaries(client, database_file, caplog):
    caplog.set_level("DEBUG")
    agent = _prepare(client, database_file)
    holder = _install()
    stranger = create_agent(client, name="Stranger")
    denied = client.post(
        "/api/v1/tools/call_mcp_tool/test",
        json={"arguments": {"server_id": "notes", "remote_tool_name": "lookup", "tool_arguments": {"query": "price"}}, "agent_id": stranger["id"]},
    )
    assert denied.status_code == 403
    anonymous = client.post(
        "/api/v1/tools/list_mcp_tools/test",
        json={"arguments": {"server_id": "notes"}},
    )
    assert anonymous.status_code == 403
    session = open_session("notes")
    assert holder[0].handshake.wait(2)
    with pytest.raises(SessionError):
        request(session, "tools/call", {"name": "lookup"})
    assert client.get("/api/v1/mcp").status_code == 404
    assert "call_mcp_tool" not in ALLOWED_TOOLS
    proposed = _propose(client, agent["id"])
    approval_id = proposed["output"]["approval_id"]
    body = client.get(f"/api/v1/approvals/{approval_id}")
    assert _SECRET not in body.text
    assert "mcp-payload" not in body.text
    assert _SECRET not in caplog.text
    with Session(get_engine()) as db:
        payload = db.get(McpExecutionPayloadRecord, approval_id)
        payload.ciphertext = payload.ciphertext[:-1] + bytes([payload.ciphertext[-1] ^ 1])
        db.commit()
    tampered = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert tampered.json()["execution_status"] == "not_dispatched"
    assert holder[0].calls == []


def test_expired_cancelled_rejected_and_terminal_do_not_execute(client, database_file):
    agent = _prepare(client, database_file)
    holder = _install()
    proposed = _propose(client, agent["id"])
    approval_id = proposed["output"]["approval_id"]
    cancelled = client.post(f"/api/v1/approvals/{approval_id}/reject")
    assert cancelled.status_code == 200
    again = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert again.status_code == 409
    second = _propose(client, agent["id"])
    second_id = second["output"]["approval_id"]
    with Session(get_engine()) as db:
        record = db.get(ApprovalRequestRecord, second_id)
        record.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()
    expired = client.post(f"/api/v1/approvals/{second_id}/approve")
    assert expired.status_code == 409
    assert holder[0].calls == []


def test_substituted_approval_payload_and_revision_do_not_dispatch(client, database_file):
    agent = _prepare(client, database_file)
    holder = _install()
    first = _propose(client, agent["id"])["output"]["approval_id"]
    second = _propose(client, agent["id"])["output"]["approval_id"]
    with Session(get_engine()) as db:
        left = db.get(McpExecutionPayloadRecord, first)
        right = db.get(McpExecutionPayloadRecord, second)
        left.nonce, right.nonce = right.nonce, left.nonce
        left.ciphertext, right.ciphertext = right.ciphertext, left.ciphertext
        db.commit()
    for approval_id in (first, second):
        decision = client.post(f"/api/v1/approvals/{approval_id}/approve")
        assert decision.status_code == 200
        assert decision.json()["execution_status"] == "not_dispatched"
    third = _propose(client, agent["id"])["output"]["approval_id"]
    with Session(get_engine()) as db:
        record = db.get(ApprovalRequestRecord, third)
        body = json.loads(record.arguments_json)
        body["approval_id"] = first
        body["config_revision"] = "f" * 64
        body["epoch"] = 99
        record.arguments_json = json.dumps(body)
        db.commit()
    refused = client.post(f"/api/v1/approvals/{third}/approve")
    assert refused.status_code == 409
    with Session(get_engine()) as db:
        assert db.get(ApprovalRequestRecord, third).status == "PENDING"
    assert holder[0].calls == []


def _notice() -> bytes:
    return encode_message({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}).rstrip(b"\n")


def _insert_terminal(status: str) -> None:
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        session.add(
            ApprovalRequestRecord(
                id="b" * 36,
                agent_id=None,
                tool_name="call_mcp_tool",
                action="execute",
                resource_type="none",
                resource_id=None,
                relative_path=None,
                arguments_json="{}",
                fingerprint="c" * 64,
                description="Review the sealed operation.",
                risk="sensitive",
                reason="review",
                mode="ask_sensitive",
                status=status,
                created_at=now,
                expires_at=now + timedelta(minutes=5),
                decided_at=now,
                execution_status=status,
                result_text=None,
            )
        )
        session.add(McpExecutionPayloadRecord(approval_id="b" * 36, nonce=b"123456789012", ciphertext=b"ciphertext-tag-value!!"))
        session.commit()
