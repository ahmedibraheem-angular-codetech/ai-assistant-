"""Tool run log, schema 25 (phase 5): every started run, with or without approval."""

import time

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.permissions.manifests import ToolManifest
from app.tools.base import ASYNC_READ_ONLY, Tool
from app.tools.registry import get_tool_registry
from app.tools.runs import ToolRunRecord
from tests.test_agents import create_agent
from tests.test_approvals import _ask, probe  # noqa: F401 - pytest fixture
from tests.test_tools import grant, scripted_chat, tool_call


def _runs():
    with Session(get_engine()) as session:
        return session.query(ToolRunRecord).order_by(ToolRunRecord.created_at).all()


def _calc(client, agent, expression="2+2"):
    return client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": expression}, "agent_id": agent["id"]},
    ).json()


def test_a_run_is_logged_without_arguments_or_output(client):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["calculator"])
    assert _calc(client, agent)["status"] == "ok"

    [run] = _runs()
    assert (run.tool_name, run.status, run.agent_id, run.error) == ("calculator", "ok", agent["id"], None)
    assert run.duration_ms >= 0
    columns = {column["name"] for column in inspect(get_engine()).get_columns("tool_runs")}
    assert columns == {"id", "created_at", "agent_id", "tool_name", "status", "duration_ms", "approval_id", "error"}


class BrokenProbe(Tool):
    name = "broken_probe"
    description = "Always fails while running."
    category = "test"
    requires_confirmation = False
    execution_policy = ASYNC_READ_ONLY
    enabled = True
    input_schema = {"type": "object", "additionalProperties": False, "properties": {}}
    permission_manifest = ToolManifest(
        tool_id="broken_probe", name="broken_probe", description="Always fails while running.",
        resource_types=("none",), actions=("read",), risk="read", approval_required=False,
        read_only=True, execution_policy=ASYNC_READ_ONLY,
    )

    async def run(self, arguments: dict) -> dict:
        raise RuntimeError("x" * 500)


@pytest.fixture
def broken(client):
    tool = BrokenProbe()
    get_tool_registry().register(tool)
    try:
        yield tool
    finally:
        get_tool_registry().unregister(tool.name)


def test_a_failed_run_keeps_its_status_and_a_short_error(client, broken):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["broken_probe"])
    body = client.post("/api/v1/tools/broken_probe/test", json={"arguments": {}, "agent_id": agent["id"]}).json()
    assert body["status"] == "error", body
    [run] = _runs()
    assert run.status == "error"
    assert run.error and len(run.error) <= 180


def test_a_run_refused_by_pause_is_logged_as_refused(client):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["calculator"])
    client.post("/api/v1/control/pause")
    assert _calc(client, agent)["status"] == "error"
    client.post("/api/v1/control/resume")
    [run] = _runs()
    assert run.status == "refused" and "paused" in run.error


def test_a_tool_call_inside_a_chat_turn_commits_with_the_turn_without_waiting(client, monkeypatch):
    agent = create_agent(client, system_prompt="Use tools.")
    grant(client, agent["id"], ["calculator"])

    def responder(item, calls):
        return tool_call("calculator", {"expression": "6*7"}) if len(calls) == 1 else "42"

    scripted_chat(monkeypatch, responder)
    started = time.monotonic()
    reply = client.post("/api/v1/chat", json={"message": "6 times 7?", "agent_id": agent["id"]})
    assert reply.status_code == 200, reply.text
    assert time.monotonic() - started < 10  # no 30 s SQLite lock wait
    [run] = _runs()
    assert (run.tool_name, run.status, run.agent_id) == ("calculator", "ok", agent["id"])

    summary = client.get("/api/v1/analytics/summary?range=today").json()
    assert summary["kpis"]["tool_actions"]["completed"] == 1
    assert summary["tools"][0]["tool"] == "calculator"
    assert summary["agents"][0]["tool_actions"] == 1


def test_a_run_after_approval_carries_the_approval_id(client, probe):  # noqa: F811
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["readable_probe"])
    approval_id = _ask(client, agent["id"])
    assert _runs() == []  # asking is not running
    started = time.monotonic()
    assert client.post(f"/api/v1/approvals/{approval_id}/approve").json()["status"] == "COMPLETED"
    assert time.monotonic() - started < 10
    [run] = _runs()
    assert (run.tool_name, run.status, run.approval_id) == ("readable_probe", "ok", approval_id)
