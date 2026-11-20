"""Dashboard analytics summary (phase 5)."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app import analytics
from app.approvals.models import ApprovalRequestRecord
from app.core.database import get_engine
from app.learning import lessons
from tests.test_agents import create_agent
from tests.test_tools import scripted_chat

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _approval(session, agent_id, tool, status, created_at, description="do a thing"):
    session.add(ApprovalRequestRecord(
        id=str(uuid4()), agent_id=agent_id, tool_name=tool, action="execute", resource_type="tool",
        resource_id=tool, relative_path=None, arguments_json=json.dumps({}), fingerprint=uuid4().hex,
        description=description, risk="medium", reason="test", mode="ask", status=status,
        created_at=created_at, expires_at=created_at + timedelta(hours=1), decided_at=None,
        execution_status=None, result_text=None,
    ))


def test_empty_database_gives_zeros_not_errors(client):
    body = client.get("/api/v1/analytics/summary").json()
    assert body["range"] == "7d"
    assert body["kpis"]["chat_turns"] == {"value": 0, "previous": 0}
    assert body["kpis"]["tool_actions"]["success_rate"] is None
    assert body["kpis"]["approvals_pending"] == {"value": 0, "oldest_at": None}
    assert body["agents"] == [] and body["queue"] == [] and body["tools"] == []
    assert len(body["trend"]["chat_turns"]) == 7 and body["trend"]["step"] == "day"
    assert body["systems"]["knowledge"] == {"documents": 0, "chunks": 0}
    assert body["paused"] is False


def test_chat_turns_are_counted_per_agent(client, monkeypatch):
    agent = create_agent(client, name="Writer", system_prompt="Be brief.")
    scripted_chat(monkeypatch, lambda item, calls: "ok")
    for _ in range(3):
        assert client.post("/api/v1/chat", json={"message": "hi", "agent_id": agent["id"]}).status_code == 200

    body = client.get("/api/v1/analytics/summary?range=today").json()
    assert body["kpis"]["chat_turns"]["value"] == 3
    assert body["kpis"]["conversations"]["value"] == 3
    [row] = body["agents"]
    assert row["name"] == "Writer" and row["chat_turns"] == 3 and row["last_active_at"]
    assert sum(body["trend"]["chat_turns"]) == 3
    assert len(body["trend"]["starts"]) == 24 and body["trend"]["step"] == "hour"


def test_approvals_feed_queue_tools_success_rate_and_trend(client):
    agent = create_agent(client, name="Researcher", system_prompt="Be exact.")
    with Session(get_engine()) as session:
        _approval(session, agent["id"], "run_code", "COMPLETED", NOW - timedelta(days=1))
        _approval(session, agent["id"], "run_code", "FAILED", NOW - timedelta(days=2))
        _approval(session, agent["id"], "write_file", "COMPLETED", NOW - timedelta(hours=3))
        _approval(session, agent["id"], "write_file", "PENDING", NOW - timedelta(minutes=5), "save a draft")
        _approval(session, agent["id"], "run_code", "COMPLETED", NOW - timedelta(days=20))  # outside 7d
        session.commit()

    body = client.get("/api/v1/analytics/summary?range=7d").json()
    actions = body["kpis"]["tool_actions"]
    assert (actions["completed"], actions["failed"], actions["success_rate"]) == (2, 1, 0.667)
    assert body["kpis"]["approvals_pending"]["value"] == 1
    assert body["queue"][0]["status"] == "PENDING"
    assert body["queue"][0]["description"] == "save a draft"
    assert body["queue"][0]["agent_name"] == "Researcher"
    assert {row["tool"]: (row["actions"], row["failed"]) for row in body["tools"]} == {
        "run_code": (2, 1), "write_file": (2, 0)}
    assert sum(body["trend"]["tool_completed"]) == 2 and sum(body["trend"]["tool_failed"]) == 1
    assert body["agents"][0]["tool_actions"] == 4

    month = client.get("/api/v1/analytics/summary?range=30d").json()
    assert month["kpis"]["tool_actions"]["completed"] == 3
    assert len(month["trend"]["starts"]) == 30


def test_lessons_and_learning_counts(client):
    agent = create_agent(client, name="Student", system_prompt="Learn.")
    with Session(get_engine()) as session:
        lessons.add_lesson(session, agent["id"], "Prices are in EGP.", topic="pricing", source="self", status="pending")
        lessons.add_lesson(session, agent["id"], "Keep answers short.", topic="style", source="user")
        session.commit()
    body = client.get("/api/v1/analytics/summary").json()
    assert body["kpis"]["lessons_new"] == {"value": 2, "pending": 1}
    assert body["learning"]["lessons_active"] == 1 and body["learning"]["lessons_pending"] == 1
    assert body["agents"][0]["lessons_pending"] == 1


def test_trend_buckets_line_up_with_the_range(database_file, client):
    with Session(get_engine()) as session:
        _approval(session, None, "run_code", "COMPLETED", NOW - timedelta(days=6, hours=12))
        _approval(session, None, "run_code", "COMPLETED", NOW - timedelta(minutes=30))
        session.commit()
        body = analytics.summary(session, "7d", now=NOW.replace(tzinfo=timezone.utc))
    assert body["trend"]["tool_completed"][0] == 1
    assert body["trend"]["tool_completed"][-1] == 1


def test_unknown_range_is_refused(client):
    assert client.get("/api/v1/analytics/summary?range=year").status_code == 422
