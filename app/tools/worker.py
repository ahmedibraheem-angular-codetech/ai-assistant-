"""Run one allowlisted blocking tool in this process.

The server starts this module with ``sys.executable -m app.tools.worker``.
The payload is JSON on stdin. The result is one JSON object on stdout.
The payload cannot select a module, a shell command, or Python source.
``blocking_probe`` is not a registered tool. Tests use it to observe whether
a killed process still performs its post-delay write.
"""

import asyncio
import inspect
import json
import sys
import tempfile
import time
from pathlib import Path

from app.core.config import get_settings
from app.tools.base import BLOCKING_READ_ONLY, SIDE_EFFECT, Tool
from app.tools.exceptions import ToolExecutionError, ToolValidationError

ALLOWED_TOOLS = frozenset(
    {
        "calculator",
        "datetime",
        "blocking_probe",
        "list_files",
        "search_files",
        "read_file",
        "get_file_metadata",
        "inspect_project",
        "create_text_file",
        "update_text_file",
        "quarantine_file",
        "show_directory",
        "measure_file",
        "preview_text",
        "bounded_wait",
    }
)
_MUTATIONS = frozenset({"create_text_file", "update_text_file", "quarantine_file"})
_APPROVAL_BOUND = _MUTATIONS | {"preview_text"}
_MAX_PAYLOAD = 65_536


def main() -> int:
    raw = sys.stdin.buffer.read(_MAX_PAYLOAD + 1)
    if len(raw) > _MAX_PAYLOAD:
        return _fail()
    try:
        payload = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _fail()
    if not isinstance(payload, dict):
        return _fail()
    from app.commands.faults import controller

    controller.apply_payload(payload)
    name = payload.get("tool")
    arguments = payload.get("arguments")
    effect = payload.get("effect")
    confirmed = payload.get("confirmed") is True
    agent_id = payload.get("agent_id")
    approval_id = payload.get("approval_id")
    injected = payload.get("injected_command")
    if not isinstance(name, str) or name not in ALLOWED_TOOLS or not isinstance(arguments, dict):
        return _fail()
    if agent_id is not None and not isinstance(agent_id, str):
        return _fail()
    if approval_id is not None and not isinstance(approval_id, str):
        return _fail()
    if injected is not None and not isinstance(injected, str):
        return _fail()
    if name == "bounded_wait" and injected != "bounded_wait":
        return _fail()
    if injected not in {None, "bounded_wait"}:
        return _fail()
    if effect not in {BLOCKING_READ_ONLY, SIDE_EFFECT}:
        return _fail()
    if effect == SIDE_EFFECT and not confirmed:
        return _fail()
    if name in _APPROVAL_BOUND and (effect != SIDE_EFFECT or not isinstance(approval_id, str) or not approval_id):
        return _fail()
    _limit_cpu(payload.get("cpu_seconds"))
    try:
        output = _dispatch(
            name,
            arguments,
            agent_id,
            approval_id if isinstance(approval_id, str) else None,
            injected if isinstance(injected, str) else None,
        )
    except ToolValidationError as exc:
        if not _emit({"ok": False, "kind": "validation", "error": str(exc)}):
            return _fail()
        return 2
    except ToolExecutionError as exc:
        if not _emit({"ok": False, "kind": "execution", "error": str(exc)}):
            return _fail()
        return 3
    except Exception:
        return _fail()
    if not isinstance(output, dict):
        return _fail()
    if not _emit({"ok": True, "output": output}):
        return _fail()
    return 0


def _dispatch(
    name: str,
    arguments: dict,
    agent_id: str | None,
    approval_id: str | None = None,
    injected: str | None = None,
) -> dict:
    from app.files.authorization import bind_file_actor, reset_file_actor

    tokens = bind_file_actor(agent_id, name, approval_id)
    try:
        return _dispatch_bound(name, arguments, injected)
    finally:
        reset_file_actor(tokens)


def _dispatch_bound(name: str, arguments: dict, injected: str | None = None) -> dict:
    if name == "calculator":
        from app.tools.calculator import CalculatorTool

        return _run(CalculatorTool(), arguments)
    if name == "datetime":
        from app.tools.datetime_tool import DateTimeTool

        return _run(DateTimeTool(), arguments)
    if name == "blocking_probe":
        return _blocking_probe(arguments)
    if name in {
        "list_files",
        "search_files",
        "read_file",
        "get_file_metadata",
        "inspect_project",
        "create_text_file",
        "update_text_file",
        "quarantine_file",
    }:
        from app.files.tools import tool_for

        return _run(tool_for(name), arguments)
    if name in {"show_directory", "measure_file", "preview_text"}:
        from app.commands.tools import tool_for as command_for

        return _run(command_for(name), arguments)
    if name == "bounded_wait" and injected == "bounded_wait":
        from app.commands.tools import wait_tool
        from app.tools.registry import get_tool_registry

        registry = get_tool_registry()
        if registry.get(name) is None:
            registry.register(wait_tool())
        return _run(wait_tool(), arguments)
    raise ToolExecutionError()


def _run(tool: Tool, arguments: dict) -> dict:
    result = tool.run(arguments)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    if not isinstance(result, dict):
        raise ToolExecutionError()
    return result


def _blocking_probe(arguments: dict) -> dict:
    marker = _temp_file(arguments.get("marker"))
    delay = arguments.get("delay")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0 or delay > 2:
        raise ToolValidationError()
    started = arguments.get("started")
    if started is not None:
        _temp_file(started).write_text("started", encoding="utf-8")
    time.sleep(float(delay))
    marker.write_text("done", encoding="utf-8")
    return {"wrote": True}


def _temp_file(value: object) -> Path:
    if not isinstance(value, str) or not value or len(value) > 400:
        raise ToolValidationError()
    path = Path(value)
    if not path.is_absolute():
        raise ToolValidationError()
    root = Path(tempfile.gettempdir()).resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ToolValidationError()
    return resolved


def _limit_cpu(value: object) -> None:
    """Bound CPU time if the parent can no longer kill this process."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 60:
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (value, value))
    except (OSError, ValueError):
        return


def _emit(payload: dict) -> bool:
    """Serialize one JSON result. Refuse any payload that exceeds the stdout byte cap."""
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    limit = get_settings().tool_worker_stdout_max_bytes
    if len(raw) > limit:
        return False
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()
    return True


def _fail() -> int:
    if not _emit({"ok": False, "kind": "failed"}):
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
