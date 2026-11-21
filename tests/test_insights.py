"""Proactive insights (phase 7, slice 3)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app import insights, routines
from app.core import controls
from app.core.database import get_engine
from app.learning import lessons
from tests.test_agents import create_agent
from tests.test_analytics import NOW, _approval


@pytest.fixture(autouse=True)
def _ollama_up(monkeypatch):
    monkeypatch.setattr(insights, "_ollama_up", lambda: True)
    monkeypatch.setattr(insights, "_ollama_cache", None)


def ids(client_or_session, now=None):
    with Session(get_engine()) as session:
        return {item["id"]: item for item in insights.collect(session, now)}


def test_empty_database_has_no_insights(client):
    assert ids(client) == {}


def test_old_pending_approvals_are_reported(client):
    agent = create_agent(client, name="A", system_prompt="x")
    with Session(get_engine()) as session:
        _approval(session, agent["id"], "t", "PENDING", NOW - timedelta(minutes=30))
        _approval(session, agent["id"], "t", "PENDING", NOW - timedelta(hours=3))
        session.commit()
    item = ids(client)["approvals-waiting"]
    assert "1 طلب" in item["detail"] and item["link"] == "/approvals" and item["severity"] == "warn"


def test_recent_approvals_are_not_reported(client):
    agent = create_agent(client, name="A", system_prompt="x")
    with Session(get_engine()) as session:
        _approval(session, agent["id"], "t", "PENDING", NOW - timedelta(minutes=10))
        session.commit()
    assert "approvals-waiting" not in ids(client)


def test_failed_routines_are_reported(client):
    with Session(get_engine()) as session:
        bad = routines.create_routine(session, name="تقرير", prompt="p", agent_id=None, time_of_day="08:00", days=[0])
        refused = routines.create_routine(session, name="آخر", prompt="p", agent_id=None, time_of_day="09:00", days=[0])
        fine = routines.create_routine(session, name="سليم", prompt="p", agent_id=None, time_of_day="10:00", days=[0])
        bad.last_status, bad.last_error = "error", "boom"
        refused.last_status = "refused"
        fine.last_status = "ok"
        session.commit()
        found = ids(client)
        assert found[f"routine-{bad.id}"]["severity"] == "warn" and "boom" in found[f"routine-{bad.id}"]["detail"]
        assert found[f"routine-{refused.id}"]["severity"] == "info"
        assert f"routine-{fine.id}" not in found


def test_budget_near_limit_is_reported(client):
    agent = create_agent(client, name="Busy", system_prompt="x")
    with Session(get_engine()) as session:
        controls.set_budget(session, agent["id"], controls.Budget(daily_tool_calls=10, daily_chat_turns=None))
    for _ in range(7):
        controls.consume_tool_call(agent["id"])
    assert f"budget-{agent['id']}-tool_calls" not in ids(client)
    controls.consume_tool_call(agent["id"])
    item = ids(client)[f"budget-{agent['id']}-tool_calls"]
    assert item["severity"] == "warn" and "8 من 10" in item["detail"]
    controls.consume_tool_call(agent["id"])
    controls.consume_tool_call(agent["id"])
    assert ids(client)[f"budget-{agent['id']}-tool_calls"]["severity"] == "high"


def test_pending_lessons_are_reported(client):
    agent = create_agent(client, name="L", system_prompt="x")
    with Session(get_engine()) as session:
        lessons.add_lesson(session, agent["id"], "درس جديد", status="pending")
    item = ids(client)["lessons-review"]
    assert item["link"] == "/team" and "1 درس" in item["detail"]


def test_long_pause_is_reported(client):
    with Session(get_engine()) as session:
        controls.set_paused(session, True, "راحة")
    assert "assistant-paused" not in ids(client)
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    assert ids(client, later)["assistant-paused"]["severity"] == "warn"


def test_ollama_down_is_reported_and_cached(client, monkeypatch):
    calls = []
    monkeypatch.setattr(insights, "_ollama_up", lambda: calls.append(1) or False)
    assert ids(client)["ollama-down"]["severity"] == "high"
    ids(client)
    assert len(calls) == 1


def test_one_failing_check_does_not_break_others(client, monkeypatch):
    def boom(session, now):
        raise RuntimeError("x")

    monkeypatch.setattr(insights, "CHECKS", [boom, insights._ollama])
    monkeypatch.setattr(insights, "_ollama_up", lambda: False)
    assert "ollama-down" in ids(client)


def test_route_returns_insights(client, monkeypatch):
    monkeypatch.setattr(insights, "_ollama_up", lambda: False)
    body = client.get("/api/v1/insights").json()
    assert body["count"] == 1 and body["insights"][0]["id"] == "ollama-down" and body["generated_at"]
