"""WP-02 post-implementation security remediation regressions."""

from __future__ import annotations

import errno
import fcntl
import os
import select
import signal
import stat
import sys
import time

import pytest

from app.mcp import process as process_module
from app.mcp.process import (
    SIGNAL_DENIED,
    SIGNAL_DELIVERED,
    SIGNAL_GONE,
    ChildProcess,
    ExecutableFacts,
    ProcessError,
    _EXEC_FAIL,
    _EXEC_READY,
    _close_inherited,
    _confirm_linux_exec,
    _error_pipe,
    _list_fds,
    _read_exec_report,
    _trusted_parents,
    _write_all,
    _write_fail_errno,
)
pytest_plugins = ["tests.test_mcp_process"]


def _child(pid: int = 424242) -> ChildProcess:
    return ChildProcess(
        pid=pid,
        pgid=pid,
        stdin_fd=-1,
        stdout_fd=-1,
        stderr_fd=-1,
        facts=ExecutableFacts(1, 2, "abc", "/bin/true"),
        generation=1,
    )


def test_signal_group_distinguishes_process_lookup_error(monkeypatch):
    child = _child()

    def boom(_pgid, _signum):
        raise ProcessLookupError()

    monkeypatch.setattr(os, "killpg", boom)
    assert child.signal_group(signal.SIGTERM) == SIGNAL_GONE


def test_signal_group_distinguishes_permission_error(monkeypatch):
    child = _child()

    def boom(_pgid, _signum):
        raise PermissionError()

    monkeypatch.setattr(os, "killpg", boom)
    assert child.signal_group(signal.SIGKILL) == SIGNAL_DENIED


def test_signal_group_reports_delivery(monkeypatch):
    child = _child()
    monkeypatch.setattr(os, "killpg", lambda *_a: None)
    assert child.signal_group(signal.SIGTERM) == SIGNAL_DELIVERED


def test_fd_enumeration_fails_closed_for_both_directories(monkeypatch):
    def unavailable(_path, _flags):
        raise OSError(errno.ENOENT, "missing")

    monkeypatch.setattr(os, "open", unavailable)
    with pytest.raises(ProcessError) as caught:
        _list_fds()
    assert caught.value.code == "spawn_failed"


def test_close_inherited_fails_closed_when_enumeration_unavailable(monkeypatch):
    monkeypatch.setattr(process_module, "_list_fds", lambda: (_ for _ in ()).throw(ProcessError("spawn_failed")))
    with pytest.raises(ProcessError) as caught:
        _close_inherited({0, 1, 2})
    assert caught.value.code == "spawn_failed"


def test_close_inherited_aborts_on_unexpected_close_failure(monkeypatch):
    keep = {0, 1, 2}
    target = 99

    monkeypatch.setattr(process_module, "_list_fds", lambda: [0, 1, 2, target])

    def boom(descriptor):
        if descriptor == target:
            raise OSError(errno.EIO, "close failed")
        raise AssertionError(descriptor)

    monkeypatch.setattr(os, "close", boom)
    with pytest.raises(ProcessError) as caught:
        _close_inherited(keep)
    assert caught.value.code == "spawn_failed"


def test_close_inherited_tolerates_already_closed_descriptor(monkeypatch):
    keep = {0, 1, 2}
    target = 77
    closed: list[int] = []

    monkeypatch.setattr(process_module, "_list_fds", lambda: [0, 1, 2, target])

    def boom(descriptor):
        closed.append(descriptor)
        raise OSError(errno.EBADF, "already closed")

    monkeypatch.setattr(os, "close", boom)
    _close_inherited(keep)
    assert closed == [target]


def test_darwin_spawn_fails_closed_when_fd_cleanup_cannot_run(binaries, monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("posix_spawn fail-closed coverage runs on macOS.")
    from app.mcp.process import open_executable, start_process

    class Server:
        executable = str(binaries["echo"].resolve())
        args = ()
        env = ()

    monkeypatch.setattr(process_module, "_list_fds", lambda: (_ for _ in ()).throw(ProcessError("spawn_failed")))
    facts, descriptor = open_executable(Server.executable)
    try:
        with pytest.raises(ProcessError) as caught:
            start_process(Server(), facts, generation=1, descriptor=descriptor)
        assert caught.value.code == "spawn_failed"
    finally:
        os.close(descriptor)


def test_exec_ready_then_eof_is_success():
    read_end, write_end = _error_pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_end)
            _write_all(write_end, _EXEC_READY)
            os.close(write_end)
            os._exit(0)
        except Exception:
            os._exit(2)
    os.close(write_end)
    outcome, err = _confirm_linux_exec(read_end, 1.0)
    os.close(read_end)
    os.waitpid(pid, 0)
    assert outcome == "success"
    assert err is None


