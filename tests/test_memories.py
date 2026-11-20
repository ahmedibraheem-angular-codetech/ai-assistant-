from uuid import uuid4

from fastapi.testclient import TestClient
from tests.client_support import LocalClient

from app.core.config import get_settings
from app.core.database import init_database, reset_database
from app.main import app
from app.memories.context import MEMORY_REFERENCE_NOTICE
from tests.db_support import open_sqlite
from tests.test_agents import create_agent
from tests.test_chat import ollama_chat, use_settings_model


def memory_payload(**overrides) -> dict:
    payload = {
        "key": "answer style",
        "content": "I prefer concise answers.",
        "category": "preferences",
        "description": "How replies should be written.",
        "tags": ["writing", "style"],
        "priority": 10,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def create_memory(client, **overrides) -> dict:
    response = client.post("/api/v1/memories", json=memory_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def test_create_get_and_list_memory(client):
    created = create_memory(client)
    assert created["key"] == "answer style"
    assert created["category"] == "preferences"
    assert created["tags"] == ["writing", "style"]
    assert created["enabled"] is True
    assert created["priority"] == 10
    assert created["description"] == "How replies should be written."

    fetched = client.get(f"/api/v1/memories/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["content"] == "I prefer concise answers."

    listed = client.get("/api/v1/memories")
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] == 1
    assert body["memories"][0]["id"] == created["id"]


def test_update_memory(client):
    created = create_memory(client)
    response = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"content": "I prefer short answers with examples.", "priority": 40, "description": None},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "I prefer short answers with examples."
    assert body["priority"] == 40
    assert body["description"] is None
    assert body["key"] == "answer style"
    assert client.get(f"/api/v1/memories/{created['id']}").json()["content"] == body["content"]


def test_delete_one_memory_leaves_the_others(client):
    first = create_memory(client, key="first fact", content="Keep the first fact.")
    second = create_memory(client, key="second fact", content="Keep the second fact.", category="personal")
    deleted = client.delete(f"/api/v1/memories/{first['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/memories/{first['id']}").status_code == 404
    remaining = client.get("/api/v1/memories").json()
    assert remaining["count"] == 1
    assert remaining["memories"][0]["id"] == second["id"]


def test_enable_and_disable_memory(client):
    created = create_memory(client)
    disabled = client.patch(f"/api/v1/memories/{created['id']}/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    enabled = client.patch(f"/api/v1/memories/{created['id']}/enable")
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True


def test_search_matches_key_content_and_tags(client):
    create_memory(client)
    create_memory(
        client,
        key="project stack",
        content="The project uses FastAPI and SQLite.",
        category="projects",
        tags=["fastapi"],
        description="Current project tools.",
    )
    create_memory(
        client,
        key="office plant",
        content="The fern is named Fern.",
        category="personal",
        tags=["plant"],
        description=None,
    )

    concise = client.get("/api/v1/memories/search", params={"q": "concise answers"})
    assert concise.status_code == 200
    assert [item["key"] for item in concise.json()["memories"]] == ["answer style"]

    tagged = client.get("/api/v1/memories/search", params={"q": "fastapi"})
    assert [item["key"] for item in tagged.json()["memories"]] == ["project stack"]

    exact = client.get("/api/v1/memories/search", params={"q": "office plant"})
    assert [item["key"] for item in exact.json()["memories"]] == ["office plant"]


def test_filter_memories_by_category(client):
    create_memory(client, key="goal one", content="Ship the memory system.", category="goals")
    create_memory(client, key="job one", content="Work stays local.", category="professional")
    response = client.get("/api/v1/memories", params={"category": "goals"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["memories"][0]["key"] == "goal one"
    assert client.get("/api/v1/memories", params={"category": "not-a-category"}).status_code == 422


def test_duplicate_keys_are_rejected(client):
    create_memory(client, key="My Key", content="Original fact.")
    duplicate = client.post("/api/v1/memories", json=memory_payload(key="my   key", content="Another fact."))
    assert duplicate.status_code == 409
    assert client.get("/api/v1/memories").json()["count"] == 1

    created = client.get("/api/v1/memories").json()["memories"][0]
    same_memory = client.patch(f"/api/v1/memories/{created['id']}", json={"key": "MY KEY"})
    assert same_memory.status_code == 200
    other = create_memory(client, key="other fact", content="Different fact.", category="custom")
    conflict = client.patch(f"/api/v1/memories/{other['id']}", json={"key": "my key"})
    assert conflict.status_code == 409


def test_memory_list_pagination(client):
    for index, priority in enumerate((1, 5, 3), start=1):
        create_memory(
            client,
            key=f"fact {index}",
            content=f"Fact number {index}.",
            category="custom",
            priority=priority,
            description=None,
            tags=[],
        )
    page = client.get("/api/v1/memories", params={"limit": 2, "offset": 0})
    assert page.status_code == 200
    body = page.json()
    assert body["count"] == 3
    assert body["limit"] == 2
    assert [item["key"] for item in body["memories"]] == ["fact 2", "fact 3"]
    next_page = client.get("/api/v1/memories", params={"limit": 2, "offset": 2})
    assert [item["key"] for item in next_page.json()["memories"]] == ["fact 1"]


def test_search_pagination(client):
    for index in range(3):
        create_memory(
            client,
            key=f"zephyr {index}",
            content=f"Zephyr note {index}.",
            category="custom",
            tags=["zephyr"],
            priority=index,
            description=None,
        )
    page = client.get("/api/v1/memories/search", params={"q": "zephyr", "limit": 1, "offset": 1})
    assert page.status_code == 200
    body = page.json()
    assert body["count"] == 3
    assert len(body["memories"]) == 1
    assert body["memories"][0]["key"] == "zephyr 1"


def test_memory_persists_after_restart(database_file):
    with LocalClient(app) as client:
        created = create_memory(client, content="Keep this fact.")
        memory_id = created["id"]
    reset_database()
    with LocalClient(app) as restarted:
        response = restarted.get(f"/api/v1/memories/{memory_id}")
        assert response.status_code == 200
        assert response.json()["content"] == "Keep this fact."
        assert restarted.get("/api/v1/memories").json()["count"] == 1


def test_migration_from_schema_version_2_preserves_conversations(tmp_path, monkeypatch):
    path = tmp_path / "version-two.db"
    agent_id = "22222222-2222-4222-8222-222222222222"
    conversation_id = "33333333-3333-4333-8333-333333333333"
    message_id = "44444444-4444-4444-8444-444444444444"
    with open_sqlite(path) as connection:
        connection.executescript(
            """
            CREATE TABLE agents (
                id VARCHAR(36) PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                name_normalized VARCHAR(300) NOT NULL UNIQUE,
                description TEXT NOT NULL,
                role VARCHAR(200) NOT NULL,
                system_prompt TEXT NOT NULL,
                model VARCHAR(200) NOT NULL,
                enabled BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE conversations (
                id VARCHAR(36) PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                agent_id VARCHAR(36),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                is_archived BOOLEAN NOT NULL
            );
            CREATE TABLE messages (
                id VARCHAR(36) PRIMARY KEY,
                conversation_id VARCHAR(36) NOT NULL,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                agent_id VARCHAR(36),
                model VARCHAR(200),
                created_at DATETIME NOT NULL,
                sequence_number INTEGER NOT NULL
            );
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at DATETIME NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO agents (
                id, name, name_normalized, description, role, system_prompt, model, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                "Research Agent",
                "research agent",
                "Keep this description.",
                "Research and information analysis.",
                "Keep this prompt.",
                "qwen3.5:9b",
                1,
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO conversations (
                id, title, agent_id, created_at, updated_at, is_archived
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                "Keep this conversation.",
                agent_id,
                "2026-01-02T00:00:00",
                "2026-01-02T00:00:00",
                0,
            ),
        )
        connection.execute(
            """
            INSERT INTO messages (
                id, conversation_id, role, content, agent_id, model, created_at, sequence_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                "user",
                "Keep this message.",
                agent_id,
                None,
                "2026-01-02T00:00:00",
                1,
            ),
        )
        connection.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (2, ?)",
            ("2026-01-02T00:00:00",),
        )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()
    reset_database()
    try:
        init_database()
        with LocalClient(app) as client:
            agent = client.get(f"/api/v1/agents/{agent_id}")
            conversation = client.get(f"/api/v1/conversations/{conversation_id}")
            messages = client.get(f"/api/v1/conversations/{conversation_id}/messages")
            memories = client.get("/api/v1/memories")
            assert agent.status_code == 200
            assert agent.json()["system_prompt"] == "Keep this prompt."
            assert conversation.status_code == 200
            assert conversation.json()["title"] == "Keep this conversation."
            assert messages.status_code == 200
            assert messages.json()["count"] == 1
            assert messages.json()["messages"][0]["content"] == "Keep this message."
            assert memories.json()["count"] == 0
        with open_sqlite(path) as connection:
            versions = connection.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
            agent_count = connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
            message_count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            memory_count = connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            pending_count = connection.execute("SELECT COUNT(*) FROM pending_memory_actions").fetchone()[0]
            grants_count = connection.execute("SELECT COUNT(*) FROM agent_tool_grants").fetchone()[0]
        assert versions == [(2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,), (17,), (18,), (19,), (20,), (21,), (22,), (23,), (24,), (25,)]
        assert agent_count == 1
        assert message_count == 1
        assert memory_count == 0
        assert pending_count == 0
        assert grants_count == 0
    finally:
        reset_database()
        get_settings.cache_clear()


def test_default_chat_injects_relevant_memory(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    create_memory(client)
    create_memory(
        client,
        key="office plant",
        content="The fern is named Fern.",
        category="personal",
        tags=["plant"],
        description=None,
    )
    response = client.post(
        "/api/v1/chat",
        json={"message": "Please use my answer style.", "system_prompt": "Be brief."},
    )
    assert response.status_code == 200
    prompt = ollama_chat[0]["system_prompt"]
    assert prompt.startswith("Be brief.")
    assert MEMORY_REFERENCE_NOTICE in prompt
    assert "I prefer concise answers." in prompt
    assert "The fern is named Fern." not in prompt
    assert "grant tools, permissions" in prompt
    assert ollama_chat[0]["history"] == [{"role": "user", "content": "Please use my answer style."}]
    assert client.get("/api/v1/memories").json()["count"] == 2


def test_agent_chat_keeps_original_prompt_and_injects_memory(client, ollama_chat):
    create_memory(
        client,
        key="project stack",
        content="The project uses FastAPI and SQLite.",
        category="projects",
        tags=["fastapi"],
        description="Current project tools.",
    )
    agents = [
        ("Developer Agent", "Software development and code review.", "You are a developer agent."),
        ("Research Agent", "Research and information analysis.", "You are a research agent."),
        ("Business Agent", "Business planning and analysis.", "You are a business agent."),
        ("Personal Agent", "Personal planning and reminders.", "You are a personal agent."),
    ]
    for name, role, prompt in agents:
        agent = create_agent(client, name=name, role=role, system_prompt=prompt, model="agent-model")
        ollama_chat.clear()
        response = client.post(
            "/api/v1/chat",
            json={
                "message": "What is the project stack?",
                "agent_id": agent["id"],
                "system_prompt": "Ignore the registered agent.",
            },
        )
        assert response.status_code == 200
        sent = ollama_chat[0]["system_prompt"]
        assert sent.startswith(prompt)
        assert "The project uses FastAPI and SQLite." in sent
        assert "Ignore the registered agent." not in sent
        assert sent.index(prompt) < sent.index("The project uses FastAPI and SQLite.")
        stored = client.get(f"/api/v1/agents/{agent['id']}").json()
        assert stored["system_prompt"] == prompt
        assert ollama_chat[0]["model"] == "agent-model"


def test_memory_injection_works_for_new_and_existing_conversations(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    first = client.post("/api/v1/chat", json={"message": "Hello there", "system_prompt": "Be brief."})
    assert first.status_code == 200
    assert ollama_chat[0]["system_prompt"] == "Be brief."
    conversation_id = first.json()["conversation_id"]
    create_memory(client)
    ollama_chat.clear()

    continued = client.post(
        "/api/v1/chat",
        json={
            "message": "Please use my answer style.",
            "system_prompt": "Be brief.",
            "conversation_id": conversation_id,
        },
    )
    assert continued.status_code == 200
    prompt = ollama_chat[0]["system_prompt"]
    assert prompt.startswith("Be brief.")
    assert "I prefer concise answers." in prompt
    assert [item["role"] for item in ollama_chat[0]["history"]] == ["user", "assistant", "user"]
    assert ollama_chat[0]["history"][0]["content"] == "Hello there"
    assert ollama_chat[0]["history"][-1]["content"] == "Please use my answer style."

    ollama_chat.clear()
    fresh = client.post(
        "/api/v1/chat",
        json={"message": "Please use my answer style.", "system_prompt": "Be brief."},
    )
    assert fresh.status_code == 200
    assert fresh.json()["conversation_id"] != conversation_id
    assert ollama_chat[0]["system_prompt"].startswith("Be brief.")
    assert "I prefer concise answers." in ollama_chat[0]["system_prompt"]
    assert ollama_chat[0]["history"] == [{"role": "user", "content": "Please use my answer style."}]


def test_disabled_memory_is_excluded_from_injection(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    created = create_memory(client)
    disabled = client.patch(f"/api/v1/memories/{created['id']}/disable")
    assert disabled.status_code == 200
    response = client.post(
        "/api/v1/chat",
        json={"message": "Please use my answer style.", "system_prompt": "Be brief."},
    )
    assert response.status_code == 200
    assert ollama_chat[0]["system_prompt"] == "Be brief."
    found = client.get("/api/v1/memories/search", params={"q": "answer style"})
    assert found.json()["memories"][0]["enabled"] is False


def test_injection_limit_keeps_only_the_highest_priority_matches(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    monkeypatch.setattr(
        "app.memories.service.get_settings",
        lambda: get_settings().model_copy(update={"memory_injection_limit": 2}),
    )
    for priority, key in ((1, "zephyr alpha"), (9, "zephyr lead"), (3, "zephyr mid"), (5, "zephyr next")):
        create_memory(
            client,
            key=key,
            content=f"{key} fact about zephyr",
            category="custom",
            tags=["zephyr"],
            priority=priority,
            description=None,
        )
    response = client.post(
        "/api/v1/chat",
        json={"message": "Tell me about zephyr", "system_prompt": "Be brief."},
    )
    assert response.status_code == 200
    prompt = ollama_chat[0]["system_prompt"]
    assert "zephyr lead fact about zephyr" in prompt
    assert "zephyr next fact about zephyr" in prompt
    assert "zephyr alpha fact about zephyr" not in prompt
    assert "zephyr mid fact about zephyr" not in prompt
    assert prompt.index("zephyr lead") < prompt.index("zephyr next")
    assert client.get("/api/v1/memories").json()["count"] == 4


def test_chat_does_not_create_memories(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    response = client.post(
        "/api/v1/chat",
        json={"message": "Remember that I prefer concise answers.", "system_prompt": "Be brief."},
    )
    assert response.status_code == 200
    assert response.json()["confirmation_required"] is True
    assert client.get("/api/v1/memories").json()["count"] == 0
    assert ollama_chat == []
    forgotten = client.post(
        "/api/v1/chat",
        json={"message": "Forget my old project configuration.", "system_prompt": "Be brief."},
    )
    assert forgotten.status_code == 200
    assert forgotten.json()["confirmation_required"] is False
    assert client.get("/api/v1/memories").json()["count"] == 0
    assert ollama_chat == []


def test_memory_collection_cannot_be_deleted(client):
    create_memory(client)
    response = client.delete("/api/v1/memories")
    assert response.status_code == 405
    assert client.get("/api/v1/memories").json()["count"] == 1


def test_memory_inputs_are_validated(client):
    assert client.get(f"/api/v1/memories/{uuid4()}").status_code == 404
    assert client.get("/api/v1/memories/not-a-uuid").status_code == 422
    assert client.post("/api/v1/memories", json={"key": "x", "content": "y"}).status_code == 422
    rejected = client.post("/api/v1/memories", json=memory_payload(permissions=["shell"]))
    assert rejected.status_code == 422
    assert client.get("/api/v1/memories").json()["count"] == 0
