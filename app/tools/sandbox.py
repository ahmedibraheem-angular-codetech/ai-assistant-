"""Run code in a throwaway Docker container (phase 2, slice 5).

Every call needs the user's approval (risk "sensitive"); standing approvals
never apply. Off unless SANDBOX_ENABLED=true.

Container limits, all enforced by Docker flags:
- no network (--network none), read-only root filesystem, a small /tmp
- non-root user, all capabilities dropped, no-new-privileges
- memory, CPU, and process-count limits; wall-clock timeout, then docker kill
- removed after the run (--rm); no host folder is mounted; no env passed in
- stdout/stderr are read with a byte cap; past the cap the container is killed
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from app.permissions.manifests import ToolManifest
from app.tools.base import ASYNC_READ_ONLY, Tool
from app.tools.exceptions import ToolExecutionError

TOOL_NAME = "run_code"
OUTPUT_CAP = 65_536
LANGUAGES = {
    "python": ("sandbox_python_image", ["python3", "-I", "-c"]),
    "node": ("sandbox_node_image", ["node", "-e"]),
    "shell": ("sandbox_shell_image", ["sh", "-c"]),
}


def docker_binary() -> str | None:
    configured = get_settings().sandbox_docker_path.strip()
    if configured and configured != "docker":
        return configured if Path(configured).is_file() else None
    found = shutil.which("docker")
    if found:
        return found
    for candidate in (Path.home() / ".docker" / "bin" / "docker", Path("/usr/local/bin/docker"),
                      Path("/opt/homebrew/bin/docker")):
        if candidate.is_file():
            return str(candidate)
    return None


def build_command(docker: str, name: str, language: str, code: str) -> list[str]:
    settings = get_settings()
    image_setting, interpreter = LANGUAGES[language]
    image = getattr(settings, image_setting)
    return [
        docker, "run", "--rm", "-i",
        "--name", name,
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
        "--memory", f"{settings.sandbox_memory_mb}m",
        "--memory-swap", f"{settings.sandbox_memory_mb}m",
        "--cpus", str(settings.sandbox_cpus),
        "--pids-limit", "128",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "--workdir", "/tmp",
        "--env", "HOME=/tmp",
        "--pull", "never",
        image,
        *interpreter, code,
    ]


async def _read_capped(stream, cap: int, state: dict, key: str, on_overflow=None) -> None:
    """Keep at most `cap` bytes. Past the cap, stop the container in the
    background and keep draining (discarding) so the Docker client never
    blocks on a full pipe."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        if total < cap:
            chunks.append(chunk[: cap - total])
        total += len(chunk)
        if total > cap and not state["overflow"]:
            state["overflow"] = True
            if on_overflow is not None:
                state["stopper"] = asyncio.create_task(on_overflow())
    state[key] = b"".join(chunks).decode("utf-8", errors="replace")


async def _kill(docker: str, name: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        docker, "kill", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except TimeoutError:
        proc.kill()


class RunCodeTool(Tool):
    name = TOOL_NAME
    description = (
        "Run a short Python, Node, or shell program in an isolated container with no "
        "network and no access to your files. Needs the user's approval every time. "
        "Returns stdout, stderr, and the exit code."
    )
    category = "sandbox"
    execution_policy = ASYNC_READ_ONLY  # runs a Docker child; isolation is the container
    requires_confirmation = False  # the permission gate asks every time (risk sensitive)
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["language", "code"],
        "properties": {
            "language": {"type": "string", "enum": sorted(LANGUAGES)},
            "code": {"type": "string", "minLength": 1, "maxLength": 20_000},
            "stdin": {"type": "string", "maxLength": 100_000},
        },
    }
    permission_manifest = ToolManifest(
        tool_id=TOOL_NAME,
        name=TOOL_NAME,
        description=description,
        resource_types=("none",),
        actions=("execute",),
        risk="sensitive",
        approval_required=True,
        read_only=False,
        execution_policy=ASYNC_READ_ONLY,
    )

    def __init__(self) -> None:
        self.calls = 0

    def is_enabled(self) -> bool:
        return bool(self.enabled and get_settings().sandbox_enabled)

    @property
    def timeout_seconds(self) -> float:
        return float(get_settings().sandbox_timeout_seconds) + 15.0

    async def run(self, arguments: dict) -> dict:
        self.calls += 1
        if not get_settings().sandbox_enabled:
            raise ToolExecutionError("The code sandbox is turned off.")
        docker = docker_binary()
        if docker is None:
            raise ToolExecutionError("Docker was not found. Install Docker Desktop or set SANDBOX_DOCKER_PATH.")
        name = f"paa-sandbox-{uuid4().hex[:12]}"
        command = build_command(docker, name, arguments["language"], arguments["code"])
        limit = float(get_settings().sandbox_timeout_seconds)
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "TMPDIR"}},
        )
        state: dict = {"stdout": "", "stderr": "", "overflow": False}
        timed_out = False
        try:
            if proc.stdin is not None:
                proc.stdin.write(arguments.get("stdin", "").encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            async def stop() -> None:
                await _kill(docker, name)

            readers = asyncio.gather(
                _read_capped(proc.stdout, OUTPUT_CAP, state, "stdout", stop),
                _read_capped(proc.stderr, OUTPUT_CAP, state, "stderr", stop),
            )
            try:
                await asyncio.wait_for(readers, limit)
            except TimeoutError:
                timed_out = True
            if timed_out:
                await _kill(docker, name)
            try:
                code = await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
                code = await proc.wait()
        except asyncio.CancelledError:
            await _kill(docker, name)
            proc.kill()
            raise
        stderr = state["stderr"]
        if code == 125 and "No such image" in stderr:
            image = getattr(get_settings(), LANGUAGES[arguments["language"]][0])
            raise ToolExecutionError(f"The sandbox image is missing. Run: docker pull {image}")
        return {
            "exit_code": code,
            "stdout": state["stdout"],
            "stderr": stderr,
            "timed_out": timed_out,
            "output_truncated": state["overflow"],
            "data_boundary": "untrusted_program_output",
        }
