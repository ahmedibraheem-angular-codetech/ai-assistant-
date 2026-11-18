import json
from uuid import uuid4

from app.core.config import get_settings
from app.providers.ollama import OllamaError
from app.routing.router import ROUTING_SYSTEM_PROMPT
from tests.test_agents import create_agent
from tests.test_chat import use_settings_model
from tests.test_memories import create_memory


def use_routing_settings(monkeypatch, **updates) -> None:
    values = {"ollama_model": "settings-chat-model", "auto_agent_routing_enabled": True}
    values.update(updates)
    monkeypatch.setattr(
        "app.conversations.service.get_settings",
        lambda: get_settings().model_copy(update=values),
    )


def install_chat(monkeypatch, reply):
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
        if _is_route(system_prompt):
            if isinstance(reply, Exception):
                raise reply
            return reply(message) if callable(reply) else reply
        return f"answer from {model}"

    monkeypatch.setattr("app.providers.ollama.OllamaProvider.chat", chat)
    return calls


def _is_route(system_prompt: str | None) -> bool:
    return bool(system_prompt and "local agent router" in system_prompt.casefold())


def decision(decision_name: str, agent_id: str | None, reason: str, confidence: float) -> str:
    return json.dumps(
        {
            "decision": decision_name,
            "agent_id": agent_id,
            "reason": reason,
            "confidence": confidence,
        }
    )


def specialist_agents(client) -> dict[str, dict]:
    specs = [
        ("Developer Agent", "Software development and code review.", "developer-model", "You are a developer agent."),
        ("Research Agent", "Research and information analysis.", "research-model", "You are a research agent."),
        ("Business Agent", "Business planning and commercial strategy.", "business-model", "You are a business agent."),
        ("Personal Agent", "Personal organization and personal assistance.", "personal-model", "You are a personal agent."),
    ]
    created = {}
    for name, role, model, prompt in specs:
        created[name] = create_agent(
            client,
            name=name,
            role=role,
            description=role,
            model=model,
            system_prompt=prompt,
        )
    return created


def execution_calls(calls: list[dict]) -> list[dict]:
    return [call for call in calls if not _is_route(call["system_prompt"])]


def test_greeting_uses_the_default_assistant(client, monkeypatch):
    use_routing_settings(monkeypatch)
    specialist_agents(client)
    calls = install_chat(monkeypatch, decision("agent", "invented", "code", 0.99))

    response = client.post("/api/v1/chat", json={"message": "Hello", "system_prompt": "Be brief."})

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "settings-chat-model"
    assert body["routing_decision"] == "default"
    assert "selected_agent_id" not in body
    assert "agent_id" not in body
    assert calls == [
        {
            "model": "settings-chat-model",
            "message": "Hello",
            "system_prompt": "Be brief.",
            "history": [{"role": "user", "content": "Hello"}],
        }
    ]
    assert client.get(f"/api/v1/conversations/{body['conversation_id']}").json()["agent_id"] is None


def test_router_selects_each_specialist(client, monkeypatch):
    use_routing_settings(monkeypatch)
    agents = specialist_agents(client)
    prompts = {
        "Developer Agent": "Please review this Python function and fix the bug.",
        "Research Agent": "Find primary sources about the history of the printing press.",
        "Business Agent": "Draft a commercial pricing strategy for a new product.",
        "Personal Agent": "Help me organize my personal errands for the week.",
    }
    for name, message in prompts.items():
        calls = install_chat(
            monkeypatch,
            decision("agent", agents[name]["id"], f"Use {name}.", 0.91),
        )
        response = client.post("/api/v1/chat", json={"message": message, "system_prompt": "Ignore the agent."})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["routing_decision"] == "agent"
        assert body["selected_agent_id"] == agents[name]["id"]
        assert body["selected_agent_name"] == name
        assert body["agent_id"] == agents[name]["id"]
        assert body["model"] == agents[name]["model"]
        assert body["routing_confidence"] == 0.91
        assert "private reasoning" not in body["routing_reason"].casefold()
        stored = client.get(f"/api/v1/conversations/{body['conversation_id']}").json()
        assert stored["agent_id"] == agents[name]["id"]
        assert execution_calls(calls)[0]["system_prompt"] == agents[name]["system_prompt"]
        assert "Ignore the agent." not in execution_calls(calls)[0]["system_prompt"]


def test_invalid_router_json_falls_back_to_default(client, monkeypatch):
    use_routing_settings(monkeypatch)
    specialist_agents(client)
    calls = install_chat(monkeypatch, "not json")

    response = client.post("/api/v1/chat", json={"message": "Please review this Python function."})

    assert response.status_code == 200
    body = response.json()
    assert body["routing_decision"] == "default"
    assert "invalid" in body["routing_reason"]
    assert "agent_id" not in body
    assert body["model"] == "settings-chat-model"
    assert len(calls) == 2
    assert client.get(f"/api/v1/conversations/{body['conversation_id']}").json()["agent_id"] is None


