"""Run one registered command after a fresh permission check.

The working directory is a directory file descriptor, not a path supplied by
the model. File contents are read through the descriptor that was opened with
O_NOFOLLOW, so a symlink swapped in later is not what the process reads.
"""

import errno
import os
import stat
from pathlib import Path

from app.approvals.service import execution_matches
from app.commands.audit import begin_command_audit, finish_command_audit
from app.commands.registry import CommandSpec, trusted_executable
from app.commands.runner import supervise
from app.files.access import ApprovedRoot, _open_root, _walk_directories, relative_parts
from app.files.changes import _protected_workspace, _reject_sensitive
from app.files.exceptions import FileAccessError
from app.files.operations import _load_root, _pause_for_tests
from app.permissions.service import evaluate_request
from app.tools.exceptions import ToolExecutionError
from app.tools.registry import get_tool_registry

_TEXT = frozenset({".txt", ".md", ".markdown", ".json", ".csv", ".py"})
_DENIED = "That command is not allowed."
_BLOCKED_PREFIXES = ("/bin", "/sbin", "/usr", "/System", "/dev", "/etc", "/private/etc", "/Library")


def execute_command(spec: CommandSpec, arguments: dict) -> dict:
    relative = arguments.get("relative_path") if spec.kind == "file" else None
    if spec.kind == "file" and not isinstance(relative, str):
        raise ToolExecutionError("That file was not found.")
    root = _load_root(arguments.get("workspace_id"))
    _assert_workspace(root)
    _authorize(spec, arguments, relative if isinstance(relative, str) else None)
    _pause_for_tests()
    _authorize(spec, arguments, relative if isinstance(relative, str) else None)
    executable = trusted_executable(spec.executable_name)
    root_fd = _open_root(root)
    file_fd = None
    result = None
    try:
        if spec.kind == "file":
            file_fd = _open_target(root_fd, str(relative), text_only=spec.text_only)
            argv = _file_argv(spec, executable)
        else:
            argv = _directory_argv(spec, executable)
        execution_id = begin_command_audit(spec, arguments)
        result = supervise(
            argv,
            directory_fd=root_fd,
            timeout=spec.timeout_seconds,
            max_output=spec.max_output_bytes,
            cpu_seconds=spec.cpu_seconds,
            max_file_bytes=spec.max_file_bytes,
            file_fd=file_fd,
        )
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(root_fd)
    if result is None:
        raise ToolExecutionError("The command could not be started.")
    from app.commands.faults import controller

    if controller.crash_stage == "before_finish":
        os._exit(86)
    status = "timed_out" if result["timed_out"] else "completed" if result["exit_code"] == 0 else "failed"
    finish_command_audit(spec, arguments, execution_id, status, result["exit_code"])
    return _public_result(spec, result)


def _file_argv(spec: CommandSpec, executable: str) -> list[str]:
    if spec.executable_name == "wc":
        return [executable, "-c", "/dev/fd/3"]
    if spec.executable_name == "cat":
        return [executable, "/dev/fd/3"]
    raise ToolExecutionError("That command is not available.")


def _directory_argv(spec: CommandSpec, executable: str) -> list[str]:
    if spec.executable_name == "ls":
        return [executable, "-1"]
    if spec.executable_name == "sleep":
        return [executable, "5"]
    raise ToolExecutionError("That command is not available.")


def _authorize(spec: CommandSpec, arguments: dict, relative: str | None) -> None:
    decision = evaluate_request(
        agent_id=_actor("agent"),
        tool_name=spec.command_id,
        action="execute",
        resource_type="file" if relative else "workspace",
        resource_id=arguments.get("workspace_id") if isinstance(arguments.get("workspace_id"), str) else None,
        relative_path=relative,
        registry=get_tool_registry(),
    )
    if decision.decision == "DENY":
        _audit(spec, arguments, "command_denied", "DENY", decision.reason, None)
        raise ToolExecutionError(decision.reason or _DENIED)
    if decision.decision == "REQUIRES_APPROVAL":
        from app.files.authorization import _APPROVAL

        approval_id = _APPROVAL.get() if isinstance(_APPROVAL.get(), str) else None
        if not execution_matches(
            approval_id,
            agent_id=_actor("agent"),
            tool_name=spec.command_id,
            action="execute",
            arguments=arguments,
        ):
            _audit(spec, arguments, "command_denied", "DENY", "approval", None)
            raise ToolExecutionError("This action needs confirmation before it can run.")