def test_exec_eof_without_ready_is_failure():
    read_end, write_end = _error_pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_end)
            os.close(write_end)
            os._exit(127)
        except Exception:
            os._exit(2)
    os.close(write_end)
    outcome, err = _confirm_linux_exec(read_end, 1.0)
    os.close(read_end)
    os.waitpid(pid, 0)
    assert outcome == "failure"
    assert err is None


def test_exec_fail_report_preserves_errno():
    read_end, write_end = _error_pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_end)
            _write_all(write_end, _EXEC_READY)
            _write_fail_errno(write_end, errno.ENOEXEC)
            os.close(write_end)
            os._exit(127)
        except Exception:
            os._exit(2)
    os.close(write_end)
    outcome, err = _confirm_linux_exec(read_end, 1.0)
    os.close(read_end)
    os.waitpid(pid, 0)
    assert outcome == "failure"
    assert err == errno.ENOEXEC


def test_exec_error_pipe_timeout_when_child_stalls():
    read_end, write_end = _error_pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_end)
            time.sleep(5)
            os._exit(127)
        except Exception:
            os._exit(2)
    os.close(write_end)
    with pytest.raises(TimeoutError):
        _read_exec_report(read_end, 0.05)
    os.close(read_end)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)


def test_write_all_retries_partial_writes(monkeypatch):
    writes = [1, 1, 3]
    sent: list[bytes] = []

    def partial(_fd, data):
        n = writes.pop(0)
        sent.append(bytes(data[:n]))
        return n

    monkeypatch.setattr(os, "write", partial)
    _write_all(3, b"ABCDE")
    assert b"".join(sent) == b"ABCDE"


def test_read_exec_report_assembles_partial_reads(monkeypatch):
    chunks = [_EXEC_READY, _EXEC_FAIL, (errno.EACCES).to_bytes(4, "big")]
    monkeypatch.setattr(select, "select", lambda *_a, **_k: ([4], [], []))

    def reader(_fd, _n):
        return chunks.pop(0)

    monkeypatch.setattr(os, "read", reader)
    payload = _read_exec_report(4, 1.0)
    assert payload == _EXEC_READY + _EXEC_FAIL + (errno.EACCES).to_bytes(4, "big")
    monkeypatch.undo()
    read_end, write_end = _error_pipe()
    try:
        os.write(write_end, payload)
        os.close(write_end)
        write_end = -1
        outcome, err = _confirm_linux_exec(read_end, 1.0)
    finally:
        os.close(read_end)
        if write_end >= 0:
            os.close(write_end)
    assert outcome == "failure"
    assert err == errno.EACCES


def test_error_pipe_sets_cloexec():
    read_end, write_end = _error_pipe()
    assert fcntl.fcntl(read_end, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    assert fcntl.fcntl(write_end, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    os.close(read_end)
    os.close(write_end)


def test_write_fail_errno_returns_false_when_write_fails(monkeypatch):
    monkeypatch.setattr(
        process_module,
        "_write_all",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.EIO, "broken")),
    )
    assert _write_fail_errno(3, errno.ENOENT) is False


def test_failed_fail_report_after_ready_times_out_instead_of_success():
    """After READY, if FAIL cannot be written, the write end stays open.

    Parent must time out (fail closed), not observe success-shaped R+EOF.
    """
    read_end, write_end = _error_pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_end)
            _write_all(write_end, _EXEC_READY)
            # Simulate FAIL-report write failure after a failed exec: park with
            # the write end still open instead of closing into R+EOF.
            process_module._park_until_killed()
        except Exception:
            os._exit(2)
    os.close(write_end)
    with pytest.raises(TimeoutError):
        _confirm_linux_exec(read_end, 0.05)
    os.close(read_end)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Real execveat path runs on Linux.")
