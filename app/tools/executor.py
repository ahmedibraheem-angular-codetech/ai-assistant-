"""Run one validated tool under an explicit execution policy.

Async read-only tools stay on the event loop. ``asyncio.wait_for`` cancels
that task at an await. It does not stop a blocking call, and cancelling the
task does not terminate a thread.

Blocking read-only tools and confirmed side effects run in a child process
started with ``sys.executable -m app.tools.worker``. On timeout or
cancellation the parent sends SIGKILL to that process group and waits until
the process has exited before returning. Killing the process stops further
work in that process. It does not undo a side effect the process already
performed. This is not a thread timeout.

A side effect that is not confirmed never starts. A side effect that cannot
be placed in a killable process fails closed and does not start either.
There is no shell and no arbitrary Python execution.
"""

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from pathlib import Path

from app.core.config import get_settings
from app.tools.base import ASYNC_READ_ONLY, BLOCKING_READ_ONLY, SIDE_EFFECT, Tool
from app.tools.exceptions import ToolExecutionError, ToolValidationError
from app.tools.models import ToolOutcome
from app.tools.runtime import acquire_worker_slot, attach_worker, release_worker_slot
from app.tools.worker import ALLOWED_TOOLS

logger = logging.getLogger(__name__)

_TIMEOUT = "The tool took too long and was stopped."
_CONFIRMATION = "This tool needs confirmation before it can run."
_FAILED = "The tool could not be completed."
_OVERFLOW = "The tool produced too much output and was stopped."
_UNISOLATED = "This tool cannot be isolated, so it was not run."
_BUSY = "Too many tools are running. Wait and try again."
_ROOT = Path(__file__).resolve().parents[2]
_STREAM_CHUNK = 8_192
# Exact allowlist only — no prefix matching (avoids FILE_SECRET / TOOL_WORKER_API_KEY).
# DATABASE_URL is intentional: the worker resolves workspaces through SQLAlchemy.
# Pytest overrides this to a temp DB; production may also set it instead of .env.
# Settings can still load other non-listed values from the project .env via cwd.
_WORKER_ENV_KEYS = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "TZ",
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "DATABASE_URL",
        "DEFAULT_TIMEZONE",
        "FILE_MAX_BYTES",
        "FILE_MAX_OUTPUT_CHARS",
        "FILE_LIST_LIMIT",
        "FILE_SEARCH_LIMIT",
        "FILE_SEARCH_SCAN_LIMIT",
        "FILE_PROJECT_DEPTH",
        "FILE_SPREADSHEET_MAX_ROWS",
        "FILE_SPREADSHEET_MAX_COLUMNS",
        "FILE_JSON_MAX_DEPTH",
        "FILE_MAX_MEMORY_BYTES",
        "FILE_MAX_CONCURRENT_WORKERS",
        "FILE_WORKER_CPU_SECONDS",
        "FILE_SEARCH_SNIPPET_CHARS",
        "FILE_ZIP_MAX_UNCOMPRESSED_BYTES",
        "FILE_ZIP_MAX_MEMBER_BYTES",
        "FILE_ZIP_MAX_FILES",
        "FILE_ZIP_MAX_RATIO",
        "FILE_ZIP_MAX_DEPTH",
        "FILE_ZIP_MAX_SECONDS",
        "FILE_WRITE_MAX_BYTES",
        "FILE_DELETE_BATCH_LIMIT",
        "FILE_BACKUP_MAX_BYTES",
        "FILE_BACKUP_MAX_COUNT",
        "TOOL_WORKER_STDOUT_MAX_BYTES",
        "TOOL_WORKER_STDERR_MAX_BYTES",
        "PYTEST_CURRENT_TEST",
        "ASSISTANT_FILE_TEST_DELAY_SECONDS",
        "ASSISTANT_FILE_TEST_STARTED_FILE",
        # Test-only crash injection; journal.crash_point also requires PYTEST_CURRENT_TEST.
        "ASSISTANT_FILE_JOURNAL_CRASH",
    }
)


class _StreamOverflow(Exception):
    """A worker pipe exceeded its configured byte limit."""