def _actor(kind: str) -> str | None:
    from app.files.authorization import _AGENT

    if kind == "agent":
        value = _AGENT.get()
        return value if isinstance(value, str) else None
    return None


def _assert_workspace(root: ApprovedRoot) -> None:
    if _protected_workspace(root):
        raise ToolExecutionError("That workspace is not available for commands.")
    text = str(Path(root.path).resolve())
    if text == "/" or any(text == prefix or text.startswith(prefix + "/") for prefix in _BLOCKED_PREFIXES):
        raise ToolExecutionError("That workspace is not available for commands.")


def _open_target(root_fd: int, relative: str, *, text_only: bool) -> int:
    parts = relative_parts(relative)
    if not parts:
        raise FileAccessError("That file was not found.")
    _reject_sensitive(parts)
    if any(part.startswith("-") or any(character in part for character in ";|&$`<>\n\r") for part in parts):
        raise FileAccessError("That path is not allowed.")
    if text_only and Path(parts[-1]).suffix.lower() not in _TEXT:
        raise FileAccessError("That file type cannot be previewed.")
    parent = root_fd if len(parts) == 1 else _walk_directories(root_fd, parts[:-1])
    try:
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise FileAccessError("Symlinks are not followed.") from exc
        raise FileAccessError("That file was not found.") from exc
    finally:
        if parent != root_fd:
            os.close(parent)
    info = os.fstat(descriptor)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise FileAccessError("That path is not a file.")
    return descriptor


def _public_result(spec: CommandSpec, result: dict) -> dict:
    stdout = result["stdout"]
    stderr = result["stderr"]
    body: dict = {
        "command_id": spec.command_id,
        "status": "timed_out" if result["timed_out"] else "completed" if result["exit_code"] == 0 else "failed",
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "truncated": result["truncated"],
        "data_boundary": "untrusted_command_output",
    }
    if spec.executable_name == "wc":
        body["bytes"] = _byte_count(stdout)
        return body
    if spec.executable_name == "sleep":
        return body
    text = stdout.decode("utf-8", errors="replace")
    body["stdout"] = text
    if stderr:
        body["stderr"] = stderr.decode("utf-8", errors="replace")[:500]
    return body


def _byte_count(payload: bytes) -> int | None:
    text = payload.decode("utf-8", errors="replace").strip().split()
    if not text:
        return None
    try:
        return int(text[0])
    except ValueError:
        return None


def _audit(spec: CommandSpec, arguments: dict, event: str, decision: str, detail: str, exit_code: int | None) -> None:
    from app.core.database import get_session_factory
    from app.files.authorization import _AGENT, _APPROVAL
    from app.permissions.audit import add_audit

    agent_id = _AGENT.get() if isinstance(_AGENT.get(), str) else None
    approval_id = _APPROVAL.get() if isinstance(_APPROVAL.get(), str) else None
    exit_text = "" if exit_code is None else f" exit={exit_code}"
    session = get_session_factory()()
    try:
        add_audit(
            session,
            event=event,
            decision=decision[:32],
            detail=f"{spec.command_id} {detail}{exit_text}"[:180],
            agent_id=agent_id,
            tool_name=spec.command_id,
            resource_type="workspace",
            resource_id=approval_id or (arguments.get("workspace_id") if isinstance(arguments.get("workspace_id"), str) else None),
            action="execute",
        )
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()
