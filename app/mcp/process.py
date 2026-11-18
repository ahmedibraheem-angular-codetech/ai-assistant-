"""Spawn and reap one local MCP stdio process.

Linux executes the opened descriptor with execveat. The executable
descriptor stays open until that syscall. Every other inherited descriptor
is closed.

macOS has no fexecve. posix_spawn resolves the configured path after the
descriptor hash. Every parent directory and the executable itself must be
owned by the service user or root and must not be group or world writable,
so an untrusted different user cannot replace the file between verification
and execution. The same service user can still replace a file they own.

A descendant that calls setsid or setpgrp can leave the process group.
A parent crash can leave an orphan. Startup does not adopt or signal an
unknown pid. The stderr ring is never logged.
"""

from __future__ import annotations

import errno
import hashlib
import os
import signal
import stat
import sys
import time
from dataclasses import dataclass, field

from app.mcp.loader import ServerConfig, blocked_location

MAX_EXECUTABLE_BYTES = 64 * 1024 * 1024
STDERR_LIMIT = 64 * 1024
_MACHO = frozenset(
    {
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }
)
_AT_EMPTY_PATH = 0x1000
_EXECVEAT = {"x86_64": 322, "aarch64": 281, "armv7l": 387, "armv6l": 387, "i386": 358, "i686": 358}
_POSIX_SPAWN_SETSID = 0x0400
_EXEC_READY = b"R"
_EXEC_FAIL = b"F"
EXEC_PIPE_SECONDS = 2.0
REAP_FAILED_SECONDS = 2.0
MACOS_PATH_LOOKUP = (
    "macOS posix_spawn resolves the path after the descriptor hash. "
    "Parent directories and the executable must be owned by the service "
    "user or root and must not be group or world writable, so an untrusted "
    "different user cannot replace the file. The same service user can "
    "still replace a file they own."
)


class ProcessError(Exception):
    def __init__(self, code: str, *, errno_value: int | None = None) -> None:
        self.code = code
        self.errno_value = errno_value
        super().__init__("The MCP process could not be started.")


SIGNAL_DELIVERED = "delivered"
SIGNAL_GONE = "gone"
SIGNAL_DENIED = "denied"


@dataclass(frozen=True, slots=True)
class ExecutableFacts:
    device: int
    inode: int
    sha256: str
    path: str

    def __repr__(self) -> str:
        return f"ExecutableFacts(device={self.device}, inode={self.inode})"


@dataclass(frozen=True, slots=True)
class SpawnPlan:
    method: str
    path: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    cwd: str

    def __repr__(self) -> str:
        return f"SpawnPlan(method={self.method!r}, cwd={self.cwd!r}, argc={len(self.argv)})"


@dataclass
class ChildProcess:
    pid: int
    pgid: int
    stdin_fd: int
    stdout_fd: int
    stderr_fd: int
    facts: ExecutableFacts
    generation: int
    signals: list[int] = field(default_factory=list)
    reaped: bool = False
    stdin_closed: bool = False

    def alive(self) -> bool:
        if self.reaped:
            return False
        try:
            waited, _status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.reaped = True
            return False
        if waited == 0:
            return True
        self.reaped = True
        return False

    def close_stdin(self) -> None:
        if self.stdin_fd >= 0:
            os.close(self.stdin_fd)
            self.stdin_fd = -1
        self.stdin_closed = True

    def signal_group(self, signum: int) -> str:
        """Signal the process group.

        Returns SIGNAL_DELIVERED, SIGNAL_GONE (ProcessLookupError), or
        SIGNAL_DENIED (PermissionError). Authorization failures are not
        treated as successful termination.
        """
        self.signals.append(signum)
        try:
            os.killpg(self.pgid, signum)
        except ProcessLookupError:
            return SIGNAL_GONE
        except PermissionError:
            return SIGNAL_DENIED
        return SIGNAL_DELIVERED

    def reap(self, *, wait_seconds: float | None = None) -> bool:
        """Reap the child. Return True only when the wait confirmed exit.

        When wait_seconds is set, polling stays bounded and returns False if
        the child is still alive when the deadline expires.
        """
        if self.reaped:
            self._close_fds()
            return True
        deadline = None if wait_seconds is None else time.monotonic() + max(0.0, wait_seconds)
        while True:
            flags = 0 if deadline is None else os.WNOHANG
            try:
                waited, _status = os.waitpid(self.pid, flags)
            except ChildProcessError:
                self.reaped = True
                self._close_fds()
                return True
            if waited != 0:
                self.reaped = True
                self._close_fds()
                return True
            if deadline is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._close_fds()
                return False
            time.sleep(min(0.01, remaining))

    def _close_fds(self) -> None:
        for name in ("stdin_fd", "stdout_fd", "stderr_fd"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)


