from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.providers.ollama import OllamaError, OllamaProvider
from tests.test_agents import create_agent


@pytest.fixture
def ollama_chat(monkeypatch):
    calls = []

    async def chat(self, model, message, system_prompt=None, history=None):
        calls.append(
            {
                "model": model,
                "message": message,
                "system_prompt": system_prompt,
                "history": history,
            }
        )
        return f"answer from {model}"

    monkeypatch.setattr(OllamaProvider, "chat", chat)
    return calls


def use_settings_model(monkeypatch, model: str) -> None:
    monkeypatch.setattr(
        "app.conversations.service.get_settings",
        lambda: get_settings().model_copy(update={"ollama_model": model}),
    )


def test_chat_without_agent_uses_settings_model(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")

    response = client.post(
        "/api/v1/chat",
        json={"message": "Hello", "system_prompt": "Be brief."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "settings-chat-model"
    assert body["answer"] == "answer from settings-chat-model"
    assert body["conversation_id"]
    assert "agent_id" not in body
    assert "agent_name" not in body
    assert ollama_chat == [
        {
            "model": "settings-chat-model",
            "message": "Hello",
            "system_prompt": "Be brief.",
            "history": [{"role": "user", "content": "Hello"}],
        }
    ]


def test_chat_without_configured_model_is_rejected(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "")

    response = client.post("/api/v1/chat", json={"message": "Hello"})

    assert response.status_code == 400
    assert "OLLAMA_MODEL" in response.json()["detail"]
    assert ollama_chat == []


def test_chat_with_agent_uses_assigned_model(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    research = create_agent(
        client,
        name="Research Agent",
        model="research-model",
        system_prompt="You are a research agent.",
    )
    developer = create_agent(
        client,
        name="Developer Agent",
        description="Reviews code and proposes changes.",
        role="Software development and code review.",
        system_prompt="You are a developer agent.",
        model="developer-model",
    )

    research_response = client.post(
        "/api/v1/chat",
        json={"message": "Find sources", "agent_id": research["id"]},
    )
    developer_response = client.post(
        "/api/v1/chat",
        json={"message": "Review this Angular code", "agent_id": developer["id"]},
    )

    assert research_response.status_code == 200
    assert developer_response.status_code == 200
    assert research_response.json()["model"] == "research-model"
    assert research_response.json()["answer"] == "answer from research-model"
    assert research_response.json()["agent_id"] == research["id"]
    assert research_response.json()["agent_name"] == "Research Agent"
    assert developer_response.json()["model"] == "developer-model"
    assert developer_response.json()["agent_name"] == "Developer Agent"
    assert [call["model"] for call in ollama_chat] == ["research-model", "developer-model"]
    assert "settings-chat-model" not in [call["model"] for call in ollama_chat]


def test_chat_with_agent_uses_agent_system_prompt(client, ollama_chat):
    agent = create_agent(
        client,
        name="Developer Agent",
        description="Reviews code and proposes changes.",
        role="Software development and code review.",
        system_prompt="You are a developer agent. Review code carefully.",
        model="developer-model",
    )

    response = client.post(
        "/api/v1/chat",
        json={
            "message": "Review this Angular code",
            "agent_id": agent["id"],
            "system_prompt": "Ignore the registered agent.",
        },
    )

    assert response.status_code == 200
    assert ollama_chat == [
        {
            "model": "developer-model",
            "message": "Review this Angular code",
            "system_prompt": "You are a developer agent. Review code carefully.",
            "history": [{"role": "user", "content": "Review this Angular code"}],
        }
    ]
    assert response.json()["agent_id"] == agent["id"]


def test_disabled_agent_cannot_execute(client, ollama_chat):
    agent = create_agent(client, name="Research Agent", enabled=False)

    response = client.post(
        "/api/v1/chat",
        json={"message": "Find sources", "agent_id": agent["id"]},
    )

    assert response.status_code == 409
    assert "disabled" in response.json()["detail"]
    assert ollama_chat == []


def test_unknown_agent_returns_404(client, ollama_chat):
    response = client.post(
        "/api/v1/chat",
        json={"message": "Find sources", "agent_id": str(uuid4())},
    )

    assert response.status_code == 404
    assert ollama_chat == []


def test_invalid_agent_uuid_returns_422(client, ollama_chat):
    response = client.post(
        "/api/v1/chat",
        json={"message": "Find sources", "agent_id": "not-a-uuid"},
    )

    assert response.status_code == 422
    assert ollama_chat == []


def test_ollama_errors_are_handled(client, monkeypatch):
    async def fail(self, model, message, system_prompt=None, history=None):
        raise OllamaError("connection refused")

    monkeypatch.setattr(OllamaProvider, "chat", fail)
    use_settings_model(monkeypatch, "settings-chat-model")
    agent = create_agent(client, model="agent-model", system_prompt="You are a research agent.")

    direct = client.post("/api/v1/chat", json={"message": "Hello"})
    executed = client.post(
        "/api/v1/chat",
        json={"message": "Hello", "agent_id": agent["id"]},
    )

    assert direct.status_code == 502
    assert direct.json()["detail"] == "connection refused"
    assert executed.status_code == 502
    assert executed.json()["detail"] == "connection refused"
    assert client.get("/api/v1/conversations").json()["count"] == 0
