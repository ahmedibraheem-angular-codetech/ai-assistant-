"""WP-01: bounded MCP writes and waits that leave the API event loop free."""

import json
import os
import threading

import pytest

from app.mcp import gateway, loader
from app.mcp import session as mcp_session
from app.mcp.loader import mcp_status, reload_configuration, set_permit_hook
from app.mcp.protocol import encode_message
from app.mcp.session import (
    DISPATCH_GUARD,
    allow_sessions,
    begin_list_frame,
    open_session,
    published_session,
    reset_test_timing,
    retire_older_epochs,
    set_epoch_listener,
    set_spawn_override,
    set_test_timing,
    shutdown_all,
    wait_mcp,
    write_budget,
)
from tests.test_mcp_gateway import (
    FakeChild,
    _SCHEMA,
    _prepare,
    _propose,
    _readline,
    _send,
    _serve,
)


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
    set_permit_hook(None)
    set_test_timing(stdin_grace=0, term_grace=0, list_seconds=2, call_seconds=2, write_seconds=0.3)
    yield
    shutdown_all()
    allow_sessions()
    mcp_session._generations.clear()
    mcp_session._stopping.clear()
    mcp_session._sessions.clear()
    mcp_session._inflight.clear()
    set_spawn_override(None)
    set_permit_hook(None)
    reset_test_timing()
    set_epoch_listener(retire_older_epochs)
    loader._snapshot = None


def _install(responder) -> list[FakeChild]:
    holder: list[FakeChild] = []

    def launch(_server, facts, generation: int) -> FakeChild:
        child = FakeChild(facts, generation)
        holder.append(child)
        _serve(child, responder)
        return child

    set_spawn_override(launch)
    return holder


def _fill(descriptor: int) -> None:
    os.set_blocking(descriptor, False)
    try:
        while True:
            try:
                os.write(descriptor, b"x" * 65536)
            except BlockingIOError:
                return
    finally:
        os.set_blocking(descriptor, True)


def _list_result(message: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message["id"],
        "result": {"tools": [{"name": "lookup", "inputSchema": _SCHEMA}]},
    }


def test_write_budget_is_finite_and_separate_from_the_response_deadline():
    assert write_budget("call") == 0.3
    assert write_budget("call") < 2


def test_full_pipe_releases_the_loader_lock(client, database_file):
    agent = _prepare(client, database_file)
    released = threading.Event()

    def responder(child: FakeChild) -> None:
        raw = _readline(child.peer_in)
        if not raw:
            return
        _send(child.peer_out, _list_result(json.loads(raw.decode())))

    holder = _install(responder)
    proposed = _propose(client, agent["id"])
    assert holder[0].handshake.wait(2)
    _fill(published_session("notes").child.stdin_fd)

    def contend() -> None:
        loader._lock.acquire()
        released.set()
        loader._lock.release()

    def hook() -> None:
        threading.Thread(target=contend, daemon=True).start()

    set_permit_hook(hook)
    decision = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")
    assert released.wait(2)
    assert decision.status_code == 200
    assert decision.json()["execution_status"] == "not_dispatched"
    assert mcp_status().available is True
    assert reload_configuration().published is True
    assert holder[0].calls == []


def test_partial_pipe_is_unknown_and_retires_the_session(client, database_file, monkeypatch):
    agent = _prepare(client, database_file)

    def responder(child: FakeChild) -> None:
        raw = _readline(child.peer_in)
        if not raw:
            return
        _send(child.peer_out, _list_result(json.loads(raw.decode())))

    holder = _install(responder)
    proposed = _propose(client, agent["id"])
    child = holder[0]
    assert child.handshake.wait(2)
    real = os.write
    started = {"value": False}

    def partial(descriptor, data):
        raw = bytes(data)
        if b"tools/call" in raw:
            if not started["value"]:
                started["value"] = True
                return real(descriptor, raw[:1])
            raise BlockingIOError()
        return real(descriptor, data)

    monkeypatch.setattr(os, "write", partial)
    approval_id = proposed["output"]["approval_id"]
    decision = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert decision.json()["execution_status"] == "unknown"
    again = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert again.status_code == 409
    assert published_session("notes") is None
    assert child.calls == []
    assert started["value"] is True


def test_broken_pipe_before_a_byte_is_not_dispatched(client, database_file):
    agent = _prepare(client, database_file)

    def responder(child: FakeChild) -> None:
        raw = _readline(child.peer_in)
        if not raw:
            return
        _send(child.peer_out, _list_result(json.loads(raw.decode())))

    holder = _install(responder)
    proposed = _propose(client, agent["id"])
    assert holder[0].handshake.wait(2)
    os.close(holder[0].peer_in)
    holder[0].peer_in = -1
    decision = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")
    assert decision.json()["execution_status"] == "not_dispatched"
    assert published_session("notes") is None


def test_close_during_a_blocked_write_returns(client, database_file):
    agent = _prepare(client, database_file)

    def responder(child: FakeChild) -> None:
        raw = _readline(child.peer_in)
        if not raw:
            return
        _send(child.peer_out, _list_result(json.loads(raw.decode())))

    holder = _install(responder)
    proposed = _propose(client, agent["id"])
    assert holder[0].handshake.wait(2)
    session = published_session("notes")
    _fill(session.child.stdin_fd)
    started = threading.Event()
    finished = threading.Event()

    def close_when_write_starts() -> None:
        assert started.wait(2)
        from app.mcp.session import close_session

        close_session(session)
        finished.set()

    def hook() -> None:
        threading.Thread(target=close_when_write_starts, daemon=True).start()
        started.set()

    set_permit_hook(hook)
    decision = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")
    assert finished.wait(2)
    assert decision.status_code == 200
    assert decision.json()["execution_status"] == "not_dispatched"


