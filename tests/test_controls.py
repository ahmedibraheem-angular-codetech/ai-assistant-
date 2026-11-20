"""Kill switch and daily agent budgets (phase 2, slice 2)."""

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_engine
from app.main import app
from app.permissions.models import PermissionAuditRecord
from tests.client_support import LocalClient
from tests.test_agents import create_agent
from tests.test_tools import grant, scripted_chat


def _calculator_agent(client, name="Research Agent"):
    agent = create_agent(client, name=name, system_prompt="Be exact.")
    grant(client, agent["id"], ["calculator"])
    return agent


def _calc(client, agent):
    return client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "2+2"}, "agent_id": agent["id"]},
    )


def _chat(client, agent):
    return client.post("/api/v1/chat", json={"message": "hi", "agent_id": agent["id"]})


def _events():
    with Session(get_engine()) as session:
        return [row.event for row in session.query(PermissionAuditRecord).all()]


def test_pause_blocks_tools_and_resume_restores_them(client):
    agent = _calculator_agent(client)
    assert _calc(client, agent).json()["status"] == "ok"
    paused = client.post("/api/v1/control/pause", json={"reason": "testing"})
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert client.get("/api/v1/control").json()["reason"] == "testing"
    body = _calc(client, agent).json()
    assert body["status"] == "error"
    assert "paused" in body["error"]
    assert client.post("/api/v1/control/resume").json() == {"paused": False}
    assert _calc(client, agent).json()["status"] == "ok"
    assert {"assistant_paused", "assistant_resumed"} <= set(_events())


def test_pause_blocks_chat(client, monkeypatch):
    agent = create_agent(client, system_prompt="Be exact.")
    scripted_chat(monkeypatch, lambda item, calls: "hello")
    assert _chat(client, agent).status_code == 200
    client.post("/api/v1/control/pause")
    blocked = _chat(client, agent)
    assert blocked.status_code == 423
    assert "paused" in blocked.json()["detail"]
    client.post("/api/v1/control/resume")
    assert _chat(client, agent).status_code == 200


def test_pause_survives_a_restart(database_file):
    with LocalClient(app) as first:
        first.post("/api/v1/control/pause", json={"reason": "night"})
    with LocalClient(app) as second:
        state = second.get("/api/v1/control").json()
        assert state["paused"] is True
        assert state["reason"] == "night"


def test_pause_stops_running_file_workers(client, monkeypatch):
    stopped = []
    monkeypatch.setattr("app.tools.runtime.stop_file_workers", lambda **kw: stopped.append(kw) or [])
    client.post("/api/v1/control/pause")
    assert stopped == [{}]


def test_daily_tool_budget(client):
    agent = _calculator_agent(client)
    response = client.put(f"/api/v1/control/agents/{agent['id']}/budget", json={"daily_tool_calls": 2})
    assert response.status_code == 200, response.text
    assert _calc(client, agent).json()["status"] == "ok"
    assert _calc(client, agent).json()["status"] == "ok"
    third = _calc(client, agent).json()
    assert third["status"] == "error"
    assert "daily tool limit (2)" in third["error"]
    usage = client.get(f"/api/v1/control/agents/{agent['id']}/budget").json()
    assert usage["tool_calls"] == 2
    assert usage["daily_tool_calls"] == 2
    assert client.delete(f"/api/v1/control/agents/{agent['id']}/budget").status_code == 200
    assert _calc(client, agent).json()["status"] == "ok"
    assert "agent_budget_set" in _events()


def test_budget_is_per_agent(client):
    first = _calculator_agent(client)
    second = _calculator_agent(client, name="Second Agent")
    client.put(f"/api/v1/control/agents/{first['id']}/budget", json={"daily_tool_calls": 1})
    assert _calc(client, first).json()["status"] == "ok"
    assert _calc(client, first).json()["status"] == "error"
    assert _calc(client, second).json()["status"] == "ok"
    assert _calc(client, second).json()["status"] == "ok"


def test_daily_chat_turn_budget(client, monkeypatch):
    agent = create_agent(client, system_prompt="Be exact.")
    scripted_chat(monkeypatch, lambda item, calls: "hello")
    client.put(f"/api/v1/control/agents/{agent['id']}/budget", json={"daily_chat_turns": 1})
    assert _chat(client, agent).status_code == 200
    blocked = _chat(client, agent)
    assert blocked.status_code == 423
    assert "daily chat limit" in blocked.json()["detail"]


def test_global_default_budget_from_settings(client, monkeypatch):
    monkeypatch.setenv("AGENT_DAILY_TOOL_CALLS", "1")
    get_settings.cache_clear()
    try:
        agent = _calculator_agent(client)
        assert _calc(client, agent).json()["status"] == "ok"
        assert _calc(client, agent).json()["status"] == "error"
    finally:
        monkeypatch.delenv("AGENT_DAILY_TOOL_CALLS")
        get_settings.cache_clear()


def test_budget_validation_and_unknown_agent(client):
    agent = _calculator_agent(client)
    assert client.put(f"/api/v1/control/agents/{agent['id']}/budget", json={"daily_tool_calls": 0}).status_code == 422
    assert client.put(f"/api/v1/control/agents/{agent['id']}/budget", json={"other": 1}).status_code == 422
    missing = "00000000-0000-4000-8000-000000000000"
    assert client.get(f"/api/v1/control/agents/{missing}/budget").status_code == 404


def test_confirmation_requests_do_not_use_budget(client):
    """Only tools that actually start are counted."""
    from app.core.controls import usage_for

    agent = _calculator_agent(client)
    client.put(f"/api/v1/control/agents/{agent['id']}/budget", json={"daily_tool_calls": 5})
    _calc(client, agent)
    assert usage_for(agent["id"])["tool_calls"] == 1


def test_control_routes_need_the_token(database_file):
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://127.0.0.1") as raw:
        assert raw.post("/api/v1/control/pause").status_code == 401
        assert raw.get("/api/v1/control").status_code == 401