class WorkerRecord:
    """A worker process that has been waited on."""

    def __init__(self, pid: int, returncode: int | None) -> None:
        self.pid = pid
        self.returncode = returncode


class ToolExecutor:
    """Run one validated tool. Unconfirmed side effects are not executed."""

    def __init__(self) -> None:
        self.reaped_workers: list[WorkerRecord] = []

    async def run(
        self,
        tool: Tool,
        arguments: dict,
        *,
        confirmed: bool,
        agent_id: str | None = None,
        approval_id: str | None = None,
    ) -> ToolOutcome:
        policy = getattr(tool, "execution_policy", ASYNC_READ_ONLY)
        timeout = _timeout_seconds(tool)
        side_effect = tool.requires_confirmation or policy == SIDE_EFFECT
        if tool.name in {
            "create_text_file",
            "update_text_file",
            "quarantine_file",
            "preview_text",
        }:
            if not confirmed or not isinstance(approval_id, str) or not approval_id:
                return ToolOutcome(tool.name, "confirmation_required", None, _CONFIRMATION)
        if side_effect and not confirmed:
            return ToolOutcome(tool.name, "confirmation_required", None, _CONFIRMATION)
        # Kill switch and daily budget: checked and counted only when a tool
        # will actually start. Each run that gets this far is logged in
        # tool_runs (app/tools/runs.py), in the request's session when bound.
        from app.core.controls import consume_tool_call
        from app.tools.runs import REFUSED, record_run

        started = time.monotonic()
        refused = consume_tool_call(agent_id)
        if refused is not None:
            record_run(tool.name, agent_id=agent_id, status=REFUSED, started=started, approval_id=approval_id, error=refused)
            return ToolOutcome(tool.name, "error", None, refused)
        try:
            outcome = await self._start(tool, arguments, timeout, policy, side_effect, agent_id, approval_id)
        except BaseException as exc:
            record_run(tool.name, agent_id=agent_id, status="error", started=started, approval_id=approval_id,
                       error=type(exc).__name__)
            raise
        record_run(tool.name, agent_id=agent_id, status=outcome.status, started=started, approval_id=approval_id,
                   error=outcome.error)
        return outcome

    async def _start(
        self,
        tool: Tool,
        arguments: dict,
        timeout: float,
        policy: str,
        side_effect: bool,
        agent_id: str | None,
        approval_id: str | None,
    ) -> ToolOutcome:
        if side_effect:
            if policy != SIDE_EFFECT or not _can_isolate(tool):
                return ToolOutcome(tool.name, "error", None, _UNISOLATED)
            return await self._run_isolated(
                tool,
                arguments,
                timeout,
                effect=SIDE_EFFECT,
                agent_id=agent_id,
                approval_id=approval_id,
            )
        if policy == BLOCKING_READ_ONLY:
            if not _can_isolate(tool):
                return ToolOutcome(tool.name, "error", None, _UNISOLATED)
            return await self._run_isolated(
                tool,
                arguments,
                timeout,
                effect=BLOCKING_READ_ONLY,
                agent_id=agent_id,
                approval_id=approval_id,
            )
        if policy != ASYNC_READ_ONLY:
            return ToolOutcome(tool.name, "error", None, _UNISOLATED)
        return await self._run_async(
            tool,
            arguments,
            timeout,
            agent_id=agent_id,
            approval_id=approval_id,
        )

    async def _run_async(
        self,
        tool: Tool,
        arguments: dict,
        timeout: float,
        *,
        agent_id: str | None,
        approval_id: str | None = None,
    ) -> ToolOutcome:
        from app.files.authorization import bind_file_actor, reset_file_actor

        tokens = bind_file_actor(agent_id, tool.name, approval_id)
        try:
            try:
                output = await asyncio.wait_for(tool.run(arguments), timeout)
            except TimeoutError:
                return ToolOutcome(tool.name, "timeout", None, _TIMEOUT)
            except ToolValidationError:
                raise
            except ToolExecutionError as exc:
                return ToolOutcome(tool.name, "error", None, str(exc))
            except Exception:
                logger.exception("Tool %s failed", tool.name)
                return ToolOutcome(tool.name, "error", None, _FAILED)
            if not isinstance(output, dict):
                return ToolOutcome(tool.name, "error", None, _FAILED)
            return ToolOutcome(tool.name, "ok", output, None)
        finally:
            reset_file_actor(tokens)

    async def _run_isolated(
        self,
        tool: Tool,
        arguments: dict,
        timeout: float,
        *,
        effect: str,
        agent_id: str | None,
        approval_id: str | None = None,
    ) -> ToolOutcome:
        workspace_id = arguments.get("workspace_id") if isinstance(arguments, dict) else None
        slot = acquire_worker_slot(
            tool.name,
            agent_id=agent_id,
            workspace_id=workspace_id if isinstance(workspace_id, str) else None,
        )
        if slot is None:
            return ToolOutcome(tool.name, "error", None, _BUSY)
        body = {
            "arguments": arguments,
            "confirmed": effect == SIDE_EFFECT,
            "effect": effect,
            "tool": tool.isolation_key,
            "agent_id": agent_id,
            "approval_id": approval_id,
            "cpu_seconds": min(get_settings().file_worker_cpu_seconds, max(1, math.ceil(timeout))),
        }
        if getattr(tool, "test_only", False) is True:
            body["injected_command"] = tool.isolation_key
        if tool.name in {"show_directory", "measure_file", "preview_text", "bounded_wait"}:
            from app.commands.faults import controller

            body.update(controller.payload())
        payload = json.dumps(body).encode()
        settings = get_settings()
        stdout_max = settings.tool_worker_stdout_max_bytes
        stderr_max = settings.tool_worker_stderr_max_bytes
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.tools.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(_ROOT),
                env=worker_environment(),
                start_new_session=True,
            )
        except OSError:
            logger.exception("Tool %s could not be isolated", tool.name)
            release_worker_slot(slot)
            return ToolOutcome(tool.name, "error", None, _FAILED)
        attach_worker(
            slot,
            proc.pid,
            agent_id=agent_id,
            workspace_id=workspace_id if isinstance(workspace_id, str) else None,
        )
        try:
            try:
                stdout = await asyncio.wait_for(
                    _bounded_communicate(proc, payload, stdout_max, stderr_max),
                    timeout,
                )
            except _StreamOverflow:
                await self._reap(proc, kill=True)
                _reconcile_commands(tool.name)
                return ToolOutcome(tool.name, "error", None, _OVERFLOW)
            except TimeoutError:
                await self._reap(proc, kill=True)
                _reconcile_commands(tool.name)
                return ToolOutcome(tool.name, "timeout", None, _TIMEOUT)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                try:
                    await self._reap(proc, kill=True)
                except Exception:
                    logger.exception("Tool %s worker was not reaped after cancellation", tool.name)
                _reconcile_commands(tool.name)
                raise
            except Exception:
                logger.exception("Tool %s failed", tool.name)
                await self._reap(proc, kill=True)
                _reconcile_commands(tool.name)
                return ToolOutcome(tool.name, "error", None, _FAILED)
        finally:
            release_worker_slot(slot)
        self._remember(proc)
        outcome = _outcome_from_worker(tool.name, proc.returncode, stdout)
        _reconcile_commands(tool.name)
        return outcome

    async def _reap(self, proc: asyncio.subprocess.Process, *, kill: bool) -> None:
        if proc.returncode is None and kill:
            _kill_group(proc)
        if proc.returncode is None:
            await proc.wait()
        self._remember(proc)

    def _remember(self, proc: asyncio.subprocess.Process) -> None:
        self.reaped_workers.append(WorkerRecord(proc.pid, proc.returncode))


