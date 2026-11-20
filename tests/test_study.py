"""Scheduled study time (phase 4, slice 3)."""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.learning.study import AgentStudyPlanRecord, run_due
from app.research.providers import RateLimiter
from app.research.service import ResearchAgent
from app.tools.registry import get_tool_registry
from tests.test_agents import create_agent
from tests.test_research import FakeFetcher, FakeSearch, document, hit, limits, research_on  # noqa: F401
from tests.test_tools import grant, scripted_chat

URL = "https://example.com/ads"


def _agent(client, tools=("research",)):
    agent = create_agent(client, system_prompt="You are the Worker.")
    grant(client, agent["id"], list(tools))
    return agent


def _plan(client, agent, topics=("facebook ads",), hours=24):
    response = client.put(f"/api/v1/agents/{agent['id']}/study-plan", json={"topics": list(topics), "interval_hours": hours})
    assert response.status_code == 200, response.text
    return response.json()


def _fake_research():
    page = document(URL, "Short video ads get more reach in 2026. Test three hooks per campaign.")
    get_tool_registry().get("research").service = ResearchAgent(
        [FakeSearch("alpha", hits=[hit(URL, snippet="Short video ads get more reach.")])],
        FakeFetcher({URL: page}),
        limits=limits(max_queries=1, retries=0),
        limiter=RateLimiter(10),
    )


def test_without_standing_approval_study_waits_for_the_user(client, research_on):  # noqa: F811
    agent = _agent(client)
    _plan(client, agent)
    _fake_research()
    run = client.post(f"/api/v1/agents/{agent['id']}/study-now").json()
    assert run["status"] == "done"
    assert run["detail"]["approvals_waiting"] == 1
    assert run["detail"]["lessons_pending"] == 0
    assert get_tool_registry().get("research").calls == 0
    assert len(client.get("/api/v1/approvals").json()["approvals"]) == 1


def test_with_standing_approval_study_learns_into_pending_lessons(client, research_on, monkeypatch):  # noqa: F811
    agent = _agent(client)
    client.post("/api/v1/permissions/standing-approvals", json={"agent_id": agent["id"], "tool_name": "research"})
    _plan(client, agent)
    _fake_research()
    run = client.post(f"/api/v1/agents/{agent['id']}/study-now").json()
    assert run["detail"]["lessons_pending"] >= 1, run
    pending = client.get(f"/api/v1/agents/{agent['id']}/lessons", params={"status": "pending"}).json()["lessons"]
    assert pending and pending[0]["text"].startswith("[facebook ads]")
    # Web-learned lessons are not used until the user approves them.
    prompts = []
    scripted_chat(monkeypatch, lambda item, calls: prompts.append(item["system_prompt"] or "") or "ok")
    client.post("/api/v1/chat", json={"message": "plan facebook ads", "agent_id": agent["id"]})
    assert "Short video" not in prompts[0]
    client.patch(f"/api/v1/lessons/{pending[0]['id']}", json={"status": "active"})
    prompts.clear()
    client.post("/api/v1/chat", json={"message": "plan facebook ads", "agent_id": agent["id"]})
    assert "[facebook ads]" in prompts[0]


def test_study_evaluates_candidate_skills(client, monkeypatch):
    agent = _agent(client, tools=())
    client.post(f"/api/v1/agents/{agent['id']}/eval-cases", json={"prompt": "Summarize invoice", "must_include": ["EGP"]})
    from app.learning.skills import save_skill

    with Session(get_engine()) as session:
        skill = save_skill(session, agent["id"], name="invoice-summary", description="Invoice totals",
                           steps="Always write the total in EGP.")
    scripted_chat(monkeypatch, lambda item, calls: "900 EGP" if "in EGP" in (item["system_prompt"] or "") else "900")
    run = client.post(f"/api/v1/agents/{agent['id']}/study-now").json()
    assert run["detail"]["evaluations"][0]["decision"] == "promoted"
    digest = client.get("/api/v1/study/digest").json()
    assert digest["skills_promoted"] == 1
    active = client.get(f"/api/v1/agents/{agent['id']}/skills", params={"status": "active"}).json()["skills"]
    assert active[0]["id"] == skill["id"]


def test_paused_assistant_skips_study(client):
    agent = _agent(client)
    _plan(client, agent)
    client.post("/api/v1/control/pause")
    run = client.post(f"/api/v1/agents/{agent['id']}/study-now").json()
    assert run["status"] == "skipped"


def test_scheduler_runs_only_due_plans_and_reschedules(client, research_on):  # noqa: F811
    due_agent = _agent(client)
    later = create_agent(client, name="Later", system_prompt="x")
    _plan(client, due_agent, hours=6)
    _plan(client, later, hours=48)
    _fake_research()
    with Session(get_engine()) as session:
        row = session.get(AgentStudyPlanRecord, due_agent["id"])
        row.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session.commit()
    results = asyncio.run(run_due())
    assert [r["agent_id"] for r in results] == [due_agent["id"]]
    plan = client.get(f"/api/v1/agents/{due_agent['id']}/study-plan").json()
    assert plan["last_run_at"] is not None
    assert datetime.fromisoformat(plan["next_run_at"]) > datetime.now(timezone.utc) + timedelta(hours=5)


def test_topics_rotate_between_sessions(client, research_on):  # noqa: F811
    agent = _agent(client)
    client.post("/api/v1/permissions/standing-approvals", json={"agent_id": agent["id"], "tool_name": "research"})
    _plan(client, agent, topics=("a1", "b2", "c3", "d4"))
    _fake_research()
    first = client.post(f"/api/v1/agents/{agent['id']}/study-now").json()
    second = client.post(f"/api/v1/agents/{agent['id']}/study-now").json()
    assert [t["topic"] for t in first["detail"]["topics"]] == ["a1", "b2", "c3"]
    assert [t["topic"] for t in second["detail"]["topics"]][0] == "d4"


def test_plan_validation(client):
    agent = _agent(client)
    assert client.put(f"/api/v1/agents/{agent['id']}/study-plan", json={"topics": [], "interval_hours": 24}).status_code == 422
    assert client.put(f"/api/v1/agents/{agent['id']}/study-plan", json={"topics": ["x"], "interval_hours": 1}).status_code == 422
    assert client.get(f"/api/v1/agents/{agent['id']}/study-plan").status_code == 404
    _plan(client, agent)
    assert client.delete(f"/api/v1/agents/{agent['id']}/study-plan").status_code == 204
