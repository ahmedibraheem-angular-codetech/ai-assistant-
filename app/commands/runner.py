"""Run one fixed argument array outside the server process.

shell is never enabled. The child stays in the caller's process group.
The registered binaries do not call setsid or setpgrp and do not spawn
children, so killing that group stops them. A timeout also sends SIGKILL
to the child and to children proc_listchildpids can see at that moment.
A descendant that has left the process group is outside this guarantee.
"""

import ctypes
import os
import select
import signal
import time

from app.tools.exceptions import ToolExecutionError

_CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"}


def supervise(
    argv: list[str],
    *,
    directory_fd: int,
    timeout: float,
    max_output: int,
    cpu_seconds: int,
    max_file_bytes: int,
    file_fd: int | None = None,
) -> dict:
    if not argv or any(not isinstance(part, str) or "\x00" in part or "\n" in part for part in argv):
        raise ToolExecutionError("The command arguments are not valid.")
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    try:
        pid = os.fork()
    except OSError as exc:
        os.close(stdout_read)
        os.close(stdout_write)
        os.close(stderr_read)
        os.close(stderr_write)
        raise ToolExecutionError("The command could not be started.") from exc
    if pid == 0:
        _child(
            argv,
            directory_fd,
            stdout_write,
            stderr_write,
            stdout_read,
            stderr_read,
            cpu_seconds,
            max_file_bytes,
            file_fd,
        )
    os.close(stdout_write)
    os.close(stderr_write)
    stdout = bytearray()
    stderr = bytearray()
    timed_out = False
    truncated = False
    open_reads = [stdout_read, stderr_read]
    deadline = time.monotonic() + timeout
    try:
        while open_reads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            readable, _, _ = select.select(open_reads, [], [], min(remaining, 0.2))
            if not readable:
                continue
            for descriptor in readable:
                chunk = os.read(descriptor, 4096)
                target = stdout if descriptor == stdout_read else stderr
                if not chunk:
                    open_reads.remove(descriptor)
                    continue
                room = max_output - len(target)
                if room <= 0:
                    truncated = True
                    open_reads.clear()
                    break
                target.extend(chunk[:room])
                if len(chunk) > room:
                    truncated = True
                    open_reads.clear()
                    break
        if timed_out or truncated:
            _kill_tree(pid)
        exit_code = _wait(pid)
    finally:
        os.close(stdout_read)
        os.close(stderr_read)
        _wait(pid)
    return {
        "exit_code": exit_code,
        "stdout": bytes(stdout),
        "stderr": bytes(stderr),
        "timed_out": timed_out,
        "truncated": truncated,
    }


def _child(
    argv: list[str],
    directory_fd: int,
    stdout_write: int,
    stderr_write: int,
    stdout_read: int,
    stderr_read: int,
    cpu_seconds: int,
    max_file_bytes: int,
    file_fd: int | None,
) -> None:
    try:
        os.fchdir(directory_fd)
        if file_fd is not None:
            os.dup2(file_fd, 3)
        os.dup2(stdout_write, 1)
        os.dup2(stderr_write, 2)
        null_fd = os.open("/dev/null", os.O_RDONLY)
        os.dup2(null_fd, 0)
        keep = {0, 1, 2}
        if file_fd is not None:
            keep.add(3)
        for descriptor in range(3, 64):
            if descriptor not in keep:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        _limits(cpu_seconds, max_file_bytes)
        os.execve(argv[0], argv, _CLEAN_ENV)
    except OSError:
        os._exit(127)
    os._exit(127)


def _limits(cpu_seconds: int, max_file_bytes: int) -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    except (OSError, ValueError):
        return


def _wait(pid: int) -> int | None:
    try:
        waited, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return None
    if waited == 0:
        try:
            _waited, status = os.waitpid(pid, 0)
        except ChildProcessError:
            return None
    if os.WIFEXITED(status):
        return int(os.WEXITSTATUS(status))
    if os.WIFSIGNALED(status):
        return -int(os.WTERMSIG(status))
    return None


def _kill_tree(pid: int) -> None:
    for child in _child_pids(pid):
        _kill_tree(child)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _child_pids(pid: int) -> list[int]:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.proc_listchildpids
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        function.restype = ctypes.c_int
        count = int(function(pid, None, 0))
        if count <= 0:
            return []
        buffer = (ctypes.c_int * count)()
        written = int(function(pid, ctypes.byref(buffer), ctypes.sizeof(buffer)))
        size = written if 0 < written <= count else count
        return [int(buffer[index]) for index in range(size) if int(buffer[index]) > 0]
    except (AttributeError, OSError):
        return []
