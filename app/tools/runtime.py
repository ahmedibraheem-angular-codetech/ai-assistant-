"""Track isolated worker processes started by the tool executor.

The executor process is the parent. Each blocking tool runs in a child
process. This registry lets the parent stop a file worker when the grant that
allowed it is removed. It does not choose agents or answer the user.
"""

import os
import signal
import threading
from dataclasses import dataclass

from app.core.config import get_settings

_FILE_TOOLS = frozenset(
    {
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

_LOCK = threading.Lock()


@dataclass
class WorkerSlot:
    tool: str
    agent_id: str | None = None
    workspace_id: str | None = None
    pid: int | None = None
    revoked: bool = False


_slots: list[WorkerSlot] = []


def acquire_worker_slot(
    tool: str,
    *,
    agent_id: str | None,
    workspace_id: str | None,
) -> WorkerSlot | None:
    """Reserve one worker slot. None means the configured limit is already full."""
    with _LOCK:
        if len(_slots) >= get_settings().file_max_concurrent_workers:
            return None
        slot = WorkerSlot(
            tool=tool,
            agent_id=agent_id,
            workspace_id=workspace_id if isinstance(workspace_id, str) else None,
        )
        _slots.append(slot)
        return slot


def attach_worker(
    slot: WorkerSlot,
    pid: int,
    *,
    agent_id: str | None,
    workspace_id: str | None,
) -> None:
    with _LOCK:
        slot.pid = pid
        slot.agent_id = agent_id
        slot.workspace_id = workspace_id if isinstance(workspace_id, str) else None
        revoked = slot.revoked
    if revoked:
        _kill(pid)


def release_worker_slot(slot: WorkerSlot | None) -> None:
    if slot is None:
        return
    with _LOCK:
        _slots[:] = [item for item in _slots if item is not slot]


def stop_file_workers(
    *,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    tools: set[str] | None = None,
) -> list[int]:
    """SIGKILL file workers that match the revoked agent, workspace, or tool.

    A worker that has not reached a killable pid is marked revoked and killed
    as soon as its pid exists. Non-file workers are left running.
    """
    pids: list[int] = []
    with _LOCK:
        for slot in _slots:
            if slot.tool not in _FILE_TOOLS:
                continue
            if agent_id is not None and slot.agent_id != agent_id:
                continue
            if workspace_id is not None and slot.workspace_id != workspace_id:
                continue
            if tools is not None and slot.tool not in tools:
                continue
            slot.revoked = True
            if slot.pid is not None:
                pids.append(slot.pid)
    for pid in pids:
        _kill(pid)
    return pids


def _kill(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
