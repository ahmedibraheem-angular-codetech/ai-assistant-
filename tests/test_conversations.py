from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from tests.client_support import LocalClient

from app.core.config import get_settings
from app.core.database import init_database, reset_database
from app.main import app
from app.providers.ollama import OllamaProvider
from tests.db_support import open_sqlite
from tests.test_agents import create_agent
from tests.test_chat import ollama_chat, use_settings_model


def test_new_chat_creates_conversation_and_saves_messages(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")

    response = client.post(
        "/api/v1/chat",
        json={"message": "  Hello\nworld  ", "system_prompt": "Be brief."},
    )

    assert response.status_code == 200
    body = response.json()
    conversation_id = body["conversation_id"]
    stored = client.get(f"/api/v1/conversations/{conversation_id}")
    assert stored.status_code == 200
    assert stored.json()["title"] == "Hello world"
    assert stored.json()["agent_id"] is None
    assert stored.json()["is_archived"] is False
    assert stored.json()["message_count"] == 2

    messages = client.get(f"/api/v1/conversations/{conversation_id}/messages")
    assert messages.status_code == 200
    payload = messages.json()
    assert payload["count"] == 2
    assert [item["role"] for item in payload["messages"]] == ["user", "assistant"]
    assert [item["sequence_number"] for item in payload["messages"]] == [1, 2]
    assert payload["messages"][0]["content"] == "  Hello\nworld  "
    assert payload["messages"][1]["content"] == "answer from settings-chat-model"
    assert payload["messages"][1]["model"] == "settings-chat-model"
    assert payload["messages"][0]["agent_id"] is None
    assert payload["messages"][1]["agent_id"] is None


def test_continue_conversation_sends_prior_turns_once(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    first = client.post("/api/v1/chat", json={"message": "First question", "system_prompt": "Be brief."})
    conversation_id = first.json()["conversation_id"]

    second = client.post(
        "/api/v1/chat",
        json={
            "message": "Second question",
            "conversation_id": conversation_id,
            "system_prompt": "Be brief.",
        },
    )

    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id
    assert second.json()["model"] == "settings-chat-model"
    assert ollama_chat[1]["history"] == [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "answer from settings-chat-model"},
        {"role": "user", "content": "Second question"},
    ]
    assert ollama_chat[1]["history"].count({"role": "user", "content": "Second question"}) == 1
    assert client.get(f"/api/v1/conversations/{conversation_id}").json()["title"] == "First question"
    history = client.get(f"/api/v1/conversations/{conversation_id}/messages").json()
    assert history["count"] == 4
    assert [item["sequence_number"] for item in history["messages"]] == [1, 2, 3, 4]


def test_title_is_limited_without_a_model_call(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    message = "A" * 81
    response = client.post("/api/v1/chat", json={"message": message})
    title = client.get(f"/api/v1/conversations/{response.json()['conversation_id']}").json()["title"]
    assert len(title) == 80
    assert title.endswith("…")
    assert title.startswith("A")
    assert ollama_chat[0]["history"] == [{"role": "user", "content": message}]


def test_agent_conversation_keeps_its_agent(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "")
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

    started = client.post(
        "/api/v1/chat",
        json={"message": "Find sources", "agent_id": research["id"], "system_prompt": "Ignore the agent."},
    )
    assert started.status_code == 200
    conversation_id = started.json()["conversation_id"]
    assert started.json()["agent_id"] == research["id"]
    assert started.json()["model"] == "research-model"
    assert ollama_chat[0]["system_prompt"] == "You are a research agent."

    conflict = client.post(
        "/api/v1/chat",
        json={"message": "Review code", "conversation_id": conversation_id, "agent_id": developer["id"]},
    )
    assert conflict.status_code == 409
    assert client.get(f"/api/v1/conversations/{conversation_id}/messages").json()["count"] == 2

    continued = client.post(
        "/api/v1/chat",
        json={"message": "What next?", "conversation_id": conversation_id},
    )
    assert continued.status_code == 200
    assert continued.json()["agent_id"] == research["id"]
    assert continued.json()["model"] == "research-model"
    assert ollama_chat[-1]["model"] == "research-model"

    default_chat = client.post("/api/v1/chat", json={"message": "Plain chat", "system_prompt": "Be brief."})
    assert default_chat.status_code == 400
    use_settings_model(monkeypatch, "settings-chat-model")
    default_chat = client.post("/api/v1/chat", json={"message": "Plain chat", "system_prompt": "Be brief."})
    plain_id = default_chat.json()["conversation_id"]
    attach = client.post(
        "/api/v1/chat",
        json={"message": "Use an agent now", "conversation_id": plain_id, "agent_id": research["id"]},
    )
    assert attach.status_code == 409
    assert client.get(f"/api/v1/conversations/{plain_id}").json()["agent_id"] is None


def test_conversation_pagination_rename_archive_and_delete(client, monkeypatch, ollama_chat, database_file):
    use_settings_model(monkeypatch, "settings-chat-model")
    created = []
    for index in range(3):
        response = client.post("/api/v1/chat", json={"message": f"Topic {index}"})
        assert response.status_code == 200
        created.append(response.json()["conversation_id"])

    page = client.get("/api/v1/conversations", params={"limit": 2, "offset": 0})
    assert page.status_code == 200
    body = page.json()
    assert body["count"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["conversations"]) == 2

    second_page = client.get("/api/v1/conversations", params={"limit": 2, "offset": 2})
    assert second_page.json()["count"] == 3
    assert len(second_page.json()["conversations"]) == 1
    assert client.get("/api/v1/conversations", params={"limit": 0}).status_code == 422

    target = created[0]
    renamed = client.patch(f"/api/v1/conversations/{target}", json={"title": "Renamed topic"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Renamed topic"
    assert renamed.json()["agent_id"] is None

    rejected = client.patch(f"/api/v1/conversations/{target}", json={"agent_id": str(uuid4())})
    assert rejected.status_code == 422

    archived = client.patch(f"/api/v1/conversations/{target}", json={"is_archived": True})
    assert archived.status_code == 200
    assert archived.json()["is_archived"] is True
    visible = client.get("/api/v1/conversations").json()
    assert target not in [item["id"] for item in visible["conversations"]]
    assert visible["count"] == 2
    included = client.get("/api/v1/conversations", params={"include_archived": True}).json()
    assert included["count"] == 3

    blocked = client.post("/api/v1/chat", json={"message": "Still there?", "conversation_id": target})
    assert blocked.status_code == 409
    assert client.get(f"/api/v1/conversations/{target}/messages").json()["count"] == 2

    restored = client.patch(f"/api/v1/conversations/{target}", json={"is_archived": False})
    assert restored.status_code == 200
    assert client.post("/api/v1/chat", json={"message": "Continue", "conversation_id": target}).status_code == 200

    deleted = client.delete(f"/api/v1/conversations/{target}")
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(f"/api/v1/conversations/{target}").status_code == 404
    assert client.get(f"/api/v1/conversations/{target}/messages").status_code == 404
    with open_sqlite(database_file) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            (target,),
        ).fetchone()[0]
    assert remaining == 0


def test_context_window_keeps_full_history(client, monkeypatch, ollama_chat):
    monkeypatch.setattr(
        "app.conversations.service.get_settings",
        lambda: get_settings().model_copy(
            update={"ollama_model": "settings-chat-model", "chat_context_message_limit": 2}
        ),
    )
    first = client.post("/api/v1/chat", json={"message": "One"})
    conversation_id = first.json()["conversation_id"]
    assert client.post("/api/v1/chat", json={"message": "Two", "conversation_id": conversation_id}).status_code == 200
    third = client.post("/api/v1/chat", json={"message": "Three", "conversation_id": conversation_id})
    assert third.status_code == 200
    assert ollama_chat[2]["history"] == [
        {"role": "assistant", "content": "answer from settings-chat-model"},
        {"role": "user", "content": "Three"},
    ]
    stored = client.get(f"/api/v1/conversations/{conversation_id}/messages").json()
    assert stored["count"] == 6
    assert stored["messages"][0]["content"] == "One"
    assert stored["messages"][-1]["role"] == "assistant"


def test_unknown_and_invalid_conversations(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    missing = client.post("/api/v1/chat", json={"message": "Hello", "conversation_id": str(uuid4())})
    invalid = client.post("/api/v1/chat", json={"message": "Hello", "conversation_id": "not-a-uuid"})
    assert missing.status_code == 404
    assert invalid.status_code == 422
    assert client.get(f"/api/v1/conversations/{uuid4()}").status_code == 404
    assert client.get("/api/v1/conversations/not-a-uuid").status_code == 422
    assert ollama_chat == []


def test_failed_inference_does_not_store_a_partial_turn(client, monkeypatch):
    calls = {"count": 0}

    from app.providers.ollama import OllamaError

    async def fail_second(self, model, message, system_prompt=None, history=None):
        calls["count"] += 1
        if message == "Second":
            raise OllamaError("connection refused")
        return "ok"

    monkeypatch.setattr(OllamaProvider, "chat", fail_second)
    use_settings_model(monkeypatch, "settings-chat-model")
    first = client.post("/api/v1/chat", json={"message": "First"})
    conversation_id = first.json()["conversation_id"]
    failed = client.post("/api/v1/chat", json={"message": "Second", "conversation_id": conversation_id})
    assert failed.status_code == 502
    assert failed.json()["detail"] == "connection refused"
    messages = client.get(f"/api/v1/conversations/{conversation_id}/messages").json()
    assert messages["count"] == 2
    assert [item["content"] for item in messages["messages"]] == ["First", "ok"]
    assert calls["count"] == 2


def test_ollama_payload_includes_history_once(monkeypatch):
    captured = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "ok"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            captured.append(json)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr("app.providers.ollama.httpx.AsyncClient", FakeClient)

    import asyncio

    provider = OllamaProvider()
    asyncio.run(
        provider.chat(
            model="settings-chat-model",
            message="Now",
            system_prompt="Be brief.",
            history=[
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Now"},
            ],
        )
    )
    assert captured[0]["model"] == "settings-chat-model"
    assert captured[0]["stream"] is False
    assert captured[0]["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Earlier"},
        {"role": "assistant", "content": "Reply"},
        {"role": "user", "content": "Now"},
    ]
    assert [item["content"] for item in captured[0]["messages"]].count("Now") == 1


def test_migration_from_v1_preserves_agents(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    agent_id = "11111111-1111-4111-8111-111111111111"
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
            "INSERT INTO schema_version (version, applied_at) VALUES (1, ?)",
            ("2026-01-01T00:00:00",),
        )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()
    reset_database()
    try:
        init_database()
        with LocalClient(app) as client:
            response = client.get(f"/api/v1/agents/{agent_id}")
            assert response.status_code == 200
            assert response.json()["name"] == "Research Agent"
            assert response.json()["model"] == "qwen3.5:9b"
            assert response.json()["system_prompt"] == "Keep this prompt."
        init_database()
        with open_sqlite(path) as connection:
            versions = connection.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
            agent = connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            messages = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            memories = connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            pending = connection.execute("SELECT COUNT(*) FROM pending_memory_actions").fetchone()[0]
            grants = connection.execute("SELECT COUNT(*) FROM agent_tool_grants").fetchone()[0]
            workspaces = connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
            workspace_grants = connection.execute("SELECT COUNT(*) FROM agent_workspace_grants").fetchone()[0]
        assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,), (17,), (18,), (19,)]
        assert agent == 1
        assert {
            "conversations",
            "messages",
            "agents",
            "memories",
            "pending_memory_actions",
            "agent_tool_grants",
            "workspaces",
            "agent_workspace_grants",
            "permission_policy",
            "tool_settings",
            "agent_path_grants",
            "capability_preferences",
            "permission_audit",
            "approval_requests",
            "file_recoveries",
            "file_operation_journal",
        }.issubset(tables)
        assert messages == 0
        assert memories == 0
        assert pending == 0
        assert grants == 0
        assert workspaces == 0
        assert workspace_grants == 0
    finally:
        reset_database()
        get_settings.cache_clear()