def test_linux_execveat_fail_report_write_failure_does_not_return_child(binaries, monkeypatch):
    from app.mcp.process import open_executable, start_process

    path = str(binaries["echo"].resolve())
    facts, descriptor = open_executable(path, platform="linux")

    class Server:
        executable = path
        args = ()
        env = ()

    def fail_execveat(*_args, **_kwargs):
        raise OSError(errno.ENOEXEC, "Exec format error")

    real_write_all = process_module._write_all

    def write_all(fd, payload):
        if payload[:1] == _EXEC_FAIL:
            raise OSError(errno.EIO, "cannot report failure")
        return real_write_all(fd, payload)

    monkeypatch.setattr(process_module, "_execveat", fail_execveat)
    monkeypatch.setattr(process_module, "_write_all", write_all)
    monkeypatch.setattr(process_module, "EXEC_PIPE_SECONDS", 0.1)
    try:
        with pytest.raises(ProcessError) as caught:
            start_process(Server(), facts, generation=7, descriptor=descriptor, platform="linux")
        assert caught.value.code == "spawn_failed"
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Real execveat coverage runs on Linux.")
def test_linux_execveat_success_uses_verified_descriptor(binaries):
    from app.mcp.process import open_executable, start_process

    path = str(binaries["echo"].resolve())
    facts, descriptor = open_executable(path, platform="linux")

    class Server:
        executable = path
        args = ("linux-ok",)
        env = ()

    try:
        child = start_process(Server(), facts, generation=4, descriptor=descriptor, platform="linux")
        assert os.read(child.stdout_fd, 64) == b"linux-ok\n"
        child.close_stdin()
        assert child.reap(wait_seconds=2) is True
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Real execveat errno coverage runs on Linux.")
def test_linux_execveat_failure_propagates_errno(binaries, monkeypatch):
    from app.mcp.process import open_executable, start_process

    path = str(binaries["echo"].resolve())
    facts, descriptor = open_executable(path, platform="linux")

    class Server:
        executable = path
        args = ()
        env = ()

    def fail_execveat(syscall, number, descriptor_number, argv, envp):
        raise OSError(errno.ENOEXEC, "Exec format error")

    monkeypatch.setattr(process_module, "_execveat", fail_execveat)
    try:
        with pytest.raises(ProcessError) as caught:
            start_process(Server(), facts, generation=5, descriptor=descriptor, platform="linux")
        assert caught.value.code == "spawn_failed"
        assert caught.value.errno_value == errno.ENOEXEC
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Real execveat stall coverage runs on Linux.")
def test_linux_exec_pipe_timeout_cleans_up_child(binaries, monkeypatch):
    from app.mcp.process import open_executable, start_process

    path = str(binaries["echo"].resolve())
    facts, descriptor = open_executable(path, platform="linux")

    class Server:
        executable = path
        args = ()
        env = ()

    def stall(*_args, **_kwargs):
        time.sleep(5)

    monkeypatch.setattr(process_module, "_write_all", stall)
    monkeypatch.setattr(process_module, "EXEC_PIPE_SECONDS", 0.05)
    try:
        with pytest.raises(ProcessError) as caught:
            start_process(Server(), facts, generation=6, descriptor=descriptor, platform="linux")
        assert caught.value.code == "spawn_failed"
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def test_trusted_parents_rejects_paths_deeper_than_supported_limit(monkeypatch):
    depth = 0

    class Info:
        st_mode = stat.S_IFDIR | 0o700
        st_uid = os.getuid()

    def fake_lstat(_path):
        nonlocal depth
        depth += 1
        return Info()

    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(os.path, "dirname", lambda path: path + "/x")
    monkeypatch.setattr(os.path, "abspath", lambda path: "/start")
    with pytest.raises(ProcessError) as caught:
        _trusted_parents("/start/leaf")
    assert caught.value.code == "executable"
    assert depth == 64


