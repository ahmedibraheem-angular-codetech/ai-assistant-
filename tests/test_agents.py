import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.client_support import LocalClient
from sqlalchemy.engine.url import make_url

from app.agents.exceptions import AgentDisabledError
from app.agents.repository import AgentRepository
from app.agents.service import AgentService
from app.core.config import get_settings
from app.core.database import get_session_factory, init_database, reset_database
from app.main import app
from app.providers.ollama import OllamaError, OllamaProvider
from tests.db_support import open_sqlite

AGENT_FIELDS = {
    "id",
    "name",
    "description",
    "role",
    "system_prompt",
    "model",
    "enabled",
    "created_at",
    "updated_at",
}


def agent_payload(**overrides) -> dict:
    payload = {
        "name": "Research Agent",
        "description": "Gathers sources and summarizes findings.",
        "role": "Research and information analysis.",
        "system_prompt": "You are a research agent. Analyze information carefully and cite uncertainty.",
        "model": "qwen3.5:9b",
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def create_agent(client, **overrides) -> dict:
    response = client.post("/api/v1/agents", json=agent_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed


def test_default_database_url_is_inside_project(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    try:
        url = get_settings().database_url
    finally:
        get_settings.cache_clear()

    database = make_url(url).database
    expected = Path(__file__).resolve().parents[1] / "data" / "assistant.db"
    assert database is not None
    assert Path(database) == expected


def test_create_agent(client):
    response = client.post("/api/v1/agents", json=agent_payload())
    assert response.status_code == 201
    body = response.json()

    assert set(body) == AGENT_FIELDS
    assert body["name"] == "Research Agent"
    assert body["description"] == "Gathers sources and summarizes findings."
    assert body["role"] == "Research and information analysis."
    assert body["system_prompt"].startswith("You are a research agent.")
    assert body["model"] == "qwen3.5:9b"
    assert body["enabled"] is True
    assert body["id"]
    parse_timestamp(body["created_at"])
    parse_timestamp(body["updated_at"])
    assert "permissions" not in body
    assert "name_normalized" not in body


def test_list_agents(client):
    empty = client.get("/api/v1/agents")
    assert empty.status_code == 200
    assert empty.json() == {"agents": [], "count": 0}

    create_agent(client, name="Research Agent")
    create_agent(
        client,
        name="Developer Agent",
        description="Reviews code and proposes changes.",
        role="Software development and code review.",
        system_prompt="You are a developer agent. Review code carefully.",
        model="qwen3.5:9b",
    )

    response = client.get("/api/v1/agents")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert {agent["name"] for agent in body["agents"]} == {"Research Agent", "Developer Agent"}
    assert all(set(agent) == AGENT_FIELDS for agent in body["agents"])


def test_get_agent(client):
    created = create_agent(client)
    response = client.get(f"/api/v1/agents/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created

    uppercase = client.get(f"/api/v1/agents/{created['id'].upper()}")
    assert uppercase.status_code == 200
    assert uppercase.json()["id"] == created["id"]

    missing = client.get(f"/api/v1/agents/{uuid4()}")
    assert missing.status_code == 404


def test_update_agent(client):
    created = create_agent(client)
    time.sleep(0.02)
    response = client.patch(
        f"/api/v1/agents/{created['id']}",
        json={
            "role": "Senior research analyst.",
            "system_prompt": "You are a research agent. Prefer primary sources.",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["role"] == "Senior research analyst."
    assert body["system_prompt"] == "You are a research agent. Prefer primary sources."
    assert body["name"] == created["name"]
    assert body["description"] == created["description"]
    assert body["model"] == created["model"]
    assert body["enabled"] is True
    assert body["created_at"] == created["created_at"]
    assert body["updated_at"] != created["updated_at"]
    assert parse_timestamp(body["updated_at"]) > parse_timestamp(created["updated_at"])

    missing = client.patch(f"/api/v1/agents/{uuid4()}", json={"role": "Missing"})
    assert missing.status_code == 404


def test_delete_agent(client):
    created = create_agent(client)
    deleted = client.delete(f"/api/v1/agents/{created['id']}")
    assert deleted.status_code == 204
    assert deleted.content == b""

    assert client.get(f"/api/v1/agents/{created['id']}").status_code == 404
    assert client.delete(f"/api/v1/agents/{created['id']}").status_code == 404
    assert client.get("/api/v1/agents").json() == {"agents": [], "count": 0}

    missing = client.delete(f"/api/v1/agents/{uuid4()}")
    assert missing.status_code == 404


def test_duplicate_agent_names(client):
    research = create_agent(client, name="Research Agent")

    exact = client.post("/api/v1/agents", json=agent_payload(name="Research Agent"))
    assert exact.status_code == 409

    folded = client.post("/api/v1/agents", json=agent_payload(name="  research   agent  "))
    assert folded.status_code == 409

    developer = create_agent(
        client,
        name="Developer Agent",
        role="Software development and code review.",
        system_prompt="You are a developer agent.",
    )
    conflict = client.patch(f"/api/v1/agents/{developer['id']}", json={"name": "Research Agent"})
    assert conflict.status_code == 409
    assert client.get(f"/api/v1/agents/{developer['id']}").json()["name"] == "Developer Agent"

    renamed = client.patch(f"/api/v1/agents/{research['id']}", json={"name": "research agent"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "research agent"

    blocked = client.post("/api/v1/agents", json=agent_payload(name="RESEARCH AGENT"))
    assert blocked.status_code == 409
    assert client.get("/api/v1/agents").json()["count"] == 2


def test_invalid_input_validation(client):
    valid = agent_payload()
    invalid_bodies = [
        {},
        agent_payload(name="   "),
        agent_payload(name="A" * 101),
        agent_payload(model=""),
        agent_payload(model="qwen 3.5"),
        agent_payload(description=" "),
        {**valid, "permissions": ["shell"]},
        {**valid, "id": str(uuid4())},
        agent_payload(enabled="not-a-bool"),
    ]
    for body in invalid_bodies:
        response = client.post("/api/v1/agents", json=body)
        assert response.status_code == 422, body

    missing_prompt = agent_payload()
    del missing_prompt["system_prompt"]
    assert client.post("/api/v1/agents", json=missing_prompt).status_code == 422

    created = create_agent(client)
    assert client.get("/api/v1/agents/not-a-uuid").status_code == 422
    assert client.patch("/api/v1/agents/not-a-uuid", json={"role": "x"}).status_code == 422
    assert client.delete("/api/v1/agents/not-a-uuid").status_code == 422
    assert client.patch(f"/api/v1/agents/{created['id']}", json={}).status_code == 422
    assert client.patch(f"/api/v1/agents/{created['id']}", json={"name": None}).status_code == 422
    assert client.patch(
        f"/api/v1/agents/{created['id']}",
        json={"permissions": ["shell"]},
    ).status_code == 422
    assert client.get("/api/v1/agents", params={"enabled": "maybe"}).status_code == 422

    session = get_session_factory()()
    try:
        repository = AgentRepository(session)
        with pytest.raises(ValueError, match="Unsupported agent fields"):
            repository.apply_update(created["id"], {"permissions": ["shell"]}, datetime.now())
    finally:
        session.close()

    stored = client.get(f"/api/v1/agents/{created['id']}")
    assert stored.status_code == 200
    assert "permissions" not in stored.json()
    assert stored.json()["name"] == "Research Agent"


def test_persistence_after_restart(database_file):
    with LocalClient(app) as first_client:
        created = create_agent(first_client)
        agent_id = created["id"]

    reset_database()

    with open_sqlite(database_file) as connection:
        row = connection.execute(
            "SELECT name, model, enabled FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "Research Agent"
    assert row[1] == "qwen3.5:9b"
    assert row[2] == 1

    init_database()

    with LocalClient(app) as restarted:
        response = restarted.get(f"/api/v1/agents/{agent_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Research Agent"
        assert body["model"] == "qwen3.5:9b"
        assert body["system_prompt"] == created["system_prompt"]
        assert restarted.get("/api/v1/agents").json()["count"] == 1

    with open_sqlite(database_file) as connection:
        versions = connection.execute("SELECT version FROM schema_version").fetchall()
        agent_count = connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    assert versions == [(19,)]
    assert agent_count == 1


def test_independent_model_assignment(client):
    research = create_agent(client, name="Research Agent", model="qwen3.5:9b", role="Research and information analysis.")
    developer = create_agent(
        client,
        name="Developer Agent",
        description="Reviews code and proposes changes.",
        role="Software development and code review.",
        system_prompt="You are a developer agent. Review code carefully.",
        model="qwen3.5:9b",
    )
    assert research["model"] == "qwen3.5:9b"
    assert developer["model"] == "qwen3.5:9b"

    updated = client.patch(f"/api/v1/agents/{developer['id']}", json={"model": "future-code-model:1"})
    assert updated.status_code == 200
    assert updated.json()["model"] == "future-code-model:1"

    research_after = client.get(f"/api/v1/agents/{research['id']}")
    developer_after = client.get(f"/api/v1/agents/{developer['id']}")
    assert research_after.json()["model"] == "qwen3.5:9b"
    assert developer_after.json()["model"] == "future-code-model:1"
    assert research_after.json()["role"] == "Research and information analysis."
    assert developer_after.json()["role"] == "Software development and code review."


def test_disabled_agent_behavior(client, monkeypatch):
    async def list_models(_self):
        return {"models": [{"name": "qwen3.5:9b", "size": 10, "modified_at": "2026-01-01T00:00:00Z"}], "count": 1}

    monkeypatch.setattr(OllamaProvider, "list_models", list_models)

    disabled = create_agent(client, name="Research Agent", enabled=False)
    active = create_agent(
        client,
        name="Developer Agent",
        description="Reviews code and proposes changes.",
        role="Software development and code review.",
        system_prompt="You are a developer agent. Review code carefully.",
        enabled=True,
    )

    assert client.get("/api/v1/agents").json()["count"] == 2
    enabled_only = client.get("/api/v1/agents", params={"enabled": True}).json()
    disabled_only = client.get("/api/v1/agents", params={"enabled": False}).json()
    assert [agent["name"] for agent in enabled_only["agents"]] == ["Developer Agent"]
    assert [agent["name"] for agent in disabled_only["agents"]] == ["Research Agent"]

    detail = client.get(f"/api/v1/agents/{disabled['id']}")
    assert detail.status_code == 200
    assert detail.json()["enabled"] is False

    availability = client.get(f"/api/v1/agents/{disabled['id']}/models")
    assert availability.status_code == 200
    availability_body = availability.json()
    assert availability_body["runnable"] is False
    assert availability_body["enabled"] is False
    assert availability_body["assigned_model"] == "qwen3.5:9b"
    assert "will not be invoked" in availability_body["invocation"]
    assert "provider-level" in availability_body["availability"]

    session = get_session_factory()()
    try:
        service = AgentService(AgentRepository(session))
        with pytest.raises(AgentDisabledError):
            service.resolve_for_invocation(disabled["id"])
        resolved = service.resolve_for_invocation(active["id"])
        assert resolved.name == "Developer Agent"
        assert resolved.enabled is True
    finally:
        session.close()

    turned_off = client.patch(f"/api/v1/agents/{active['id']}", json={"enabled": False})
    assert turned_off.status_code == 200
    assert client.get(f"/api/v1/agents/{disabled['id']}").json()["enabled"] is False

    session = get_session_factory()()
    try:
        service = AgentService(AgentRepository(session))
        with pytest.raises(AgentDisabledError):
            service.resolve_for_invocation(active["id"])
    finally:
        session.close()

    turned_on = client.patch(f"/api/v1/agents/{disabled['id']}", json={"enabled": True})
    assert turned_on.status_code == 200
    assert turned_on.json()["enabled"] is True
    assert client.get(f"/api/v1/agents/{active['id']}").json()["enabled"] is False

    session = get_session_factory()()
    try:
        service = AgentService(AgentRepository(session))
        assert service.resolve_for_invocation(disabled["id"]).enabled is True
        with pytest.raises(AgentDisabledError):
            service.resolve_for_invocation(active["id"])
    finally:
        session.close()


def test_agent_model_availability_comes_from_provider(client, monkeypatch):
    research = create_agent(client, name="Research Agent", model="qwen3.5:9b")
    developer = create_agent(
        client,
        name="Developer Agent",
        description="Reviews code and proposes changes.",
        role="Software development and code review.",
        system_prompt="You are a developer agent.",
        model="future-code-model:1",
    )

    async def list_models(_self):
        return {
            "models": [
                {"name": "qwen3.5:9b", "size": 100, "modified_at": "2026-01-01T00:00:00Z"},
            ],
            "count": 1,
        }

    monkeypatch.setattr(OllamaProvider, "list_models", list_models)

    found = client.get(f"/api/v1/agents/{research['id']}/models")
    assert found.status_code == 200
    body = found.json()
    assert body["source"] == "provider"
    assert body["provider"] == "ollama"
    assert body["assigned_model"] == "qwen3.5:9b"
    assert body["assigned_model_available"] is True
    assert body["runnable"] is True
    assert [model["name"] for model in body["models"]] == ["qwen3.5:9b"]
    assert "provider-level" in body["availability"]

    missing_model = client.get(f"/api/v1/agents/{developer['id']}/models")
    assert missing_model.status_code == 200
    assert missing_model.json()["assigned_model_available"] is False
    assert missing_model.json()["assigned_model"] == "future-code-model:1"

    before = client.get(f"/api/v1/agents/{research['id']}").json()

    async def fail_list_models(_self):
        raise OllamaError("connection refused")

    monkeypatch.setattr(OllamaProvider, "list_models", fail_list_models)
    unavailable = client.get(f"/api/v1/agents/{research['id']}/models")
    assert unavailable.status_code == 200
    unavailable_body = unavailable.json()
    assert unavailable_body["models"] == []
    assert unavailable_body["assigned_model_available"] is None
    assert unavailable_body["assigned_model"] == "qwen3.5:9b"
    assert "could not be reached" in unavailable_body["availability"]
    assert "connection refused" in unavailable_body["provider_error"]

    after = client.get(f"/api/v1/agents/{research['id']}").json()
    assert after == before

    async def explode(_self):
        raise AssertionError("provider should not be called")

    monkeypatch.setattr(OllamaProvider, "list_models", explode)
    assert client.get("/api/v1/agents/not-a-uuid/models").status_code == 422
    assert client.get(f"/api/v1/agents/{uuid4()}/models").status_code == 404
