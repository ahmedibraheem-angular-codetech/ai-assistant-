"""In-memory MCP stdio sessions.

Lock order, outer to inner, is the per-server gateway lock, then the loader
publication lock, then the registry lock, then the session lock, then the
send lock. The registry lock is never acquired while the send lock is held.
The loader lock is never acquired while the send lock is held. A request
waits for its response only after releasing the send lock. The per-server
gateway lock is also released before that wait. Same-server frames stay
serialized by the send lock, and each response is delivered to the pending
entry for its JSON-RPC id.

Stdin writes use a finite budget. Two seconds is enough to transfer one
frame of at most 1 MiB into a pipe that is being read, and it is short
enough that a child which stops reading cannot hold the write permit.
The response wait uses the separate list or call deadline and starts only
after the write returns. Tests may lower write_seconds. The write budget
never exceeds the operation deadline for that kind.

The discovery pin is the one approved exception: it holds the send lock
while the approval row and payload row commit, and never while an MCP
response is outstanding or while the loader lock is held.

Sessions are not stored in SQLite. Startup does not adopt an unknown pid.
A process generation is never reused. The public request path rejects tool
and resource methods before any byte is written.

Lifecycle ownership: every launched child stays reachable until confirmed
reaped. Ready sessions live in `_sessions`. Sessions whose children are not
yet reaped live in `_stopping` after they leave `_sessions`. Failed startups
that launched a child use the same `_stopping` path via `_abandon`. Linux
spawn failures that cannot confirm reap attach a signal/reap-only handle on
`ProcessError.unreaped`; `_bring_up` adopts it before `_abandon` so accounting
and admission stay honest. At most one cleanup owner runs per session
(`_closing`); retries are bounded and cannot overlap. `open_session` never
launches a second child for a server identity while that identity remains in
`_stopping`.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import signal
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

from app.mcp.loader import current_snapshot, mcp_status, set_epoch_listener
from app.mcp.process import (
    ProcessError,
    StderrRing,
    assess_executable,
    environment_sha256,
    open_executable,
    start_process,
)
from app.mcp.protocol import (
    MAX_LINE_BYTES,
    LIST_CHANGED,
    accepted_initialize,
    cancellation_notification,
    cancellation_target,
    client_allowed,
    decode_line,
    empty_result,
    encode_message,
    initialize_request,
    initialized_notification,
    method_not_found,
    ping_request,
    resources_list_request,
    tools_list_request,
)

logger = logging.getLogger(__name__)

INITIALIZE_TIMEOUT = 5.0
PING_TIMEOUT = 5.0
LIST_TIMEOUT = 5.0
CALL_TIMEOUT = 30.0
CALL_HARD_CAP = 60.0
WRITE_BUDGET = 2.0
IDLE_TIMEOUT = 60.0
STDIN_GRACE = 2.0
TERM_GRACE = 2.0
NOTIFY_LIMIT = 32
MAX_TOMBSTONES = 64

_registry = threading.Lock()
_sessions: dict[str, "McpSession"] = {}
_stopping: dict[str, "McpSession"] = {}
_inflight: dict[str, "_Reservation"] = {}
_generations: dict[str, int] = {}
_accepting = True
_spawn_override = None
_monotonic = time.monotonic
_sleep = time.sleep


def _tracked_server_ids_locked() -> set[str]:
    """Server identities that hold a process slot. Caller holds `_registry`."""
    return set(_sessions) | set(_inflight) | set(_stopping)


def _child_reaped(session: "McpSession") -> bool:
    child = session.child
    return child is None or bool(getattr(child, "reaped", False))


def _publish_lifecycle_locked(session: "McpSession", *, terminated: bool) -> None:
    """Move session between registries. Caller holds `_registry`.

    Ready publication uses `_sessions` only. An unreaped child is always
    retained in `_stopping` so shutdown and open_session cannot lose it.
    """
    if _sessions.get(session.server_id) is session:
        _sessions.pop(session.server_id, None)
    if terminated or _child_reaped(session):
        if _stopping.get(session.server_id) is session:
            _stopping.pop(session.server_id, None)
    else:
        _stopping[session.server_id] = session


class SessionError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The MCP session is not available.")


@dataclass
class _Timing:
    initialize_seconds: float = INITIALIZE_TIMEOUT
    ping_seconds: float = PING_TIMEOUT
    idle_seconds: float = IDLE_TIMEOUT
    stdin_grace: float = STDIN_GRACE
    term_grace: float = TERM_GRACE
    notify_limit: int = NOTIFY_LIMIT
    # None means the MCP_MAX_PROCESSES setting (R-17). Tests set an explicit number.
    max_processes: int | None = None
    max_tombstones: int = MAX_TOMBSTONES
    list_seconds: float = LIST_TIMEOUT
    call_seconds: float = CALL_TIMEOUT
    write_seconds: float = WRITE_BUDGET


_timing = _Timing()


class _Pending:
    def __init__(self, deadline_at: float) -> None:
        self.deadline_at = deadline_at
        self.event = threading.Event()
        self.result: object = None
        self.error: str | None = None
        self.request_id = ""
        self.discovery_at_response: int | None = None
        self.accepted = 0


class _DispatchGuard:
    """Identity held only by the gateway. Any other caller writes nothing."""


DISPATCH_GUARD = _DispatchGuard()


class DiscoveryPin:
    """Holds the send lock until the approval transaction finishes."""

    def __init__(self, session: McpSession) -> None:
        self._session = session
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        self._session.send_lock.release()


class _Reservation:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.session: McpSession | None = None
        self.error: SessionError | None = None
        self.generation = 0


class McpSession:
    def __init__(self, server_id: str, revision: str, epoch: int, generation: int, env_sha256: str) -> None:
        self.server_id = server_id
        self.revision = revision
        self.epoch = epoch
        self.generation = generation
        self.discovery = 1
        self.cache_stale = False
        self.env_sha256 = env_sha256
        self.state = "starting"
        self.child = None
        self.facts = None
        self.pending: dict[str, _Pending] = {}
        self.tombstone_order: deque[str] = deque()
        self.tombstone_reason: dict[str, str] = {}
        self.notices: deque[float] = deque()
        self.message_count = 0
        self.stderr = StderrRing()
        self.stdout_buffer = bytearray()
        self.send_lock = threading.Lock()
        self.lock = threading.Lock()
        self.reader: threading.Thread | None = None
        self.wake_r, self.wake_w = os.pipe()
        self.last_activity = _monotonic()
        self.close_done = threading.Event()
        self._closing = False
        self._closed = False
        self._stream_broken = False

    def __repr__(self) -> str:
        return f"McpSession(server_id={self.server_id!r}, epoch={self.epoch!r}, state={self.state!r})"

    @property
    def executable_device(self) -> int | None:
        return None if self.facts is None else self.facts.device

    @property
    def executable_inode(self) -> int | None:
        return None if self.facts is None else self.facts.inode

    @property
    def executable_sha256(self) -> str | None:
        return None if self.facts is None else self.facts.sha256


def set_test_timing(**kwargs: float) -> None:
    for key, value in kwargs.items():
        setattr(_timing, key, value)


def reset_test_timing() -> None:
    global _timing
    _timing = _Timing()


def set_clock(monotonic, sleep) -> None:
    global _monotonic, _sleep
    _monotonic = monotonic
    _sleep = sleep


def reset_clock() -> None:
    global _monotonic, _sleep
    _monotonic = time.monotonic
    _sleep = time.sleep


def set_spawn_override(callback) -> None:
    global _spawn_override
    _spawn_override = callback


def operation_deadline(kind: str) -> float:
    """Return the response budget for one MCP operation."""
    if kind == "initialize":
        return INITIALIZE_TIMEOUT
    if kind == "ping":
        return PING_TIMEOUT
    if kind == "list":
        return _timing.list_seconds
    if kind == "call":
        return min(_timing.call_seconds, CALL_HARD_CAP)
    raise SessionError("rejected")


def write_budget(kind: str) -> float:
    """Return the finite stdin budget for one frame.

    This is not the server's response time. A non-reading child must release
    the write permit when this budget expires.
    """
    return max(0.0, min(_timing.write_seconds, operation_deadline(kind)))


def open_session(server_id: str) -> McpSession:
    """Start or reuse the single session for one server. Two callers share one process."""
    for _attempt in range(3):
        status = mcp_status()
        snapshot = current_snapshot()
        if not status.available or snapshot is None:
            raise SessionError("unavailable")
        server = next((item for item in snapshot.servers if item.id == server_id), None)
        if server is None:
            raise SessionError("missing")
        if not server.enabled:
            raise SessionError("disabled")
        waiter = None
        stale = None
        stopping_target = None
        owner = False
        reservation: _Reservation | None = None
        generation = 0
        with _registry:
            if not _accepting:
                raise SessionError("shutting_down")
            # An unreaped prior child blocks a new generation for this identity.
            stopping = _stopping.get(server_id)
            if stopping is not None:
                stopping_target = stopping
            else:
                existing = _sessions.get(server_id)
                if existing is not None and existing.state == "ready":
                    if existing.epoch == snapshot.epoch and existing.revision == snapshot.revision:
                        return existing
                    stale = existing
                elif existing is not None and existing.state != "dead":
                    waiter = existing.close_done
                if stale is None and waiter is None:
                    current = _inflight.get(server_id)
                    if current is not None:
                        reservation = current
                    elif len(_tracked_server_ids_locked()) >= _max_processes():
                        raise SessionError("busy")
                    else:
                        reservation = _Reservation()
                        _inflight[server_id] = reservation
                        _generations[server_id] = _generations.get(server_id, 0) + 1
                        generation = _generations[server_id]
                        reservation.generation = generation
                        owner = True
        if stopping_target is not None:
            # Bounded cleanup (or wait for the current cleanup owner), then refuse
            # a new spawn if the prior child is still unreaped.
            close_session(stopping_target)
            with _registry:
                if _stopping.get(server_id) is not None:
                    raise SessionError("busy")
            continue
        if stale is not None:
            close_session(stale)
            continue
        if waiter is not None:
            waiter.wait(timeout=_timing.initialize_seconds + _timing.stdin_grace + _timing.term_grace + 1)
            continue
        assert reservation is not None
        if not owner:
            if not reservation.event.wait(timeout=_timing.initialize_seconds + 1):
                raise SessionError("startup_failed")
            if reservation.error is not None:
                raise reservation.error
            if reservation.session is None:
                raise SessionError("startup_failed")
            return reservation.session
        try:
            started = _bring_up(server, snapshot.revision, snapshot.epoch, generation)
            reservation.session = started
            return started
        except SessionError as exc:
            reservation.error = exc
            raise
        except ProcessError as exc:
            wrapped = SessionError("startup_failed")
            reservation.error = wrapped
            raise wrapped from exc
        finally:
            with _registry:
                _inflight.pop(server_id, None)
            reservation.event.set()
    raise SessionError("startup_failed")


def request(
    session: McpSession,
    method: str,
    params: dict | None = None,
    *,
    discovery_generation: int | None = None,
    deadline: float | None = None,
) -> object:
    """Send one allowed client request. Tool and resource methods write nothing."""
    del params
    if not client_allowed(method, startup=False) or method != "ping":
        raise SessionError("rejected")
    _require_current(session)
    seconds = _timing.ping_seconds if deadline is None else min(float(deadline), CALL_HARD_CAP)
    request_id = str(uuid.uuid4())
    pending = _Pending(_monotonic() + seconds)
    with session.lock:
        if session.state != "ready":
            raise SessionError("not_ready")
        with session.send_lock:
            if discovery_generation is not None and discovery_generation != session.discovery:
                raise SessionError("stale_generation")
            if session.state != "ready":
                raise SessionError("not_ready")
            session.pending[request_id] = pending
            try:
                _write_locked(session, ping_request(request_id))
            except Exception:
                session.pending.pop(request_id, None)
                raise
    if not wait_for(pending.event, seconds):
        _cancel_outstanding(session, request_id)
        raise SessionError("timeout")
    if pending.error:
        raise SessionError(pending.error)
    return pending.result


def close_session(session: McpSession) -> None:
    """Stop one session. One cleanup owner at a time; unreaped children stay tracked.

    Ownership: the thread that sets `_closing` runs one bounded shutdown attempt.
    Concurrent callers wait on `close_done`. After an unsuccessful attempt the
    session remains in `_stopping` with `_closing` cleared so a later call may
    retry. Retry exhaustion never drops an unreaped child from the registry.
    """
    retry = False
    with session.lock:
        if session.state == "dead":
            return
        if session._closing:
            owner = False
        elif (
            session._closed
            and session.state == "stopping"
            and session.child is not None
            and not getattr(session.child, "reaped", False)
        ):
            # Prior shutdown could not confirm termination. Allow one bounded retry
            # without re-entering reader teardown. Registry ownership stays in
            # `_stopping` until reap is confirmed.
            session._closing = True
            session.close_done = threading.Event()
            owner = True
            retry = True
        elif session._closed:
            return
        else:
            session._closing = True
            session.state = "stopping"
            owner = True
    if not owner:
        session.close_done.wait(timeout=_timing.initialize_seconds + _timing.stdin_grace + _timing.term_grace + 1)
        return
    terminated = False
    try:
        if not retry:
            _wake(session)
            reader = session.reader
            if reader is not None and reader is not threading.current_thread() and reader.is_alive():
                reader.join(timeout=1 + _timing.stdin_grace + _timing.term_grace)
        try:
            terminated = _shutdown_child(session)
        except Exception:
            # Unexpected waitpid/signal failures must not skip registry publish or
            # replace a deterministic busy/lifecycle outcome for open_session.
            logger.warning("The MCP session shutdown failed.")
            terminated = False
        if not retry:
            with session.send_lock:
                for request_id, pending in list(session.pending.items()):
                    _bury(session, request_id, "cancelled")
                    pending.error = "closed"
                    pending.event.set()
                session.pending.clear()
    finally:
        if terminated and session.child is not None and not _child_reaped(session):
            terminated = False
        with _registry:
            _publish_lifecycle_locked(session, terminated=terminated)
        with session.lock:
            session._closed = True
            # Only "dead" means termination was confirmed. Unconfirmed stops stay
            # "stopping" with _closing cleared so a later close_session may retry.
            session.state = "dead" if terminated else "stopping"
            session._closing = False
        if not retry or terminated:
            _close_pipe(session, "wake_r")
            _close_pipe(session, "wake_w")
        session.close_done.set()
        if not retry:
            _notify_generation(session.server_id, session.generation)


def _inflight_drain_seconds() -> float:
    """Bounded wait for an in-flight open_session owner to finish or fail."""
    return _timing.initialize_seconds + (2 * (_timing.stdin_grace + _timing.term_grace)) + 5


def _snapshot_tracked_sessions_locked() -> list[McpSession]:
    """Deduped `_sessions` then `_stopping`. Caller holds `_registry`."""
    pending: list[McpSession] = []
    seen: set[int] = set()
    for session in list(_sessions.values()) + list(_stopping.values()):
        identity = id(session)
        if identity in seen:
            continue
        seen.add(identity)
        pending.append(session)
    return pending


def shutdown_all() -> None:
    """Refuse new sessions and reap every tracked child this process started.

    Waits for known `_inflight` owners (bounded), closes `_sessions` and
    `_stopping`, then drains any still-inflight startup once more before a
    final close pass. Unreaped children remain in `_stopping` and lingering
    inflight owners are reported explicitly; shutdown never claims they were
    cleaned up.
    """
    global _accepting
    with _registry:
        _accepting = False
        inflight = list(_inflight.values())
    for reservation in inflight:
        reservation.event.wait(timeout=_inflight_drain_seconds())
    with _registry:
        pending = _snapshot_tracked_sessions_locked()
    for session in pending:
        try:
            close_session(session)
        except Exception:
            logger.warning("The MCP session shutdown failed.")
    # Startups that outlived the first wait may still hold a launched child.
    # One more bounded drain, then close whatever they published to stopping.
    with _registry:
        late_inflight = list(_inflight.values())
    for reservation in late_inflight:
        reservation.event.wait(timeout=_inflight_drain_seconds())
    with _registry:
        remaining = _snapshot_tracked_sessions_locked()
    for session in remaining:
        try:
            close_session(session)
        except Exception:
            logger.warning("The MCP session shutdown failed.")
    with _registry:
        unresolved = list(_stopping.values())
        lingering_inflight = [(server_id, reservation) for server_id, reservation in _inflight.items()]
    for session in unresolved:
        logger.warning(
            "MCP unreaped child remains after shutdown_all server_id=%s generation=%s state=%s",
            session.server_id,
            session.generation,
            session.state,
        )
    for server_id, reservation in lingering_inflight:
        logger.warning(
            "MCP inflight startup remains after shutdown_all server_id=%s generation=%s",
            server_id,
            reservation.generation,
        )


def allow_sessions() -> None:
    global _accepting
    with _registry:
        _accepting = True
        # Tests may re-enable acceptance after shutdown; do not clear `_stopping`.
        # Unreaped children must stay visible until confirmed reaped.


def enforce_idle(session: McpSession, now: float | None = None) -> bool:
    moment = _monotonic() if now is None else now
    with session.lock:
        if session.state != "ready" or session.pending:
            return False
        due = moment - session.last_activity >= _timing.idle_seconds
    if not due:
        return False
    close_session(session)
    return True


def deliver_line(session: McpSession, raw: bytes) -> str:
    """Accept one stdout line for this process generation."""
    with _registry:
        latest = _generations.get(session.server_id, 0)
    if session.generation != latest:
        return "stale"
    with session.lock:
        if session.state in {"stopping", "dead"}:
            return "closed"
    try:
        envelope = decode_line(raw)
    except Exception:
        close_session(session)
        return "violation"
    if envelope.kind == "response":
        return _on_response(session, envelope)
    if envelope.kind == "request":
        return _on_server_request(session, envelope)
    return _on_notification(session, envelope)


def tombstone_size(session: McpSession) -> int:
    return len(session.tombstone_order)


_discovery_listener = None
_generation_listener = None
_after_epoch = None


def set_discovery_listener(callback) -> None:
    """Run after a list-changed notification releases the send lock."""
    global _discovery_listener
    _discovery_listener = callback


def set_generation_listener(callback) -> None:
    """Run after a session closes and its locks are released."""
    global _generation_listener
    _generation_listener = callback


def set_after_epoch(callback) -> None:
    """Run after older sessions close. The loader lock is not held."""
    global _after_epoch
    _after_epoch = callback


def published_session(server_id: str) -> McpSession | None:
    """Return the ready session without taking the loader lock."""
    with _registry:
        session = _sessions.get(server_id)
        if session is None or session.state != "ready":
            return None
        return session


def pin_discovery(session: McpSession, expected: int) -> DiscoveryPin:
    """Acquire the send lock only when discovery is still the observed generation.

    The caller keeps the lock until DiscoveryPin.release. This function does
    not read configuration and does not take the loader lock.
    """
    session.send_lock.acquire()
    try:
        if session.state != "ready" or session._closed or session.discovery != expected:
            raise SessionError("stale_generation")
    except BaseException:
        session.send_lock.release()
        raise
    return DiscoveryPin(session)


def begin_list_frame(session: McpSession, method: str, *, guard: object) -> _Pending:
    """Write one list frame. The send lock is released before the caller waits."""
    if guard is not DISPATCH_GUARD or method not in {"tools/list", "resources/list"}:
        raise SessionError("rejected")
    request_id = str(uuid.uuid4())
    pending = _Pending(_monotonic() + operation_deadline("list"))
    pending.request_id = request_id
    payload = tools_list_request(request_id) if method == "tools/list" else resources_list_request(request_id)
    with session.lock:
        if session.state != "ready":
            raise SessionError("not_ready")
        with session.send_lock:
            if session.state != "ready":
                raise SessionError("not_ready")
            session.pending[request_id] = pending
            try:
                pending.accepted = _write_counted(session, payload, kind="list")
            except SessionError:
                session.pending.pop(request_id, None)
                raise
    return pending


def observe_discovery(session: McpSession, *, guard: object) -> int:
    """Read the discovery generation while holding the send lock, then release it."""
    if guard is not DISPATCH_GUARD:
        raise SessionError("rejected")
    with session.lock:
        if session.state != "ready":
            raise SessionError("not_ready")
        with session.send_lock:
            if session.state != "ready":
                raise SessionError("not_ready")
            return session.discovery


def begin_execution_frame(
    session: McpSession,
    payload: dict,
    *,
    guard: object,
    process_generation: int,
    discovery_generation: int,
) -> _Pending:
    """Write one call or read frame. The caller already holds the write permit.

    The session locks are released before this function returns. A short
    write that accepts at least one byte raises SessionError('unknown') and
    does not continue the frame.
    """
    if guard is not DISPATCH_GUARD or payload.get("method") not in {"tools/call", "resources/read"}:
        raise SessionError("rejected")
    request_id = str(uuid.uuid4())
    pending = _Pending(_monotonic() + operation_deadline("call"))
    pending.request_id = request_id
    frame = dict(payload)
    frame["id"] = request_id
    with session.lock:
        if session.state != "ready" or session.generation != process_generation:
            raise SessionError("stale_generation")
        with session.send_lock:
            if (
                session.state != "ready"
                or session.generation != process_generation
                or session.discovery != discovery_generation
            ):
                raise SessionError("stale_generation")
            session.pending[request_id] = pending
            try:
                pending.accepted = _write_counted(session, frame, kind="call")
            except SessionError:
                session.pending.pop(request_id, None)
                raise
    return pending


def wait_mcp(session: McpSession, pending: _Pending, *, seconds: float) -> str:
    """Classify a finished frame. No cancellation byte is written."""
    if not wait_for(pending.event, seconds):
        with session.send_lock:
            current = session.pending.pop(pending.request_id, None)
            if current is pending:
                _bury(session, pending.request_id, "timeout")
                pending.error = "timeout"
        return "unknown"
    if pending.error == "protocol":
        return "remote_failed"
    if pending.error:
        return "unknown"
    result = pending.result
    if isinstance(result, dict) and result.get("isError") is True:
        return "remote_failed"
    return "completed"


def retire_older_epochs(epoch: int) -> None:
    """Close ready and stopping sessions from prior epochs.

    Stopping sessions are included so unreaped children from retired epochs
    remain under lifecycle ownership and receive another bounded cleanup.
    """
    victims: list[McpSession] = []
    seen: set[int] = set()
    with _registry:
        for session in list(_sessions.values()) + list(_stopping.values()):
            if session.epoch >= epoch:
                continue
            identity = id(session)
            if identity in seen:
                continue
            seen.add(identity)
            victims.append(session)
    for session in victims:
        close_session(session)
    callback = _after_epoch
    if callback is None:
        return
    try:
        callback(epoch)
    except Exception:
        logger.warning("The MCP session shutdown failed.")


def wait_for(event: threading.Event, seconds: float) -> bool:
    if seconds <= 0:
        return event.is_set()
    deadline = _monotonic() + seconds
    while _monotonic() < deadline:
        if event.is_set():
            return True
        _sleep(min(0.01, max(0.0, deadline - _monotonic())))
    return event.is_set()


def _bring_up(server, revision: str, epoch: int, generation: int) -> McpSession:
    session = McpSession(server.id, revision, epoch, generation, environment_sha256(server.env))
    try:
        session.child = _launch(server, generation)
        session.facts = session.child.facts
        if not _epoch_matches(revision, epoch):
            raise SessionError("mismatch")
        with _registry:
            if not _accepting:
                raise SessionError("shutting_down")
        with session.lock:
            session.state = "initializing"
        session.reader = threading.Thread(target=_read_loop, args=(session,), name=f"mcp-{server.id}")
        session.reader.start()
        if not _initialize(session):
            raise SessionError("startup_failed")
        if not _epoch_matches(revision, epoch):
            raise SessionError("mismatch")
        with session.lock:
            session.state = "ready"
            session.last_activity = _monotonic()
        with _registry:
            if not _accepting:
                raise SessionError("shutting_down")
            _sessions[server.id] = session
        return session
    except Exception as exc:
        # Linux failed-spawn may attach a signal/reap-only handle when kill/reap
        # could not confirm exit. Adopt it before abandon so the pid stays in
        # `_stopping` and blocks a new generation for this server identity.
        if (
            session.child is None
            and isinstance(exc, ProcessError)
            and exc.unreaped is not None
        ):
            session.child = exc.unreaped
            session.facts = exc.unreaped.facts
        if session.reader is not None:
            close_session(session)
        else:
            _abandon(session)
        raise


def _launch(server, generation: int):
    override = _spawn_override
    if override is not None:
        facts = assess_executable(server.executable)
        return override(server, facts, generation)
    facts, descriptor = open_executable(server.executable)
    try:
        return start_process(server, facts, generation=generation, descriptor=descriptor)
    finally:
        os.close(descriptor)


def _initialize(session: McpSession) -> bool:
    request_id = str(uuid.uuid4())
    pending = _Pending(_monotonic() + _timing.initialize_seconds)
    with session.send_lock:
        session.pending[request_id] = pending
        _write_locked(session, initialize_request(request_id))
    if wait_for(pending.event, _timing.initialize_seconds) and pending.error is None and accepted_initialize(pending.result):
        with session.send_lock:
            _write_locked(session, initialized_notification())
        return True
    _cancel_outstanding(session, request_id)
    return False


def _epoch_matches(revision: str, epoch: int) -> bool:
    status = mcp_status()
    return bool(status.available and status.revision == revision and status.epoch == epoch)


def _require_current(session: McpSession) -> None:
    status = mcp_status()
    if not status.available or status.revision != session.revision or status.epoch != session.epoch:
        close_session(session)
        raise SessionError("unavailable")


def _cancel_outstanding(session: McpSession, request_id: str) -> None:
    with session.send_lock:
        pending = session.pending.pop(request_id, None)
        if pending is None:
            return
        _bury(session, request_id, "timeout")
        pending.error = "timeout"
        pending.event.set()
        if session.child is not None and session.child.stdin_fd >= 0:
            _write_locked(session, cancellation_notification(request_id))


def _on_response(session: McpSession, envelope) -> str:
    request_id = envelope.request_id if isinstance(envelope.request_id, str) else None
    with session.send_lock:
        pending = session.pending.pop(request_id, None) if request_id is not None else None
        if pending is not None:
            pending.discovery_at_response = session.discovery
            if envelope.error is not None:
                pending.error = "protocol"
            else:
                pending.result = envelope.result
            pending.event.set()
            return "delivered"
        if request_id is not None and session.tombstone_reason.get(request_id) in {"timeout", "cancelled"}:
            return "discarded"
    close_session(session)
    return "violation"


def _on_server_request(session: McpSession, envelope) -> str:
    payload = empty_result(envelope.request_id) if envelope.method == "ping" else method_not_found(envelope.request_id)
    with session.lock:
        if session.state in {"stopping", "dead"}:
            return "closed"
        with session.send_lock:
            if session.state in {"stopping", "dead"}:
                return "closed"
            _write_locked(session, payload)
    return "replied"


def _on_notification(session: McpSession, envelope) -> str:
    method = envelope.method or ""
    cancel_id = cancellation_target(envelope.params) if method == "notifications/cancelled" else None
    with session.lock:
        if session.state in {"stopping", "dead"}:
            return "closed"
        exceeded = not _allow_notice(session)
        if method == "notifications/message":
            session.message_count += 1
        session.last_activity = _monotonic()
    observed = None
    if method in LIST_CHANGED:
        with session.send_lock:
            session.discovery += 1
            session.cache_stale = True
            observed = session.discovery
    if observed is not None:
        listener = _discovery_listener
        if listener is not None:
            try:
                listener(session.server_id, observed)
            except Exception:
                logger.warning("The MCP session shutdown failed.")
    if cancel_id is not None:
        _fail_one(session, cancel_id)
    if exceeded:
        close_session(session)
        return "rate"
    return "accepted"


def _fail_one(session: McpSession, request_id: str) -> None:
    with session.send_lock:
        pending = session.pending.pop(request_id, None)
        if pending is None:
            return
        _bury(session, request_id, "cancelled")
        pending.error = "cancelled"
        pending.event.set()


def _allow_notice(session: McpSession) -> bool:
    now = _monotonic()
    while session.notices and now - session.notices[0] >= 1:
        session.notices.popleft()
    if len(session.notices) >= _timing.notify_limit:
        return False
    session.notices.append(now)
    return True


def _bury(session: McpSession, request_id: str, reason: str) -> None:
    if request_id in session.tombstone_reason:
        session.tombstone_reason[request_id] = reason
        return
    session.tombstone_order.append(request_id)
    session.tombstone_reason[request_id] = reason
    while len(session.tombstone_order) > _timing.max_tombstones:
        expired = session.tombstone_order.popleft()
        session.tombstone_reason.pop(expired, None)


def _write_locked(session: McpSession, payload: dict) -> None:
    if session.state not in {"starting", "initializing", "ready"}:
        raise SessionError("not_ready")
    try:
        raw = encode_message(payload)
    except Exception as exc:
        raise SessionError("exited") from exc
    try:
        _transfer_frame(session, raw, kind="initialize")
    except SessionError as exc:
        raise SessionError("exited") from exc


def _write_counted(session: McpSession, payload: dict, *, kind: str) -> int:
    """Write one frame. Zero accepted bytes and a later short write are different failures."""
    if session.state not in {"starting", "initializing", "ready"} or session._stream_broken:
        raise SessionError("not_dispatched")
    try:
        raw = encode_message(payload)
    except Exception as exc:
        raise SessionError("not_dispatched") from exc
    return _transfer_frame(session, raw, kind=kind)


def _transfer_frame(session: McpSession, raw: bytes, *, kind: str) -> int:
    """Write one complete frame without blocking past the write budget.

    The caller holds the send lock. The descriptor is nonblocking only for
    this transfer and is restored before the lock is released. A timeout or
    error after at least one byte marks the stream broken so a later frame
    cannot be appended to a partial line.
    """
    child = session.child
    if child is None or child.stdin_fd < 0:
        raise SessionError("not_dispatched")
    descriptor = child.stdin_fd
    deadline = time.monotonic() + write_budget(kind)
    try:
        blocking = os.get_blocking(descriptor)
    except OSError as exc:
        raise SessionError("not_dispatched") from exc
    accepted = 0
    watch = [session.wake_r] if session.wake_r >= 0 else []
    try:
        try:
            os.set_blocking(descriptor, False)
        except OSError as exc:
            raise SessionError("not_dispatched") from exc
        view = memoryview(raw)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _raise_write(session, accepted)
            try:
                readable, writable, _ = select.select(watch, [descriptor], [], remaining)
            except (OSError, ValueError) as exc:
                _raise_write(session, accepted, exc)
            if session.wake_r in readable:
                _raise_write(session, accepted)
            if descriptor not in writable:
                continue
            try:
                written = os.write(descriptor, view)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno == errno.EPIPE:
                    session._stream_broken = True
                    if session.state in {"starting", "initializing", "ready"}:
                        session.state = "stopping"
                _raise_write(session, accepted, exc)
            if written <= 0:
                _raise_write(session, accepted)
            accepted += written
            view = view[written:]
    finally:
        try:
            os.set_blocking(descriptor, blocking)
        except OSError:
            pass
    session.last_activity = _monotonic()
    return accepted


def _raise_write(session: McpSession, accepted: int, cause: BaseException | None = None) -> None:
    if accepted > 0:
        session._stream_broken = True
        if session.state in {"starting", "initializing", "ready"}:
            session.state = "stopping"
        error = SessionError("unknown")
    else:
        error = SessionError("not_dispatched")
    if cause is not None:
        raise error from cause
    raise error


def _notify_generation(server_id: str, generation: int) -> None:
    listener = _generation_listener
    if listener is None:
        return
    try:
        listener(server_id, generation)
    except Exception:
        logger.warning("The MCP session shutdown failed.")


def _read_loop(session: McpSession) -> None:
    child = session.child
    if child is None:
        return
    stdout = child.stdout_fd
    stderr = child.stderr_fd
    watched = [item for item in (stdout, stderr, session.wake_r) if item >= 0]
    for descriptor in watched:
        os.set_blocking(descriptor, False)
    try:
        while True:
            with session.lock:
                if session.state in {"stopping", "dead"}:
                    return
            if enforce_idle(session):
                return
            try:
                readable, _, _ = select.select(watched, [], [], 0.05)
            except (OSError, ValueError):
                return
            if session.wake_r in readable:
                _read_some(session.wake_r)
            if stderr in readable:
                data = _read_some(stderr)
                if data:
                    session.stderr.extend(data)
            if stdout in readable:
                data = _read_some(stdout)
                if data is None:
                    continue
                if data == b"":
                    if session.state == "ready" or session.state == "initializing":
                        close_session(session)
                    return
                session.stdout_buffer.extend(data)
                if not _consume_stdout(session):
                    return
    except Exception:
        logger.warning("The MCP session failed.")
        if session.state not in {"stopping", "dead"}:
            close_session(session)


def _consume_stdout(session: McpSession) -> bool:
    while True:
        if len(session.stdout_buffer) > MAX_LINE_BYTES and b"\n" not in session.stdout_buffer:
            close_session(session)
            return False
        newline = session.stdout_buffer.find(b"\n")
        if newline < 0:
            return True
        line = bytes(session.stdout_buffer[:newline])
        del session.stdout_buffer[: newline + 1]
        if len(line) > MAX_LINE_BYTES:
            close_session(session)
            return False
        outcome = deliver_line(session, line)
        if outcome in {"violation", "rate"} or session.state in {"stopping", "dead"}:
            return False
    return True


def _read_some(descriptor: int) -> bytes | None:
    try:
        return os.read(descriptor, 65536)
    except BlockingIOError:
        return None
    except OSError:
        return b""


def _shutdown_child(session: McpSession) -> bool:
    """Stop the child. Return True only when the process has been reaped."""
    from app.mcp.process import SIGNAL_DENIED, SIGNAL_GONE

    child = session.child
    if child is None:
        return True
    denied = False
    try:
        child.close_stdin()
    except OSError:
        logger.warning("The MCP session shutdown failed.")
    _wait_grace(_timing.stdin_grace, child.alive)
    try:
        if child.alive():
            outcome = child.signal_group(signal.SIGTERM)
            if outcome == SIGNAL_DENIED:
                denied = True
            elif outcome == SIGNAL_GONE:
                pass
            else:
                # SIGNAL_DELIVERED or legacy test doubles without a return value.
                _wait_grace(_timing.term_grace, child.alive)
        if child.alive() and not denied:
            outcome = child.signal_group(signal.SIGKILL)
            if outcome == SIGNAL_DENIED:
                denied = True
            elif outcome == SIGNAL_GONE:
                pass
            else:
                _wait_grace(_timing.term_grace, child.alive)
    except OSError:
        logger.warning("The MCP session shutdown failed.")
        denied = True
    # Every shutdown reap is bounded. Deadline expiry leaves the session stopping.
    try:
        confirmed = child.reap(wait_seconds=_timing.term_grace)
    except TypeError:
        # Test doubles may still expose the older reap() signature.
        child.reap()
        confirmed = not child.alive()
    except OSError:
        logger.warning("The MCP session shutdown failed.")
        return False
    if not confirmed:
        logger.warning("The MCP session shutdown failed.")
        return False
    return True


def _wait_grace(seconds: float, alive) -> None:
    deadline = _monotonic() + seconds
    while _monotonic() < deadline:
        if not alive():
            return
        _sleep(min(0.01, max(0.0, deadline - _monotonic())))


def _abandon(session: McpSession) -> None:
    """Shut down a failed startup that never joined `_sessions`.

    Uses the same `_stopping` ownership as `close_session`: an unreaped child
    stays tracked for process accounting, open_session blocking, and shutdown.

    Registry publication always runs in `finally`, even when `_shutdown_child`
    raises (for example InterruptedError from waitpid during grace). Shutdown
    exceptions are logged and swallowed so `_bring_up` can still raise the
    original startup failure after ownership is retained.
    """
    with session.lock:
        session.state = "stopping"
        session._closing = True
    terminated = False
    try:
        terminated = _shutdown_child(session)
    except Exception:
        logger.warning("The MCP session shutdown failed.")
        terminated = False
    finally:
        # Never claim dead/reaped unless the child is absent or confirmed reaped.
        if terminated and session.child is not None and not _child_reaped(session):
            terminated = False
        with _registry:
            _publish_lifecycle_locked(session, terminated=terminated)
        with session.lock:
            session._closed = True
            session.state = "dead" if terminated else "stopping"
            session._closing = False
        _close_pipe(session, "wake_r")
        _close_pipe(session, "wake_w")
        session.close_done.set()


def _wake(session: McpSession) -> None:
    if session.wake_w >= 0:
        try:
            os.write(session.wake_w, b"\0")
        except OSError:
            return


def _close_pipe(session: McpSession, name: str) -> None:
    descriptor = getattr(session, name)
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except OSError:
            pass
        setattr(session, name, -1)


set_epoch_listener(retire_older_epochs)


def _max_processes() -> int:
    """Concurrent MCP children allowed (R-17: configurable, default 2)."""
    if _timing.max_processes is not None:
        return _timing.max_processes
    from app.core.config import get_settings

    return get_settings().mcp_max_processes