def test_release_failed_linux_child_attaches_unreaped_when_kill_and_reap_false(monkeypatch):
    facts = ExecutableFacts(1, 2, "abc", "/usr/bin/true")
    closed: list[int] = []
    monkeypatch.setattr(process_module, "_kill_and_reap", lambda _pid: False)
    monkeypatch.setattr(process_module, "_close_many", lambda fds: closed.extend(fds))
    error = process_module._release_failed_linux_child(
        424242,
        facts,
        9,
        [11, 12, 13],
        errno_value=errno.ENOEXEC,
    )
    assert error.code == "spawn_failed"
    assert error.errno_value == errno.ENOEXEC
    assert error.unreaped is not None
    assert error.unreaped.pid == 424242
    assert error.unreaped.pgid == 424242
    assert error.unreaped.generation == 9
    assert error.unreaped.facts is facts
    assert error.unreaped.reaped is False
    assert error.unreaped.stdin_fd == -1
    assert error.unreaped.stdout_fd == -1
    assert error.unreaped.stderr_fd == -1
    assert closed == [11, 12, 13]


def test_release_failed_linux_child_omits_unreaped_when_reaped(monkeypatch):
    facts = ExecutableFacts(1, 2, "abc", "/usr/bin/true")
    monkeypatch.setattr(process_module, "_kill_and_reap", lambda _pid: True)
    monkeypatch.setattr(process_module, "_close_many", lambda _fds: None)
    error = process_module._release_failed_linux_child(7, facts, 1, [1, 2], errno_value=1)
    assert error.unreaped is None
    assert error.code == "spawn_failed"


def test_spawn_linux_failure_path_surfaces_unreaped_handle(monkeypatch):
    """Deterministic `_spawn_linux` failure when `_kill_and_reap` returns False."""
    facts = ExecutableFacts(1, 2, "abc", "/usr/bin/true")

    class Plan:
        argv = ("/usr/bin/true",)
        env = ()
        path = "/usr/bin/true"

    monkeypatch.setattr(process_module, "execveat_syscall_number", lambda: 281)
    monkeypatch.setattr(process_module, "_cstring_array", lambda values: values)
    monkeypatch.setattr(process_module, "_bind_syscall", lambda: object())
    monkeypatch.setattr(os, "fork", lambda: 515151)
    monkeypatch.setattr(process_module, "_confirm_linux_exec", lambda *_a, **_k: ("failure", errno.ENOEXEC))
    monkeypatch.setattr(process_module, "_kill_and_reap", lambda _pid: False)

    with pytest.raises(ProcessError) as caught:
        process_module._spawn_linux(Plan(), facts, 3, descriptor=99)
    assert caught.value.unreaped is not None
    assert caught.value.unreaped.pid == 515151
    assert caught.value.unreaped.reaped is False
    assert caught.value.unreaped.stdout_fd == -1
    assert caught.value.errno_value == errno.ENOEXEC


def test_spawn_linux_confirm_process_error_reaped_propagates_without_self_cause(monkeypatch):
    """Reaped cleanup must re-raise the original ProcessError, never ``raise exc from exc``."""
    facts = ExecutableFacts(1, 2, "abc", "/usr/bin/true")
    original = ProcessError("spawn_failed", errno_value=errno.EIO)

    class Plan:
        argv = ("/usr/bin/true",)
        env = ()
        path = "/usr/bin/true"

    def boom(*_a, **_k):
        raise original

    monkeypatch.setattr(process_module, "execveat_syscall_number", lambda: 281)
    monkeypatch.setattr(process_module, "_cstring_array", lambda values: values)
    monkeypatch.setattr(process_module, "_bind_syscall", lambda: object())
    monkeypatch.setattr(os, "fork", lambda: 616161)
    monkeypatch.setattr(process_module, "_confirm_linux_exec", boom)
    monkeypatch.setattr(process_module, "_kill_and_reap", lambda _pid: True)

    with pytest.raises(ProcessError) as caught:
        process_module._spawn_linux(Plan(), facts, 4, descriptor=99)
    assert caught.value is original
    assert caught.value.code == "spawn_failed"
    assert caught.value.errno_value == errno.EIO
    assert caught.value.unreaped is None
    assert caught.value.__cause__ is None