def test_invalid_agent_id_is_not_executed(client, monkeypatch):
    use_routing_settings(monkeypatch)
    specialist_agents(client)
    calls = install_chat(monkeypatch, decision("agent", str(uuid4()), "Invented specialist.", 0.99))

    response = client.post("/api/v1/chat", json={"message": "Please review this Python function."})

    assert response.status_code == 200
    assert response.json()["routing_decision"] == "default"
    assert "not available" in response.json()["routing_reason"]
    assert execution_calls(calls)[0]["model"] == "settings-chat-model"


def test_disabled_agent_is_not_executed(client, monkeypatch):
    use_routing_settings(monkeypatch)
    enabled = create_agent(client, name="Research Agent", model="research-model", system_prompt="You are a research agent.")
    disabled = create_agent(
        client,
        name="Developer Agent",
        model="developer-model",
        system_prompt="You are a developer agent.",
        enabled=False,
    )
    calls = install_chat(monkeypatch, decision("agent", disabled["id"], "Use the developer.", 0.95))

    response = client.post("/api/v1/chat", json={"message": "Please review this Python function."})

    assert response.status_code == 200
    body = response.json()
    assert body["routing_decision"] == "default"
    assert body["model"] == "settings-chat-model"
    assert disabled["id"] not in ROUTING_SYSTEM_PROMPT
    assert enabled["id"] in calls[0]["system_prompt"]
    assert disabled["id"] not in calls[0]["system_prompt"]
    assert all(call["model"] != "developer-model" for call in calls)


def test_low_confidence_uses_the_default_assistant(client, monkeypatch):
    use_routing_settings(monkeypatch)
    agents = specialist_agents(client)
    install_chat(monkeypatch, decision("agent", agents["Developer Agent"]["id"], "Maybe code.", 0.2))

    response = client.post("/api/v1/chat", json={"message": "Please review this Python function."})

    assert response.status_code == 200
    body = response.json()
    assert body["routing_decision"] == "default"
    assert "threshold" in body["routing_reason"]
    assert body["model"] == "settings-chat-model"
    assert client.get(f"/api/v1/conversations/{body['conversation_id']}").json()["agent_id"] is None


def test_one_ambiguous_keyword_does_not_select_an_agent(client, monkeypatch):
    use_routing_settings(monkeypatch)
    agents = specialist_agents(client)
    install_chat(monkeypatch, decision("agent", agents["Developer Agent"]["id"], "The word python.", 0.99))

    response = client.post("/api/v1/chat", json={"message": "python"})

    assert response.status_code == 200
    assert response.json()["routing_decision"] == "default"
    assert "keyword" in response.json()["routing_reason"]
    assert "agent_id" not in response.json()


