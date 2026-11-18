from tests.db_support import open_sqlite
from tests.test_agents import create_agent
from tests.test_chat import ollama_chat, use_settings_model
from tests.test_memories import create_memory


def propose(client, message: str, **extra):
    response = client.post("/api/v1/memories/actions/propose", json={"message": message, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_remember_proposal_does_not_save_until_confirm(client):
    proposal = propose(client, "Remember that I prefer concise answers.")
    assert proposal["intent"] == "remember"
    assert proposal["proposed_operation"] == "create"
    assert proposal["confirmation_required"] is True
    assert proposal["action_id"]
    assert proposal["expires_at"]
    assert proposal["proposed_memory"]["content"] == "I prefer concise answers"
    assert proposal["proposed_memory"]["category"] == "preferences"
    assert client.get("/api/v1/memories").json()["count"] == 0

    confirmed = client.post(f"/api/v1/memories/actions/{proposal['action_id']}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["operation"] == "create"
    stored = client.get("/api/v1/memories").json()
    assert stored["count"] == 1
    assert stored["memories"][0]["content"] == "I prefer concise answers"
    assert stored["memories"][0]["id"] == confirmed.json()["memories"][0]["id"]


def test_cancel_remember_does_not_save(client):
    proposal = propose(client, "Remember that I prefer concise answers.")
    cancelled = client.post(f"/api/v1/memories/actions/{proposal['action_id']}/cancel", json={})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.get("/api/v1/memories").json()["count"] == 0
    stored = client.get(f"/api/v1/memories/actions/{proposal['action_id']}")
    assert stored.json()["status"] == "cancelled"


def test_forget_proposal_deletes_only_the_selected_memory(client):
    keep = create_memory(client, key="office plant", content="The fern is named Fern.", category="personal", tags=["plant"])
    remove = create_memory(client, key="piper voice", content="I use Piper.", category="preferences", tags=["piper"])
    proposal = propose(client, "Forget that I use Piper.")
    assert proposal["confirmation_required"] is True
    assert proposal["proposed_operation"] == "delete"
    assert [item["id"] for item in proposal["matching_memories"]] == [remove["id"]]
    assert client.get("/api/v1/memories").json()["count"] == 2

    confirmed = client.post(f"/api/v1/memories/actions/{proposal['action_id']}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["memories"][0]["id"] == remove["id"]
    remaining = client.get("/api/v1/memories").json()
    assert remaining["count"] == 1
    assert remaining["memories"][0]["id"] == keep["id"]


def test_cancel_forget_does_not_delete(client):
    created = create_memory(client, key="piper voice", content="I use Piper.", category="preferences", tags=["piper"])
    proposal = propose(client, "Forget that I use Piper.")
    cancelled = client.post(f"/api/v1/memories/actions/{proposal['action_id']}/cancel", json={})
    assert cancelled.status_code == 200
    assert client.get(f"/api/v1/memories/{created['id']}").status_code == 200


def test_expired_action_cannot_execute(client, database_file):
    proposal = propose(client, "Remember that I prefer concise answers.")
    with open_sqlite(database_file) as connection:
        connection.execute(
            "UPDATE pending_memory_actions SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00", proposal["action_id"]),
        )
    confirmed = client.post(f"/api/v1/memories/actions/{proposal['action_id']}/confirm", json={})
    assert confirmed.status_code == 409
    assert "expired" in confirmed.json()["detail"].casefold()
    assert client.get("/api/v1/memories").json()["count"] == 0


def test_action_cannot_execute_twice(client):
    proposal = propose(client, "Remember that I prefer concise answers.")
    action_id = proposal["action_id"]
    assert client.post(f"/api/v1/memories/actions/{action_id}/confirm", json={}).status_code == 200
    again = client.post(f"/api/v1/memories/actions/{action_id}/confirm", json={})
    assert again.status_code == 409
    assert client.get("/api/v1/memories").json()["count"] == 1


def test_confirmation_cannot_change_the_proposed_operation(client):
    keep = create_memory(client, key="office plant", content="The fern is named Fern.", category="personal", tags=["plant"])
    proposal = propose(client, "Remember that I prefer concise answers.")
    rejected = client.post(
        f"/api/v1/memories/actions/{proposal['action_id']}/confirm",
        json={"operation": "delete", "target_memory_ids": [keep["id"]]},
    )
    assert rejected.status_code == 422
    assert client.get("/api/v1/memories").json()["count"] == 1
    assert client.get(f"/api/v1/memories/{keep['id']}").status_code == 200

    confirmed = client.post(f"/api/v1/memories/actions/{proposal['action_id']}/confirm", json={})
    assert confirmed.status_code == 200
    assert confirmed.json()["operation"] == "create"
    stored = client.get("/api/v1/memories").json()
    assert stored["count"] == 2
    assert keep["id"] in {item["id"] for item in stored["memories"]}


def test_unrelated_yes_does_not_confirm(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    first = client.post(
        "/api/v1/chat",
        json={"message": "Remember that I prefer concise answers.", "system_prompt": "Be brief."},
    )
    assert first.status_code == 200
    conversation_id = first.json()["conversation_id"]
    action_id = first.json()["memory_action_id"]
    assert ollama_chat == []

    unrelated = client.post("/api/v1/chat", json={"message": "yes", "system_prompt": "Be brief."})
    assert unrelated.status_code == 200
    assert unrelated.json()["conversation_id"] != conversation_id
    assert "memory_action_id" not in unrelated.json()
    assert ollama_chat[0]["system_prompt"] == "Be brief."
    assert ollama_chat[0]["message"] == "yes"
    assert client.get("/api/v1/memories").json()["count"] == 0
    assert client.get(f"/api/v1/memories/actions/{action_id}").json()["status"] == "pending"

    ollama_chat.clear()
    confirmed = client.post(
        "/api/v1/chat",
        json={"message": "confirm", "conversation_id": conversation_id, "system_prompt": "Be brief."},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["memory_action_id"] == action_id
    assert confirmed.json()["confirmation_required"] is False
    assert ollama_chat == []
    assert client.get("/api/v1/memories").json()["count"] == 1


def test_ambiguous_requests_ask_for_clarification(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    create_memory(client, key="home piper", content="I use Piper at home.", category="preferences", tags=["piper"])
    create_memory(client, key="work piper", content="I use Piper at work.", category="professional", tags=["piper"])

    empty = propose(client, "Remember that.")
    assert empty["confirmation_required"] is False
    assert empty["action_id"] is None
    assert empty["clarification"]

    multiple = propose(client, "Forget that I use Piper.")
    assert multiple["confirmation_required"] is False
    assert multiple["action_id"] is None
    assert len(multiple["matching_memories"]) == 2
    assert client.get("/api/v1/memories").json()["count"] == 2

    quoted = client.post(
        "/api/v1/chat",
        json={
            "message": 'The example was "Remember that I prefer concise answers."',
            "system_prompt": "Be brief.",
        },
    )
    assert quoted.status_code == 200
    assert "memory_intent" not in quoted.json()
    assert ollama_chat[0]["system_prompt"] == "Be brief."

    ollama_chat.clear()
    hypothetical = client.post(
        "/api/v1/chat",
        json={"message": "If I said remember that I like tea, would you save it?", "system_prompt": "Be brief."},
    )
    assert hypothetical.status_code == 200
    assert "memory_intent" not in hypothetical.json()
    assert ollama_chat[0]["message"] == "If I said remember that I like tea, would you save it?"
    assert client.get("/api/v1/memories").json()["count"] == 2


def test_normal_and_agent_chat_are_unaffected(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    normal = client.post("/api/v1/chat", json={"message": "Hello", "system_prompt": "Be brief."})
    assert normal.status_code == 200
    assert "memory_intent" not in normal.json()
    assert ollama_chat[0]["system_prompt"] == "Be brief."
    assert ollama_chat[0]["history"] == [{"role": "user", "content": "Hello"}]

    agent = create_agent(
        client,
        name="Developer Agent",
        role="Software development and code review.",
        system_prompt="You are a developer agent.",
        model="agent-model",
    )
    ollama_chat.clear()
    response = client.post(
        "/api/v1/chat",
        json={"message": "Review this function", "agent_id": agent["id"], "system_prompt": "Ignore the registered agent."},
    )
    assert response.status_code == 200
    assert response.json()["agent_id"] == agent["id"]
    assert "memory_intent" not in response.json()
    assert ollama_chat[0]["system_prompt"] == "You are a developer agent."
    assert ollama_chat[0]["model"] == "agent-model"
    assert client.get(f"/api/v1/agents/{agent['id']}").json()["system_prompt"] == "You are a developer agent."


def test_disabled_memory_stays_out_of_injection_after_a_confirmed_disable(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    created = create_memory(client)
    proposal = propose(client, "Stop using my answer style.")
    assert proposal["proposed_operation"] == "disable"
    assert proposal["matching_memories"][0]["id"] == created["id"]
    assert client.post(f"/api/v1/memories/actions/{proposal['action_id']}/confirm", json={}).status_code == 200
    assert client.get(f"/api/v1/memories/{created['id']}").json()["enabled"] is False

    response = client.post(
        "/api/v1/chat",
        json={"message": "Please use my answer style.", "system_prompt": "Be brief."},
    )
    assert response.status_code == 200
    assert ollama_chat[0]["system_prompt"] == "Be brief."


def test_list_memories_distinguishes_disabled_records(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    enabled = create_memory(client, key="goal one", content="Ship the assistant.", category="goals", priority=1)
    disabled = create_memory(client, key="goal two", content="Pause the side project.", category="goals", priority=5)
    assert client.patch(f"/api/v1/memories/{disabled['id']}/disable").status_code == 200
    create_memory(client, key="project stack", content="The project uses FastAPI.", category="projects")

    listed = propose(client, "List my goal memories", limit=1)
    assert listed["intent"] == "list_memories"
    assert listed["confirmation_required"] is False
    assert listed["count"] == 2
    assert len(listed["matching_memories"]) == 1
    assert listed["matching_memories"][0]["enabled"] is False
    assert "[disabled]" in listed["answer"]
    assert "FastAPI" not in listed["answer"]

    chat = client.post("/api/v1/chat", json={"message": "What do you remember about me?", "system_prompt": "Be brief."})
    assert chat.status_code == 200
    assert chat.json()["memory_intent"] == "list_memories"
    assert ollama_chat == []
    states = {item["id"]: item["enabled"] for item in chat.json()["memories"]}
    assert states[enabled["id"]] is True
    assert states[disabled["id"]] is False
