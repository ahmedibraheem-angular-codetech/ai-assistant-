"""Commands the assistant is allowed to run.

The identifier is chosen in code. An executable path, a shell string, or an
argument list from a model is never accepted. A command that is not in this
registry cannot run.
"""

import os
import stat
from dataclasses import dataclass

from app.tools.base import BLOCKING_READ_ONLY, SIDE_EFFECT
from app.tools.exceptions import ToolExecutionError

_TRUSTED = {
    "ls": "/bin/ls",
    "wc": "/usr/bin/wc",
    "cat": "/bin/cat",
    "sleep": "/bin/sleep",
}


@dataclass(frozen=True, slots=True)
class CommandSpec:
    command_id: str
    executable_name: str
    kind: str
    risk: str
    approval_required: bool
    execution_policy: str
    timeout_seconds: float
    max_output_bytes: int
    cpu_seconds: int
    max_file_bytes: int
    text_only: bool


def builtin_specs() -> tuple[CommandSpec, ...]:
    specs = (
        CommandSpec(
            command_id="show_directory",
            executable_name="ls",
            kind="directory",
            risk="read",
            approval_required=True,
            execution_policy=BLOCKING_READ_ONLY,
            timeout_seconds=5,
            max_output_bytes=8_192,
            cpu_seconds=5,
            max_file_bytes=0,
            text_only=False,
        ),
        CommandSpec(
            command_id="measure_file",
            executable_name="wc",
            kind="file",
            risk="read",
            approval_required=True,
            execution_policy=BLOCKING_READ_ONLY,
            timeout_seconds=5,
            max_output_bytes=256,
            cpu_seconds=5,
            max_file_bytes=0,
            text_only=False,
        ),
        CommandSpec(
            command_id="preview_text",
            executable_name="cat",
            kind="file",
            risk="sensitive",
            approval_required=True,
            execution_policy=SIDE_EFFECT,
            timeout_seconds=5,
            max_output_bytes=4_096,
            cpu_seconds=5,
            max_file_bytes=0,
            text_only=True,
        ),
    )
    return specs


def test_wait_spec() -> CommandSpec:
    """Sleep command used by tests. Production startup does not register it."""
    return CommandSpec(
        command_id="bounded_wait",
        executable_name="sleep",
        kind="directory",
        risk="read",
        approval_required=True,
        execution_policy=BLOCKING_READ_ONLY,
        timeout_seconds=0.4,
        max_output_bytes=64,
        cpu_seconds=2,
        max_file_bytes=0,
        text_only=False,
    )


def spec_for(command_id: str) -> CommandSpec | None:
    for spec in builtin_specs():
        if spec.command_id == command_id:
            return spec
    return None


def command_ids() -> frozenset[str]:
    return frozenset(spec.command_id for spec in builtin_specs())


def trusted_executable(name: str) -> str:
    """Return one fixed binary. Symlinks and unknown names are refused."""
    path = _TRUSTED.get(name)
    if path is None:
        raise ToolExecutionError("That command is not available.")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ToolExecutionError("That command is not available.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ToolExecutionError("That command is not available.")
    return path