class StderrRing:
    def __init__(self, limit: int = STDERR_LIMIT) -> None:
        self.limit = limit
        self._buffer = bytearray()

    def extend(self, data: bytes) -> None:
        if not data:
            return
        self._buffer.extend(data)
        extra = len(self._buffer) - self.limit
        if extra > 0:
            del self._buffer[:extra]

    def __len__(self) -> int:
        return len(self._buffer)

    def __repr__(self) -> str:
        return f"StderrRing(limit={self.limit}, size={len(self._buffer)})"


def spawn_identity() -> str:
    if sys.platform == "darwin":
        return MACOS_PATH_LOOKUP
    if sys.platform.startswith("linux"):
        return (
            "Linux spawn uses execveat on the hashed descriptor or fails closed. "
            "The executable descriptor stays open until the syscall. Every other "
            "inherited descriptor is closed."
        )
    return "This platform has no approved MCP spawn path."


def execveat_syscall_number() -> int | None:
    if not sys.platform.startswith("linux"):
        return None
    return _EXECVEAT.get(os.uname().machine)


def environment_sha256(pairs: tuple[tuple[str, str], ...]) -> str:
    import json

    payload = json.dumps([list(pair) for pair in pairs], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def header_accepted(header: bytes, platform: str) -> bool:
    if header.startswith(b"#!"):
        return False
    if platform == "darwin":
        return header in _MACHO
    if platform.startswith("linux"):
        return header == b"\x7fELF"
    return False


def parent_entry_trusted(info: os.stat_result, owner: int) -> bool:
    """True when one path component cannot be replaced by an untrusted user."""
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    if info.st_mode & 0o022:
        return False
    return info.st_uid in {owner, 0}


def prepare_spawn(server: ServerConfig) -> SpawnPlan:
    argv = (server.executable, *server.args)
    if any("\x00" in part or "\n" in part for part in argv):
        raise ProcessError("executable")
    for key, value in server.env:
        if "\x00" in key or "\x00" in value or "=" in key:
            raise ProcessError("executable")
    method = "posix_spawn" if sys.platform == "darwin" else "execveat"
    if sys.platform.startswith("linux"):
        method = "execveat"
    elif sys.platform != "darwin":
        raise ProcessError("spawn_failed")
    return SpawnPlan(method, server.executable, argv, tuple(server.env), "/")


def open_executable(path: str, *, platform: str | None = None) -> tuple[ExecutableFacts, int]:
    if blocked_location(path):
        raise ProcessError("executable")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProcessError("executable") from exc
    try:
        return assess_descriptor(descriptor, path, platform=platform), descriptor
    except Exception:
        os.close(descriptor)
        raise


def assess_executable(path: str, *, platform: str | None = None) -> ExecutableFacts:
    facts, descriptor = open_executable(path, platform=platform)
    os.close(descriptor)
    return facts


def assess_descriptor(descriptor: int, path: str, *, platform: str | None = None) -> ExecutableFacts:
    platform = sys.platform if platform is None else platform
    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        raise ProcessError("executable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid not in {os.getuid(), 0}
        or info.st_mode & (stat.S_ISUID | stat.S_ISGID | 0o022)
        or not info.st_mode & stat.S_IXUSR
        or info.st_size > MAX_EXECUTABLE_BYTES
    ):
        raise ProcessError("executable")
    _trusted_parents(path)
    try:
        header = os.read(descriptor, 4)
    except OSError as exc:
        raise ProcessError("executable") from exc
    if not header_accepted(header, platform):
        raise ProcessError("executable")
    digest = hashlib.sha256(header)
    remaining = info.st_size - len(header)
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            raise ProcessError("executable")
        digest.update(chunk)
        remaining -= len(chunk)
    try:
        again = os.fstat(descriptor)
    except OSError as exc:
        raise ProcessError("executable") from exc
    if again.st_dev != info.st_dev or again.st_ino != info.st_ino or again.st_size != info.st_size:
        raise ProcessError("changed")
    return ExecutableFacts(info.st_dev, info.st_ino, digest.hexdigest(), path)


def revalidate(descriptor: int, facts: ExecutableFacts) -> None:
    """Confirm the opened descriptor and path still name the verified inode."""
    try:
        current = os.fstat(descriptor)
        outside = os.lstat(facts.path)
    except OSError as exc:
        raise ProcessError("changed") from exc
    if stat.S_ISLNK(outside.st_mode) or not stat.S_ISREG(outside.st_mode):
        raise ProcessError("changed")
    if (
        current.st_dev != facts.device
        or current.st_ino != facts.inode
        or outside.st_dev != facts.device
        or outside.st_ino != facts.inode
        or outside.st_uid not in {os.getuid(), 0}
        or outside.st_mode & (stat.S_ISUID | stat.S_ISGID | 0o022)
        or not outside.st_mode & stat.S_IXUSR
    ):
        raise ProcessError("changed")
    _trusted_parents(facts.path)


def start_process(
    server: ServerConfig,
    facts: ExecutableFacts,
    *,
    generation: int,
    descriptor: int,
    platform: str | None = None,
) -> ChildProcess:
    plan = prepare_spawn(server)
    platform = sys.platform if platform is None else platform
    revalidate(descriptor, facts)
    if platform.startswith("linux"):
        if execveat_syscall_number() is None or platform_override_blocks_execveat():
            raise ProcessError("execveat_unavailable")
        return _spawn_linux(plan, facts, generation, descriptor)
    if platform == "darwin":
        revalidate(descriptor, facts)
        return _spawn_darwin(plan, facts, generation, descriptor)
    raise ProcessError("spawn_failed")


def platform_override_blocks_execveat() -> bool:
    return False


def _trusted_parents(path: str) -> None:
    current = os.path.dirname(os.path.abspath(path))
    owner = os.getuid()
    for _ in range(64):
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise ProcessError("executable") from exc
        if not parent_entry_trusted(info, owner):
            raise ProcessError("executable")
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent
    raise ProcessError("executable")


def _spawn_linux(plan: SpawnPlan, facts: ExecutableFacts, generation: int, descriptor: int) -> ChildProcess:
    """Fork + execveat with a best-effort CLOEXEC error pipe.

    Protocol (CLOEXEC on both ends):
    - Child writes READY ('R') immediately before execveat.
    - On exec failure the child must write FAIL ('F') + 4-byte errno.
    - If FAIL cannot be written after READY, the child keeps the write end open
      and parks until the parent times out and kills it — it must not close the
      pipe in a success-shaped R+EOF pattern.
    - Successful exec closes the write end via CLOEXEC → parent sees R then EOF.

    Guarantees:
    - EOF with no READY ⇒ fail closed (never armed).
    - Explicit FAIL ⇒ fail closed with errno when present.
    - Parent timeout ⇒ fail closed; child is killed and reap is attempted.
    - A returned ChildProcess means the parent observed R+EOF (or would require
      a live CLOEXEC close). It is best-effort confirmation of exec.

    Residual race (accepted, not claimed as eliminated):
    - After READY is written and before execveat enters, a kill/crash closes the
      write end and looks like R+EOF success. The pipe protocol cannot distinguish
      that from successful exec without breaking immediate post-exec exits.
    """
    stdin_read, stdin_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    try:
        error_read, error_write = _error_pipe()
    except OSError as exc:
        _close_many([stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write])
        raise ProcessError("spawn_failed") from exc
    number = execveat_syscall_number()
    if number is None:
        _close_many(
            [stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write, error_read, error_write]
        )
        raise ProcessError("execveat_unavailable")
    argv = _cstring_array(plan.argv)
    envp = _cstring_array(tuple(f"{key}={value}" for key, value in plan.env))
    syscall = _bind_syscall()
    try:
        pid = os.fork()
    except OSError as exc:
        _close_many(
            [stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write, error_read, error_write]
        )
        raise ProcessError("spawn_failed") from exc
    if pid == 0:
        armed = False
        fail_reported = False
        try:
            os.close(error_read)
            os.setsid()
            os.chdir("/")
            os.dup2(stdin_read, 0)
            os.dup2(stdout_write, 1)
            os.dup2(stderr_write, 2)
            _close_inherited({0, 1, 2, descriptor, error_write})
            _write_all(error_write, _EXEC_READY)
            armed = True
            _execveat(syscall, number, descriptor, argv, envp)
            fail_reported = _write_fail_errno(error_write, ctypes_errno() or errno.EINVAL)
            if not fail_reported:
                _park_until_killed()
        except ProcessError:
            if armed:
                fail_reported = _write_fail_errno(error_write, errno.EIO)
                if not fail_reported:
                    _park_until_killed()
            else:
                _write_fail_errno(error_write, errno.EIO)
        except OSError as exc:
            if armed:
                fail_reported = _write_fail_errno(error_write, exc.errno or errno.EIO)
                if not fail_reported:
                    _park_until_killed()
            else:
                _write_fail_errno(error_write, exc.errno or errno.EIO)
        finally:
            if armed and not fail_reported:
                _park_until_killed()
            try:
                os.close(error_write)
            except OSError:
                pass
            os._exit(127)
    os.close(error_write)
    _close_many([stdin_read, stdout_write, stderr_write])
    try:
        outcome, err = _confirm_linux_exec(error_read, EXEC_PIPE_SECONDS)
    except Exception as exc:
        _kill_and_reap(pid)
        _close_many([stdin_write, stdout_read, stderr_read, error_read])
        if isinstance(exc, ProcessError):
            raise
        raise ProcessError("spawn_failed") from exc
    os.close(error_read)
    if outcome != "success":
        _kill_and_reap(pid)
        _close_many([stdin_write, stdout_read, stderr_read])
        raise ProcessError("spawn_failed", errno_value=err)
    return ChildProcess(pid, pid, stdin_write, stdout_read, stderr_read, facts, generation)


def _error_pipe() -> tuple[int, int]:
    import fcntl

    read_end, write_end = os.pipe()
    try:
        flags = fcntl.fcntl(read_end, fcntl.F_GETFD)
        fcntl.fcntl(read_end, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
        flags = fcntl.fcntl(write_end, fcntl.F_GETFD)
        fcntl.fcntl(write_end, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
    except OSError:
        _close_many([read_end, write_end])
        raise
    return read_end, write_end


def _errno_bytes(value: int) -> bytes:
    return int(value).to_bytes(4, "big", signed=False)


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write every byte or raise. Partial writes are retried; EINTR is retried."""
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(descriptor, payload[offset:])
        except InterruptedError:
            continue
        if written == 0:
            raise OSError(errno.EIO, "error pipe write returned zero")
        offset += written


def _write_fail_errno(descriptor: int, value: int) -> bool:
    """Write FAIL+errno. Return True only when the full report was written."""
    try:
        _write_all(descriptor, _EXEC_FAIL + _errno_bytes(value))
    except OSError:
        return False
    return True


def _write_exec_errno(descriptor: int, value: int) -> bool:
    """Compatibility wrapper used by tests; prefer _write_fail_errno in new code."""
    return _write_fail_errno(descriptor, value)


def _park_until_killed() -> None:
    """Keep the error-pipe write end open until the parent times out and kills us.

    Used only after READY when the FAIL report cannot be written. Exiting or
    closing the pipe would look like successful R+EOF to the parent.
    """
    while True:
        try:
            time.sleep(3600)
        except Exception:
            continue


def _confirm_linux_exec(descriptor: int, timeout: float) -> tuple[str, int | None]:
    """Return ('success', None) or ('failure', errno_or_None)."""
    payload = _read_exec_report(descriptor, timeout)
    if not payload:
        # Child closed the write end without READY: died or aborted before exec.
        return "failure", None
    if payload.startswith(_EXEC_FAIL):
        if len(payload) < 5:
            raise OSError(errno.EIO, "incomplete exec failure report")
        return "failure", int.from_bytes(payload[1:5], "big", signed=False)
    if payload.startswith(_EXEC_READY):
        rest = payload[1:]
        if not rest:
            # Best-effort success: CLOEXEC after exec, or residual READY→exec kill.
            return "success", None
        if rest.startswith(_EXEC_FAIL):
            if len(rest) < 5:
                raise OSError(errno.EIO, "incomplete exec failure report")
            return "failure", int.from_bytes(rest[1:5], "big", signed=False)
        raise OSError(errno.EIO, "unexpected exec report after ready")
    raise OSError(errno.EIO, "unexpected exec report")


def _read_exec_report(descriptor: int, timeout: float) -> bytes:
    """Bounded read of the exec report. Returns bytes collected through EOF."""
    import select

    deadline = time.monotonic() + max(0.0, timeout)
    payload = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("exec error pipe timed out")
        try:
            readable, _w, _x = select.select([descriptor], [], [], remaining)
        except InterruptedError:
            continue
        if not readable:
            raise TimeoutError("exec error pipe timed out")
        try:
            chunk = os.read(descriptor, 64)
        except InterruptedError:
            continue
        if not chunk:
            return bytes(payload)
        payload.extend(chunk)
        if payload.startswith(_EXEC_FAIL) and len(payload) >= 5:
            return bytes(payload[:5])
        if payload.startswith(_EXEC_READY) and len(payload) >= 6 and payload[1:2] == _EXEC_FAIL:
            return bytes(payload[:6])


def _read_exec_errno(descriptor: int) -> int | None:
    """Test helper: read a legacy 4-byte errno payload or None on EOF."""
    payload = b""
    while len(payload) < 4:
        chunk = os.read(descriptor, 4 - len(payload))
        if not chunk:
            break
        payload += chunk
    if not payload:
        return None
    if len(payload) != 4:
        raise OSError(errno.EIO, "incomplete exec error report")
    return int.from_bytes(payload, "big", signed=False)


def ctypes_errno() -> int:
    import ctypes

    return int(ctypes.get_errno())


def _kill_and_reap(pid: int) -> bool:
    """SIGKILL the child and attempt a bounded reap. Return True only if reaped."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass
    return _reap_failed_child(pid)


def _reap_failed_child(pid: int, *, wait_seconds: float = REAP_FAILED_SECONDS) -> bool:
    """Return True only when waitpid confirmed the child exited."""
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if waited != 0:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def _bind_syscall():
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    return syscall


def _execveat(syscall, number: int, descriptor: int, argv: object, envp: object) -> None:
    import ctypes

    ctypes.set_errno(0)
    result = syscall(
        ctypes.c_long(number),
        ctypes.c_int(descriptor),
        ctypes.c_char_p(b""),
        argv,
        envp,
        ctypes.c_int(_AT_EMPTY_PATH),
    )
    if result != -1:
        return
    err = ctypes.get_errno() or errno.EINVAL
    raise OSError(err, os.strerror(err))


def _spawn_darwin(plan: SpawnPlan, facts: ExecutableFacts, generation: int, descriptor: int) -> ChildProcess:
    stdin_read, stdin_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    try:
        revalidate(descriptor, facts)
        pid = _posix_spawn(plan, stdin_read, stdout_write, stderr_write)
    except Exception:
        _close_many([stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write])
        raise
    _close_many([stdin_read, stdout_write, stderr_write])
    return ChildProcess(pid, pid, stdin_write, stdout_read, stderr_read, facts, generation)


def _posix_spawn(plan: SpawnPlan, stdin_read: int, stdout_write: int, stderr_write: int) -> int:
    import ctypes

    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    actions = ctypes.c_void_p()
    attributes = ctypes.c_void_p()
    if libc.posix_spawn_file_actions_init(ctypes.byref(actions)) != 0:
        raise ProcessError("spawn_failed")
    if libc.posix_spawn_file_actions_addchdir_np(ctypes.byref(actions), ctypes.c_char_p(b"/")) != 0:
        libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        raise ProcessError("spawn_failed")
    for source, target in ((stdin_read, 0), (stdout_write, 1), (stderr_write, 2)):
        if libc.posix_spawn_file_actions_adddup2(ctypes.byref(actions), ctypes.c_int(source), ctypes.c_int(target)) != 0:
            libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
            raise ProcessError("spawn_failed")
    for descriptor in _extra_fds(stdin_read, stdout_write, stderr_write):
        if libc.posix_spawn_file_actions_addclose(ctypes.byref(actions), ctypes.c_int(descriptor)) != 0:
            libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
            raise ProcessError("spawn_failed")
    if libc.posix_spawnattr_init(ctypes.byref(attributes)) != 0:
        libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        raise ProcessError("spawn_failed")
    if libc.posix_spawnattr_setflags(ctypes.byref(attributes), ctypes.c_short(_POSIX_SPAWN_SETSID)) != 0:
        libc.posix_spawnattr_destroy(ctypes.byref(attributes))
        libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        raise ProcessError("spawn_failed")
    argv = _cstring_array(plan.argv)
    envp = _cstring_array(tuple(f"{key}={value}" for key, value in plan.env))
    pid = ctypes.c_int()
    libc.posix_spawn.restype = ctypes.c_int
    result = libc.posix_spawn(
        ctypes.byref(pid),
        ctypes.c_char_p(plan.path.encode()),
        ctypes.byref(actions),
        ctypes.byref(attributes),
        argv,
        envp,
    )
    libc.posix_spawnattr_destroy(ctypes.byref(attributes))
    libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
    if result != 0:
        raise ProcessError("spawn_failed")
    return int(pid.value)


def _cstring_array(values: tuple[str, ...]):
    import ctypes

    buffers = [item.encode() for item in values]
    array = (ctypes.c_char_p * (len(buffers) + 1))()
    for index, buffer in enumerate(buffers):
        array[index] = buffer
    array[len(buffers)] = None
    array._keepalive = buffers  # type: ignore[attr-defined]
    return array


def _extra_fds(*kept: int) -> list[int]:
    return [number for number in _list_fds() if number > 2 and number not in kept]


def _descriptor_is_open(descriptor: int) -> bool:
    try:
        import fcntl

        fcntl.fcntl(descriptor, fcntl.F_GETFD)
        return True
    except OSError:
        return False


def _list_fds() -> list[int]:
    """Return open descriptor numbers without including a closed listing FD.

    Opening /dev/fd or /proc/self/fd creates a directory descriptor that appears
    in the listing. That descriptor is closed when listing finishes. Recording
    it for a later posix_spawn close action makes the spawn fail with EBADF.
    macOS can also expose stale /dev/fd names; only keep descriptors that are
    still open.

    Fail closed when neither directory can be enumerated. An empty result after
    a successful listing is allowed.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    last_error: OSError | None = None
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            dir_fd = os.open(directory, flags)
        except OSError as exc:
            last_error = exc
            continue
        found: list[int] = []
        try:
            try:
                names = os.listdir(dir_fd)
            except OSError as exc:
                last_error = exc
                continue
            for name in names:
                try:
                    number = int(name)
                except ValueError:
                    continue
                if number == dir_fd or not _descriptor_is_open(number):
                    continue
                found.append(number)
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass
        return found
    raise ProcessError("spawn_failed") from last_error


def _close_inherited(keep: set[int]) -> None:
    """Close every open descriptor except the ones the child must keep.

    The executable descriptor must remain in keep until execveat returns.
    Descriptors above 256 are included. Enumeration failure aborts spawn.
    EBADF means the descriptor is already gone. Any other close failure, or an
    interrupted close that leaves the descriptor open, aborts spawn.
    """
    for descriptor in _list_fds():
        if descriptor in keep:
            continue
        try:
            os.close(descriptor)
        except InterruptedError:
            if _descriptor_is_open(descriptor):
                raise ProcessError("spawn_failed")
            continue
        except OSError as exc:
            if exc.errno == errno.EBADF:
                continue
            raise ProcessError("spawn_failed") from exc


def _close_many(descriptors: list[int]) -> None:
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            continue
