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