def test_reap_deadline_retains_stdout_and_stderr(monkeypatch):
    stdin_r, stdin_w = os.pipe()
    stdout_r, stdout_w = os.pipe()
    stderr_r, stderr_w = os.pipe()
    child = ChildProcess(
        pid=606060,
        pgid=606060,
        stdin_fd=stdin_w,
        stdout_fd=stdout_r,
        stderr_fd=stderr_r,
        facts=ExecutableFacts(1, 2, "abc", "/usr/bin/true"),
        generation=1,
    )
    monkeypatch.setattr(os, "waitpid", lambda *_a, **_k: (0, 0))
    assert child.reap(wait_seconds=0.02) is False
    assert child.reaped is False
    assert child.stdin_fd == stdin_w
    assert child.stdout_fd == stdout_r
    assert child.stderr_fd == stderr_r
    # Confirmed reap still releases parent ends.
    child.reaped = True
    assert child.reap(wait_seconds=0) is True
    assert child.stdin_fd == -1
    assert child.stdout_fd == -1
    assert child.stderr_fd == -1
    for descriptor in (stdin_r, stdout_w, stderr_w):
        os.close(descriptor)


def test_release_failed_linux_child_closes_fds_when_kill_and_reap_raises(monkeypatch):
    """Unexpected kill/reap failure still closes FDs and surfaces adoptable unreaped."""
    facts = ExecutableFacts(1, 2, "abc", "/usr/bin/true")
    closed: list[int] = []

    def boom(_pid):
        raise RuntimeError("kill/reap exploded")

    monkeypatch.setattr(process_module, "_kill_and_reap", boom)
    monkeypatch.setattr(process_module, "_close_many", lambda fds: closed.extend(fds))
    with pytest.raises(ProcessError) as caught:
        process_module._release_failed_linux_child(99, facts, 2, [21, 22, 23])
    assert caught.value.code == "spawn_failed"
    assert caught.value.unreaped is not None
    assert caught.value.unreaped.pid == 99
    assert caught.value.unreaped.pgid == 99
    assert caught.value.unreaped.generation == 2
    assert caught.value.unreaped.facts is facts
    assert caught.value.unreaped.stdin_fd == -1
    assert caught.value.unreaped.stdout_fd == -1
    assert caught.value.unreaped.stderr_fd == -1
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert "kill/reap exploded" in str(caught.value.__cause__)
    assert closed == [21, 22, 23]


def test_reap_failed_child_retries_interrupted_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def waitpid(_pid, _flags):
        calls["n"] += 1
        if calls["n"] == 1:
            raise InterruptedError()
        return (123, 0)

    monkeypatch.setattr(os, "waitpid", waitpid)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    assert process_module._reap_failed_child(123, wait_seconds=1.0) is True
    assert calls["n"] == 2


def test_reap_failed_child_retries_eintr_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def waitpid(_pid, _flags):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EINTR, "Interrupted system call")
        return (123, 0)

    monkeypatch.setattr(os, "waitpid", waitpid)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    assert process_module._reap_failed_child(123, wait_seconds=1.0) is True
    assert calls["n"] == 2


def test_reap_failed_child_interrupted_past_deadline_returns_false(monkeypatch):
    clock = {"t": 100.0}

    def mono():
        return clock["t"]

    def waitpid(_pid, _flags):
        clock["t"] += 0.5
        raise InterruptedError()

    monkeypatch.setattr(time, "monotonic", mono)
    monkeypatch.setattr(os, "waitpid", waitpid)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    assert process_module._reap_failed_child(55, wait_seconds=0.2) is False


def test_reap_failed_child_treats_echild_as_reaped(monkeypatch):
    def waitpid(_pid, _flags):
        raise OSError(errno.ECHILD, "No child processes")

    monkeypatch.setattr(os, "waitpid", waitpid)
    assert process_module._reap_failed_child(77, wait_seconds=0.1) is True


def test_release_failed_linux_child_preserves_unreaped_pid_when_not_reaped(monkeypatch):
    facts = ExecutableFacts(9, 8, "digest", "/usr/bin/true")
    monkeypatch.setattr(process_module, "_kill_and_reap", lambda _pid: False)
    monkeypatch.setattr(process_module, "_close_many", lambda _fds: None)
    error = process_module._release_failed_linux_child(
        31337,
        facts,
        4,
        [1],
        errno_value=errno.ENOEXEC,
    )
    assert error.unreaped is not None
    assert error.unreaped.pid == 31337
    assert error.unreaped.pgid == 31337
    assert error.unreaped.generation == 4
    assert error.unreaped.facts is facts