def _reconcile_commands(name: str) -> None:
    if name not in {"show_directory", "measure_file", "preview_text", "bounded_wait"}:
        return
    try:
        from app.commands.audit import reconcile_command_audits

        reconcile_command_audits()
    except Exception:
        logger.exception("Command audit reconciliation failed")


def _timeout_seconds(tool: Tool) -> float:
    if tool.timeout_seconds is None:
        return get_settings().tool_default_timeout_seconds
    return tool.timeout_seconds


def worker_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Build the scrubbed environment passed to an isolated tool worker.

    Only exact keys in ``_WORKER_ENV_KEYS`` are copied. Prefix wildcards are
    intentionally not used so ad-hoc secrets cannot ride along.
    """
    parent = os.environ if source is None else source
    env: dict[str, str] = {}
    for key, value in parent.items():
        if key in _WORKER_ENV_KEYS:
            env[key] = value
    env.setdefault("PATH", "/usr/bin:/bin")
    if "LC_ALL" not in env and "LANG" in env:
        env["LC_ALL"] = env["LANG"]
    return env


def _can_isolate(tool: Tool) -> bool:
    key = getattr(tool, "isolation_key", None)
    return os.name == "posix" and isinstance(key, str) and key in ALLOWED_TOOLS


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        proc.kill()


async def _bounded_communicate(
    proc: asyncio.subprocess.Process,
    payload: bytes,
    stdout_max: int,
    stderr_max: int,
) -> bytes:
    """Feed stdin and drain stdout/stderr concurrently under byte caps.

    Raises ``_StreamOverflow`` if either pipe exceeds its limit. Collected
    bytes past the limit are discarded; callers must kill and reap the worker
    and must not parse the rejected stream as a successful result.
    """
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        raise RuntimeError("worker pipes are missing")

    async def feed_stdin() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            try:
                if not proc.stdin.is_closing():
                    proc.stdin.close()
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, AssertionError, OSError):
                pass

    stdin_task = asyncio.create_task(feed_stdin())
    stdout_task = asyncio.create_task(_read_limited(proc.stdout, stdout_max))
    stderr_task = asyncio.create_task(_read_limited(proc.stderr, stderr_max))
    tasks = (stdin_task, stdout_task, stderr_task)

    async def _abort_tasks(*, kill: bool) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        # Unblock a stdin write that is stuck because the worker never read.
        # _run_isolated still calls _reap(kill=True) for wait/remember.
        if kill and proc.returncode is None:
            _kill_group(proc)
        await asyncio.gather(*tasks, return_exceptions=True)

    try:
        # Await all three together so an output overflow surfaces immediately,
        # even if stdin is blocked because the worker has not consumed it yet.
        _stdin_done, stdout, _stderr = await asyncio.gather(
            stdin_task,
            stdout_task,
            stderr_task,
        )
    except _StreamOverflow:
        await _abort_tasks(kill=True)
        raise
    except asyncio.CancelledError:
        await _abort_tasks(kill=True)
        raise
    except Exception:
        await _abort_tasks(kill=True)
        raise
    if proc.returncode is None:
        await proc.wait()
    return stdout


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
    """Read until EOF. Raise ``_StreamOverflow`` if more than ``limit`` bytes arrive."""
    buf = bytearray()
    while True:
        remaining = limit - len(buf)
        chunk = await stream.read(_STREAM_CHUNK if remaining >= _STREAM_CHUNK else remaining + 1)
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
        if len(buf) > limit:
            raise _StreamOverflow


def _outcome_from_worker(tool_name: str, returncode: int | None, stdout: bytes) -> ToolOutcome:
    payload = _load_payload(stdout)
    if payload is None:
        return ToolOutcome(tool_name, "error", None, _FAILED)
    output = payload.get("output")
    if payload.get("ok") is True and returncode == 0 and isinstance(output, dict):
        return ToolOutcome(tool_name, "ok", output, None)
    kind = payload.get("kind")
    error = payload.get("error")
    if kind == "validation" and returncode == 2 and _safe_message(error):
        raise ToolValidationError(error)
    if kind == "execution" and _safe_message(error):
        return ToolOutcome(tool_name, "error", None, error)
    return ToolOutcome(tool_name, "error", None, _FAILED)


def _load_payload(stdout: bytes) -> dict | None:
    try:
        payload = json.loads(stdout.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _safe_message(error: object) -> bool:
    return isinstance(error, str) and 0 < len(error) <= 200 and "\n" not in error and "\r" not in error
