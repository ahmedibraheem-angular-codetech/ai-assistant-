"""Code sandbox (phase 2, slice 5).

Unit tests check the Docker command and the permission rules. Live tests run
only when RUN_DOCKER_TESTS=1 and Docker is available.
"""

import asyncio
import os

import pytest

from app.core.config import get_settings
from app.permissions.manifests import manifest_for_tool
from app.permissions.standing import eligible
from app.tools.registry import get_tool_registry
from app.tools.sandbox import RunCodeTool, build_command, docker_binary
from tests.test_agents import create_agent
from tests.test_permissions import _mode
from tests.test_tools import grant


@pytest.fixture
def sandbox_on(monkeypatch):
    monkeypatch.setenv("SANDBOX_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_container_flags_lock_everything_down():
    command = build_command("/usr/bin/docker", "paa-sandbox-x", "python", "print(1)")
    joined = " ".join(command)
    for flag in [
        "--rm",
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 65534:65534",
        "--pids-limit 128",
        "--memory 512m",
        "--memory-swap 512m",
        "--pull never",
    ]:
        assert flag in joined, flag
    assert "-v" not in command and "--volume" not in command and "--mount" not in command
    assert "--privileged" not in command
    assert command[-1] == "print(1)"
    assert command[-3:-1] == ["-I", "-c"]


def test_no_host_environment_is_passed_into_the_container():
    command = build_command("/usr/bin/docker", "n", "shell", "env")
    env_flags = [command[i + 1] for i, part in enumerate(command) if part in {"-e", "--env"}]
    assert env_flags == ["HOME=/tmp"]


def test_manifest_always_needs_approval_and_never_gets_standing_approval():
    tool = get_tool_registry().get("run_code")
    manifest = manifest_for_tool(tool)
    assert manifest.risk == "sensitive"
    assert manifest.approval_required is True
    assert eligible(manifest) is False


def test_off_by_default(client):
    assert get_tool_registry().get("run_code").is_enabled() is False


def test_every_run_asks_for_approval(client, sandbox_on):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["run_code"])
    response = client.post(
        "/api/v1/tools/run_code/test",
        json={"arguments": {"language": "python", "code": "print(1)"}, "agent_id": agent["id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmation_required"


def test_standing_approval_is_refused_for_the_sandbox(client, sandbox_on):
    agent = create_agent(client, system_prompt="Be exact.")
    response = client.post(
        "/api/v1/permissions/standing-approvals",
        json={"agent_id": agent["id"], "tool_name": "run_code"},
    )
    assert response.status_code == 422


def test_restricted_mode_denies_the_sandbox(client, sandbox_on):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["run_code"])
    _mode(client, "restricted_auto")
    response = client.post(
        "/api/v1/tools/run_code/test",
        json={"arguments": {"language": "python", "code": "print(1)"}, "agent_id": agent["id"]},
    )
    assert response.status_code == 403


def test_unknown_language_is_rejected(client, sandbox_on):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["run_code"])
    response = client.post(
        "/api/v1/tools/run_code/test",
        json={"arguments": {"language": "ruby", "code": "puts 1"}, "agent_id": agent["id"]},
    )
    assert response.status_code == 422


_LIVE = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_TESTS") != "1" or docker_binary() is None,
    reason="set RUN_DOCKER_TESTS=1 with Docker running",
)


def _run(code, language="python", stdin=""):
    return asyncio.run(RunCodeTool().run({"language": language, "code": code, "stdin": stdin}))


@_LIVE
def test_live_hello(sandbox_on):
    out = _run("print(2+2)")
    assert out["exit_code"] == 0
    assert out["stdout"].strip() == "4"


@_LIVE
def test_live_no_network_no_host_files_read_only(sandbox_on):
    out = _run(
        "import os, urllib.request\n"
        "try:\n urllib.request.urlopen('http://1.1.1.1', timeout=3); print('NET')\n"
        "except Exception: print('nonet')\n"
        "print(os.path.exists('/Users'))\n"
        "try:\n open('/etc/x','w'); print('RW')\nexcept OSError: print('ro')\n"
    )
    assert out["stdout"].split() == ["nonet", "False", "ro"]


@_LIVE
def test_live_timeout_and_output_cap(sandbox_on, monkeypatch):
    monkeypatch.setenv("SANDBOX_TIMEOUT_SECONDS", "3")
    get_settings.cache_clear()
    assert _run("while True: pass")["timed_out"] is True
    flood = _run("import sys\nwhile True: sys.stdout.write('x'*65536)")
    assert flood["output_truncated"] is True
    assert flood["timed_out"] is False


@_LIVE
def test_live_runs_only_after_the_user_approves(client, sandbox_on):
    from tests.approval_support import finish_tool_test

    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["run_code"])
    proposed = client.post(
        "/api/v1/tools/run_code/test",
        json={"arguments": {"language": "node", "code": "console.log(6*7)"}, "agent_id": agent["id"]},
    )
    assert proposed.json()["status"] == "confirmation_required"
    done = finish_tool_test(client, proposed)
    assert done.json()["status"] == "ok", done.json()
    assert done.json()["output"]["stdout"].strip() == "42"