def test_response_wait_does_not_hold_the_gateway_or_loader_lock(client, database_file):
    agent = _prepare(client, database_file)
    seen = threading.Event()
    release = threading.Event()

    def responder(child: FakeChild) -> None:
        raw = _readline(child.peer_in)
        _send(child.peer_out, _list_result(json.loads(raw.decode())))
        frame = _readline(child.peer_in)
        child.calls.append(json.loads(frame.decode()))
        seen.set()
        assert release.wait(3)
        message = child.calls[0]
        _send(child.peer_out, {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": "ok"}]}})

    _install(responder)
    proposed = _propose(client, agent["id"])
    result: dict = {}

    def approve() -> None:
        result["response"] = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")

    worker = threading.Thread(target=approve)
    worker.start()
    assert seen.wait(3)
    assert loader._lock.acquire(blocking=False) is True
    loader._lock.release()
    assert gateway._server_lock("notes").acquire(blocking=False) is True
    gateway._server_lock("notes").release()
    session = published_session("notes")
    assert session.send_lock.acquire(blocking=False) is True
    session.send_lock.release()
    release.set()
    worker.join(3)
    assert result["response"].json()["status"] == "COMPLETED"
    assert len(result["response"].json()) > 0


def test_withheld_approval_leaves_the_event_loop_free(client, database_file):
    agent = _prepare(client, database_file)
    seen = threading.Event()
    release = threading.Event()

    def responder(child: FakeChild) -> None:
        raw = _readline(child.peer_in)
        _send(child.peer_out, _list_result(json.loads(raw.decode())))
        frame = _readline(child.peer_in)
        seen.set()
        assert release.wait(3)
        message = json.loads(frame.decode())
        _send(child.peer_out, {"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}})

    _install(responder)
    proposed = _propose(client, agent["id"])
    outcome: dict = {}

    def approve() -> None:
        outcome["response"] = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")

    worker = threading.Thread(target=approve)
    worker.start()
    assert seen.wait(3)
    health: dict = {}

    def probe() -> None:
        health["response"] = client.get("/api/v1/health")

    probe_thread = threading.Thread(target=probe)
    probe_thread.start()
    probe_thread.join(1)
    assert probe_thread.is_alive() is False
    assert health["response"].status_code == 200
    assert health["response"].json()["status"] == "ok"
    release.set()
    worker.join(3)
    assert outcome["response"].status_code == 200


def test_withheld_discovery_leaves_the_event_loop_free(client, database_file):
    agent = _prepare(client, database_file)
    seen = threading.Event()
    release = threading.Event()

    def responder(child: FakeChild) -> None:
        raw = _readline(child.peer_in)
        seen.set()
        assert release.wait(3)
        _send(child.peer_out, _list_result(json.loads(raw.decode())))

    _install(responder)
    outcome: dict = {}

    def discover() -> None:
        outcome["response"] = client.post(
            "/api/v1/tools/list_mcp_tools/test",
            json={"arguments": {"server_id": "notes"}, "agent_id": agent["id"]},
        )

    worker = threading.Thread(target=discover)
    worker.start()
    assert seen.wait(3)
    assert loader._lock.acquire(blocking=False) is True
    loader._lock.release()
    health: dict = {}

    def probe() -> None:
        health["response"] = client.get("/api/v1/health")

    probe_thread = threading.Thread(target=probe)
    probe_thread.start()
    probe_thread.join(1)
    assert probe_thread.is_alive() is False
    assert health["response"].status_code == 200
    release.set()
    worker.join(3)
    assert outcome["response"].status_code == 200
    assert outcome["response"].json()["status"] == "ok"


def test_out_of_order_responses_match_their_request_ids(client, database_file):
    _prepare(client, database_file)
    first_id = threading.Event()

    def responder(child: FakeChild) -> None:
        first = json.loads(_readline(child.peer_in).decode())
        second = json.loads(_readline(child.peer_in).decode())
        _send(child.peer_out, {"jsonrpc": "2.0", "id": second["id"], "result": {"mark": second["id"]}})
        _send(child.peer_out, {"jsonrpc": "2.0", "id": first["id"], "result": {"mark": first["id"]}})
        first_id.set()

    holder = _install(responder)
    session = open_session("notes")
    assert holder[0].handshake.wait(2)
    barrier = threading.Barrier(2)
    found: dict = {}

    def once(slot: int) -> None:
        barrier.wait()
        pending = begin_list_frame(session, "tools/list", guard=DISPATCH_GUARD)
        found[slot] = pending.request_id
        assert wait_mcp(session, pending, seconds=2) == "completed"
        found[f"result-{slot}"] = pending.result

    threads = [threading.Thread(target=once, args=(slot,)) for slot in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert first_id.wait(1)
    assert found["result-0"]["mark"] == found[0]
    assert found["result-1"]["mark"] == found[1]
    assert found[0] != found[1]


def test_successful_frame_is_one_complete_tools_call(client, database_file):
    agent = _prepare(client, database_file)

    def responder(child: FakeChild) -> None:
        raw = _readline(child.peer_in)
        _send(child.peer_out, _list_result(json.loads(raw.decode())))
        frame = _readline(child.peer_in)
        message = json.loads(frame.decode())
        child.calls.append(message)
        assert frame.endswith(b"\n")
        assert encode_message(message) == frame
        _send(child.peer_out, {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": "ok"}]}})

    holder = _install(responder)
    proposed = _propose(client, agent["id"])
    decision = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve")
    assert decision.json()["status"] == "COMPLETED"
    assert len(holder[0].calls) == 1
    assert holder[0].calls[0]["method"] == "tools/call"