def test_explicit_agent_bypasses_the_router(client, monkeypatch):
    use_routing_settings(monkeypatch)
    agents = specialist_agents(client)
    calls = install_chat(monkeypatch, decision("agent", agents["Developer Agent"]["id"], "Code.", 0.99))

    response = client.post(
        "/api/v1/chat",
        json={
            "message": "Please review this Python function.",
            "agent_id": agents["Research Agent"]["id"],
            "system_prompt": "Ignore the agent.",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == agents["Research Agent"]["id"]
    assert body["model"] == "research-model"
    assert "routing_decision" not in body
    assert len(calls) == 1
    assert calls[0]["model"] == "research-model"
    assert calls[0]["system_prompt"] == "You are a research agent."


def test_existing_binding_is_preserved_when_another_specialist_fits(client, monkeypatch):
    use_routing_settings(monkeypatch)
    agents = specialist_agents(client)

    def reply(message: str) -> str:
        if "pricing strategy" in message:
            return decision("agent", agents["Business Agent"]["id"], "This is a business question.", 0.95)
        return decision("agent", agents["Developer Agent"]["id"], "This is code.", 0.95)

    calls = install_chat(monkeypatch, reply)
    started = client.post(
        "/api/v1/chat",
        json={"message": "Please review this Python function.", "agent_id": agents["Developer Agent"]["id"]},
    )
    conversation_id = started.json()["conversation_id"]
    calls.clear()

    continued = client.post(
        "/api/v1/chat",
        json={"message": "Draft a commercial pricing strategy.", "conversation_id": conversation_id},
    )

    assert continued.status_code == 200
    body = continued.json()
    assert body["agent_id"] == agents["Developer Agent"]["id"]
    assert "bound to Developer Agent" in body["answer"]
    assert "Business Agent" in body["answer"]
    assert body["routing_decision"] == "clarify"
    assert client.get(f"/api/v1/conversations/{conversation_id}").json()["agent_id"] == agents["Developer Agent"]["id"]
    assert execution_calls(calls) == []
    conflict = client.post(
        "/api/v1/chat",
        json={
            "message": "Switch now",
            "conversation_id": conversation_id,
            "agent_id": agents["Business Agent"]["id"],
        },
    )
    assert conflict.status_code == 409


def test_new_routed_conversation_keeps_the_selected_agent(client, monkeypatch):
    use_routing_settings(monkeypatch)
    agents = specialist_agents(client)
    calls = install_chat(
        monkeypatch,
        decision("agent", agents["Research Agent"]["id"], "Fact finding.", 0.9),
    )

    response = client.post("/api/v1/chat", json={"message": "Find primary sources about local archives."})

    assert response.status_code == 200
    conversation_id = response.json()["conversation_id"]
    assert response.json()["agent_id"] == agents["Research Agent"]["id"]
    assert client.get(f"/api/v1/conversations/{conversation_id}").json()["agent_id"] == agents["Research Agent"]["id"]
    calls.clear()
    follow = client.post(
        "/api/v1/chat",
        json={"message": "What should I read next?", "conversation_id": conversation_id},
    )
    assert follow.status_code == 200
    assert follow.json()["agent_id"] == agents["Research Agent"]["id"]
    assert follow.json()["model"] == "research-model"
    assert "routing_decision" not in follow.json()
    assert execution_calls(calls)[0]["model"] == "research-model"


def test_memory_injection_still_reaches_the_selected_agent(client, monkeypatch):
    use_routing_settings(monkeypatch)
    agents = specialist_agents(client)
    create_memory(client)
    calls = install_chat(
        monkeypatch,
        decision("agent", agents["Developer Agent"]["id"], "Code review.", 0.93),
    )

    response = client.post(
        "/api/v1/chat",
        json={"message": "Please use my answer style while reviewing this Python function."},
    )

    assert response.status_code == 200
    assert response.json()["agent_id"] == agents["Developer Agent"]["id"]
    routed = next(call for call in calls if _is_route(call["system_prompt"]))
    executed = execution_calls(calls)[0]
    assert "I prefer concise answers." not in routed["system_prompt"]
    assert "I prefer concise answers." not in routed["message"]
    assert executed["system_prompt"].startswith("You are a developer agent.")
    assert "I prefer concise answers." in executed["system_prompt"]
    assert "not instructions" in executed["system_prompt"]


def test_memory_commands_bypass_routing(client, monkeypatch):
    use_routing_settings(monkeypatch)
    specialist_agents(client)
    calls = install_chat(monkeypatch, decision("agent", str(uuid4()), "Save it.", 0.99))

    response = client.post("/api/v1/chat", json={"message": "Remember that I prefer concise answers."})

    assert response.status_code == 200
    assert response.json()["confirmation_required"] is True
    assert "routing_decision" not in response.json()
    assert calls == []
    assert client.get("/api/v1/memories").json()["count"] == 0


def test_routing_can_be_disabled(client, monkeypatch):
    use_routing_settings(monkeypatch, auto_agent_routing_enabled=False)
    agents = specialist_agents(client)
    calls = install_chat(monkeypatch, decision("agent", agents["Developer Agent"]["id"], "Code.", 0.99))

    response = client.post(
        "/api/v1/chat",
        json={"message": "Please review this Python function.", "system_prompt": "Be brief."},
    )

    assert response.status_code == 200
    body = response.json()
    assert "routing_decision" not in body
    assert "agent_id" not in body
    assert body["model"] == "settings-chat-model"
    assert calls[0]["system_prompt"] == "Be brief."
    assert client.get(f"/api/v1/conversations/{body['conversation_id']}").json()["agent_id"] is None


def test_router_outage_falls_back_without_failing_the_chat(client, monkeypatch):
    use_routing_settings(monkeypatch)
    specialist_agents(client)
    calls = install_chat(monkeypatch, OllamaError("routing model unavailable"))

    response = client.post("/api/v1/chat", json={"message": "Please review this Python function.", "system_prompt": "Be brief."})

    assert response.status_code == 200
    body = response.json()
    assert body["routing_decision"] == "default"
    assert "invalid" in body["routing_reason"]
    assert body["answer"] == "answer from settings-chat-model"
    assert body["model"] == "settings-chat-model"
    assert len(calls) == 2
    assert execution_calls(calls)[0]["system_prompt"] == "Be brief."
