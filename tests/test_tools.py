import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.agents.repository import AgentRepository
from app.agents.service import AgentService
from app.core.config import get_settings
from app.core.database import get_engine, init_database, reset_database
from app.main import app
from app.memories.context import MEMORY_REFERENCE_NOTICE
from app.providers.ollama import OllamaProvider
from app.files.tools import FILE_TOOL_NAMES
from app.knowledge.identity import set_embedding_client
from app.knowledge.tools import KNOWLEDGE_TOOL_NAMES
from app.tools.base import BLOCKING_READ_ONLY, SIDE_EFFECT, Tool
from app.tools.calculator import evaluate
from app.tools.exceptions import (
    DuplicateToolError,
    SearchProviderError,
    ToolDisabledError,
    ToolExecutionError,
    ToolPermissionError,
    ToolValidationError,
)
from app.tools.executor import ToolExecutor
from app.tools.grants import AgentToolGrantRepository
from app.tools.parsing import parse_tool_request
from app.tools.registry import ToolRegistry, get_tool_registry
from app.tools.service import ToolService
from app.tools.web_search import DuckDuckGoSearchProvider, SearchHit, WebSearchTool, parse_duckduckgo
from tests.db_support import open_sqlite
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_agents import create_agent
from tests.test_chat import ollama_chat, use_settings_model
from tests.test_memories import create_memory

_STOP = "I could not complete that request with the available tools."


@pytest.fixture(autouse=True)
def reset_builtin_tools():
    _restore_builtins()
    yield
    _restore_builtins()


def _restore_builtins() -> None:
    registry = get_tool_registry()
    calculator = registry.get("calculator")
    clock = registry.get("datetime")
    search = registry.get("web_search")
    research = registry.get("research")
    if calculator is not None:
        calculator.calls = 0
        calculator.enabled = True
    if clock is not None:
        clock.calls = 0
        clock._now = datetime.now
    if search is not None:
        search.calls = 0
        search.enabled = True
        search.provider = DuckDuckGoSearchProvider()
    if research is not None:
        research.calls = 0
        research.enabled = True
        research.service = None
    for name in FILE_TOOL_NAMES:
        tool = registry.get(name)
        if tool is not None:
            tool.enabled = True
            tool.ran_on_loop = False
            tool.timeout_seconds = 15.0
    for name in KNOWLEDGE_TOOL_NAMES:
        tool = registry.get(name)
        if tool is not None:
            tool.calls = 0
            tool.service = None
    set_embedding_client(None)
    for name in [tool.name for tool in registry.list_tools()]:
        if name not in {
            "calculator",
            "datetime",
            "web_search",
            "research",
            *FILE_TOOL_NAMES,
            *KNOWLEDGE_TOOL_NAMES,
            "list_mcp_tools",
            "list_mcp_resources",
            "call_mcp_tool",
            "read_mcp_resource",
        }:
            registry.unregister(name)


def grant(client, agent_id: str, names: list[str]) -> dict:
    response = client.put(f"/api/v1/agents/{agent_id}/tools", json={"allowed_tools": names})
    assert response.status_code == 200, response.text
    return response.json()


def scripted_chat(monkeypatch, responder):
    calls = []

    async def chat(self, model, message, system_prompt=None, history=None):
        item = {
            "model": model,
            "message": message,
            "system_prompt": system_prompt,
            "history": None if history is None else [dict(entry) for entry in history],
        }
        calls.append(item)
        return responder(item, calls)

    monkeypatch.setattr(OllamaProvider, "chat", chat)
    return calls


def _result_texts(history: list[dict]) -> list[str]:
    return [item["content"] for item in history if str(item["content"]).startswith("Tool result:")]


def tool_call(name: str, arguments: dict, **extra) -> str:
    call = {"name": name, "arguments": arguments, **extra}
    return json.dumps({"tool_calls": [call]})


class SideEffectTool(Tool):
    name = "side_effect_probe"
    description = "Changes state and must be confirmed before it runs."
    category = "test"
    requires_confirmation = True
    timeout_seconds = 2.0
    enabled = True
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, arguments: dict) -> dict:
        self.calls += 1
        return {"changed": True}


class SlowTool(Tool):
    name = "slow_probe"
    description = "Sleeps longer than its timeout."
    category = "test"
    requires_confirmation = False
    timeout_seconds = 0.05
    enabled = True
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }

    def __init__(self) -> None:
        self.calls = 0
        self.finished = False

    async def run(self, arguments: dict) -> dict:
        self.calls += 1
        await asyncio.sleep(1)
        self.finished = True
        return {"done": True}


def test_registry_lists_builtins_and_rejects_duplicates():
    fresh = ToolRegistry()
    fresh.register(get_tool_registry().get("calculator"))
    with pytest.raises(DuplicateToolError):
        fresh.register(get_tool_registry().get("calculator"))
    assert fresh.get("missing") is None
    with pytest.raises(DuplicateToolError):
        get_tool_registry().register(get_tool_registry().get("calculator"))
    names = {tool.name for tool in get_tool_registry().list_tools()}
    assert names == {
        "calculator",
        "datetime",
        "web_search",
        "research",
        *FILE_TOOL_NAMES,
        *KNOWLEDGE_TOOL_NAMES,
        "list_mcp_tools",
        "list_mcp_resources",
        "call_mcp_tool",
        "read_mcp_resource",
    }
    assert get_tool_registry().get("shell") is None


def test_calculator_accepts_arithmetic_and_rejects_code():
    source = Path("app/tools/calculator.py").read_text(encoding="utf-8")
    assert "eval(" not in source
    assert "exec(" not in source
    assert evaluate("(2+3)*4") == 20
    assert evaluate("2*(3+4)") == 14
    assert evaluate("10 / 2") == 5
    assert evaluate("1/2") == 0.5
    assert evaluate("-3 + 5") == 2
    with pytest.raises(ToolValidationError):
        evaluate("__import__('os')")
    with pytest.raises(ToolValidationError):
        evaluate("2/0")
    with pytest.raises(ToolValidationError):
        evaluate("2+")
    with pytest.raises(ToolValidationError):
        evaluate("")


@pytest.mark.asyncio
async def test_datetime_uses_configured_timezone(monkeypatch):
    monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
    get_settings.cache_clear()
    clock = get_tool_registry().get("datetime")
    clock._now = lambda tz: datetime(2026, 6, 15, 23, 30, tzinfo=tz)

    current = await clock.run({"operation": "now"})
    shifted = await clock.run({"operation": "add", "hours": 2})
    formatted = await clock.run({"operation": "format", "format": "%Y-%m-%d"})

    assert current["timezone"] == "America/New_York"
    assert current["date"] == "2026-06-15"
    assert "-04:00" in current["iso"]
    assert shifted["date"] == "2026-06-16"
    assert shifted["time"] == "01:30:00"
    assert formatted["formatted"] == "2026-06-15"


@pytest.mark.asyncio
async def test_datetime_reports_invalid_timezone(monkeypatch):
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Not/AZone")
    get_settings.cache_clear()
    clock = get_tool_registry().get("datetime")
    with pytest.raises(ToolExecutionError) as caught:
        await clock.run({"operation": "now"})
    assert str(caught.value) == "The configured timezone is not available."
    assert "ZoneInfo" not in str(caught.value)


def test_duckduckgo_parser_shapes_public_results_and_skips_unsafe_urls():
    payload = {
        "Heading": "Example",
        "AbstractText": "An example page.",
        "AbstractURL": "https://example.com/a",
        "AbstractSource": "Example Source",
        "RelatedTopics": [
            {"Text": "More - A related page", "FirstURL": "https://example.com/more"},
            {"Text": "Bad", "FirstURL": "javascript:alert(1)"},
            {
                "Name": "Group",
                "Topics": [{"Text": "Nested - Inside", "FirstURL": "https://example.org/nested"}],
            },
        ],
    }
    hits = parse_duckduckgo(payload, limit=5)
    assert [hit.url for hit in hits] == [
        "https://example.com/a",
        "https://example.com/more",
        "https://example.org/nested",
    ]
    assert hits[0].title == "Example"
    assert hits[0].source == "Example Source"
    assert hits[0].published_at is None
    assert hits[1].title == "More"
    assert hits[1].source == "example.com"
    assert len(parse_duckduckgo(payload, limit=1)) == 1


@pytest.mark.asyncio
async def test_duckduckgo_provider_success_and_failure(monkeypatch):
    seen = []
    payload = {
        "Heading": "Local models",
        "AbstractText": "A public summary.",
        "AbstractURL": "https://example.com/models",
        "AbstractSource": "Example",
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class Client:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs.get("timeout"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            seen.append((url, params))
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    hits = await DuckDuckGoSearchProvider().search("local models", limit=3, timeout=2)
    assert hits[0].url == "https://example.com/models"
    assert seen[1][0] == "https://api.duckduckgo.com/"
    assert seen[1][1]["q"] == "local models"
    assert len([item for item in seen if isinstance(item, tuple)]) == 1

    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", FailingClient)
    with pytest.raises(SearchProviderError) as caught:
        await DuckDuckGoSearchProvider().search("local models", limit=3, timeout=2)
    assert "connection refused" not in str(caught.value)

    tool = WebSearchTool(DuckDuckGoSearchProvider())
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    get_settings.cache_clear()
    with pytest.raises(ToolExecutionError) as failed:
        await tool.run({"query": "local models"})
    assert str(failed.value) == "Web search did not return results."
    assert "connection refused" not in str(failed.value)


def test_list_and_get_tools(client):
    listed = client.get("/api/v1/tools")
    assert listed.status_code == 200
    body = listed.json()
    assert {item["name"] for item in body["tools"]} == {
        "calculator",
        "datetime",
        "web_search",
        "research",
        *FILE_TOOL_NAMES,
        *KNOWLEDGE_TOOL_NAMES,
        "list_mcp_tools",
        "list_mcp_resources",
        "call_mcp_tool",
        "read_mcp_resource",
    }
    search = client.get("/api/v1/tools/web_search")
    assert search.status_code == 200
    assert search.json()["enabled"] is False
    assert "network" in search.json()["description"].lower()
    assert search.json()["requires_confirmation"] is False
    assert client.get("/api/v1/tools/shell").status_code == 404


def test_tool_test_validates_permissions_and_arguments(client):
    denied = client.post("/api/v1/tools/calculator/test", json={"arguments": {"expression": "1+1"}})
    assert denied.status_code == 403
    assert get_tool_registry().get("calculator").calls == 0

    agent = create_agent(client, system_prompt="Be exact.")
    granted = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "(2+3)*4"}, "agent_id": agent["id"]},
    )
    assert granted.status_code == 403

    grant(client, agent["id"], ["calculator"])
    granted = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "(2+3)*4"}, "agent_id": agent["id"]},
    )
    assert granted.status_code == 200
    assert granted.json()["status"] == "ok"
    assert granted.json()["output"]["value"] == 20

    invalid = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "__import__('os')"}, "agent_id": agent["id"]},
    )
    assert invalid.status_code == 422
    assert "not supported" in invalid.json()["detail"]

    divided = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "2/0"}, "agent_id": agent["id"]},
    )
    assert divided.status_code == 422
    assert "Division by zero" in divided.json()["detail"]

    extra = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "1+1", "confirmed": True}, "agent_id": agent["id"]},
    )
    assert extra.status_code == 422
    bypass = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "1+1"}, "agent_id": agent["id"], "confirmed": True},
    )
    assert bypass.status_code == 422


def test_disabled_agent_and_disabled_tool_cannot_run(client):
    agent = create_agent(client)
    grant(client, agent["id"], ["calculator"])
    assert client.patch(f"/api/v1/agents/{agent['id']}", json={"enabled": False}).status_code == 200
    response = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "1+1"}, "agent_id": agent["id"]},
    )
    assert response.status_code == 409
    assert get_tool_registry().get("calculator").calls == 0

    other = create_agent(client, name="Writer Agent", role="Writing.", system_prompt="Write.", model="writer")
    grant(client, other["id"], ["calculator"])
    get_tool_registry().get("calculator").enabled = False
    disabled = client.post(
        "/api/v1/tools/calculator/test",
        json={"arguments": {"expression": "1+1"}, "agent_id": other["id"]},
    )
    assert disabled.status_code == 409
    assert get_tool_registry().get("calculator").calls == 0


def test_web_search_api_success_failure_and_disabled(client, monkeypatch):
    agent = create_agent(client)
    grant(client, agent["id"], ["web_search"])
    blocked = client.post(
        "/api/v1/tools/web_search/test",
        json={"arguments": {"query": "local models"}, "agent_id": agent["id"]},
    )
    assert blocked.status_code == 409
    assert get_tool_registry().get("web_search").calls == 0

    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    get_settings.cache_clear()

    class FailingProvider:
        name = "duckduckgo"

        async def search(self, query: str, *, limit: int, timeout: float):
            raise SearchProviderError()

    search = get_tool_registry().get("web_search")
    search.provider = FailingProvider()
    failed = client.post(
        "/api/v1/tools/web_search/test",
        json={"arguments": {"query": "local models"}, "agent_id": agent["id"]},
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "error"
    assert failed.json().get("output") is None
    assert failed.json()["error"] == "Web search did not return results."
    assert "results" not in failed.json()

    class FakeProvider:
        name = "duckduckgo"

        async def search(self, query: str, *, limit: int, timeout: float):
            assert limit == 5
            return [
                SearchHit(
                    title="Local models",
                    url="https://example.com/models",
                    snippet="A public summary.",
                    source="example.com",
                    published_at=None,
                )
            ]

    search.provider = FakeProvider()
    succeeded = client.post(
        "/api/v1/tools/web_search/test",
        json={"arguments": {"query": "local models"}, "agent_id": agent["id"]},
    )
    assert succeeded.status_code == 200
    body = succeeded.json()
    assert body["status"] == "ok"
    assert body["output"]["network"] == "external"
    assert body["output"]["provider"] == "duckduckgo"
    assert body["output"]["results"][0]["published_at"] is None
    assert body["output"]["results"][0]["url"] == "https://example.com/models"


@pytest.mark.asyncio
async def test_timeout_and_confirmation_gate():
    slow = SlowTool()
    started = time.monotonic()
    outcome = await ToolExecutor().run(slow, {}, confirmed=False)
    assert outcome.status == "timeout"
    assert outcome.output is None
    assert slow.finished is False
    assert time.monotonic() - started < 0.8

    probe = SideEffectTool()
    blocked = await ToolExecutor().run(probe, {}, confirmed=False)
    assert blocked.status == "confirmation_required"
    assert probe.calls == 0
    allowed = await ToolExecutor().run(probe, {}, confirmed=True)
    assert allowed.status == "error"
    assert allowed.error == "This tool cannot be isolated, so it was not run."
    assert probe.calls == 0
    assert ToolExecutor().reaped_workers == []


def test_confirmation_is_required_for_model_and_api(client, monkeypatch):
    probe = SideEffectTool()
    get_tool_registry().register(probe)
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    other = create_agent(
        client,
        name="Other Agent",
        role="Other.",
        system_prompt="Other.",
        model="other-model",
    )
    grant(client, agent["id"], ["side_effect_probe"])

    def respond(item, calls):
        if len(calls) == 1:
            return tool_call(
                "side_effect_probe",
                {},
                confirmed=True,
                agent_id=other["id"],
            )
        return "I need confirmation before changing anything."

    calls = scripted_chat(monkeypatch, respond)
    response = client.post(
        "/api/v1/chat",
        json={"message": "Change the record.", "agent_id": agent["id"]},
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "I need confirmation before changing anything."
    assert probe.calls == 0
    assert "confirmation" in calls[1]["history"][-1]["content"]
    direct = client.post(
        "/api/v1/tools/side_effect_probe/test",
        json={"arguments": {}, "agent_id": agent["id"]},
    )
    assert direct.status_code == 200
    assert direct.json()["status"] == "confirmation_required"
    assert probe.calls == 0


def test_agent_permissions_reject_unknown_and_ungranted_tools(client, monkeypatch):
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    privileged = create_agent(
        client,
        name="Privileged Agent",
        role="Privileged.",
        system_prompt="Privileged.",
        model="priv-model",
    )
    grant(client, agent["id"], ["datetime"])
    grant(client, privileged["id"], ["calculator"])

    def respond(item, calls):
        if "Do not call any tools" in (item["system_prompt"] or ""):
            return "Done."
        if len(calls) == 1:
            return tool_call("calculator", {"expression": "2+2"}, agent_id=privileged["id"], confirmed=True)
        if len(calls) == 2:
            return tool_call("shell", {"command": "ls"})
        return "No tools ran."

    calls = scripted_chat(monkeypatch, respond)
    response = client.post("/api/v1/chat", json={"message": "Calculate 2 + 2.", "agent_id": agent["id"]})
    assert response.status_code == 200
    assert response.json()["answer"] == "No tools ran."
    assert get_tool_registry().get("calculator").calls == 0
    assert "not allowed" in calls[1]["history"][-1]["content"]
    assert "not available" in calls[2]["history"][-1]["content"]
    assert calls[0]["system_prompt"].startswith("Be exact.")
    assert "calculator" not in calls[0]["system_prompt"]


def test_malformed_tool_call_does_not_execute(client, monkeypatch):
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    grant(client, agent["id"], ["calculator"])

    def respond(item, calls):
        if len(calls) == 1:
            return '{"tool_calls": "nope"}'
        return "The calculator was not used."

    calls = scripted_chat(monkeypatch, respond)
    response = client.post("/api/v1/chat", json={"message": "What is 2 + 2?", "agent_id": agent["id"]})
    assert response.status_code == 200
    assert response.json()["answer"] == "The calculator was not used."
    assert get_tool_registry().get("calculator").calls == 0
    assert "not valid" in calls[1]["history"][-1]["content"]
    assert len(calls) == 2

    def respond_broken(item, calls):
        return '{"tool_calls":'

    scripted_chat(monkeypatch, respond_broken)
    broken = client.post("/api/v1/chat", json={"message": "What is 2 + 2?", "agent_id": agent["id"]})
    assert broken.status_code == 200
    assert broken.json()["answer"] == '{"tool_calls":'
    assert get_tool_registry().get("calculator").calls == 0


def test_sequential_tool_calls_and_no_memory_writes(client, monkeypatch, database_file):
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    grant(client, agent["id"], ["calculator", "datetime"])
    create_memory(client)

    def respond(item, calls):
        if len(calls) == 1:
            return tool_call("calculator", {"expression": "2+2"})
        if len(calls) == 2:
            return tool_call("datetime", {"operation": "now"})
        return "Remember that my favorite color is blue."

    calls = scripted_chat(monkeypatch, respond)
    response = client.post(
        "/api/v1/chat",
        json={"message": "What is 2 + 2, and what time is it?", "agent_id": agent["id"]},
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "Remember that my favorite color is blue."
    results = [item["content"] for item in calls[2]["history"] if item["content"].startswith("Tool result:")]
    assert len(results) == 2
    assert '"value": 4' in results[0]
    assert '"tool": "datetime"' in results[1]
    assert '"status": "ok"' in results[1]
    assert get_tool_registry().get("calculator").calls == 0
    assert get_tool_registry().get("datetime").calls == 0
    assert client.get("/api/v1/memories").json()["count"] == 1
    with open_sqlite(database_file) as connection:
        pending = connection.execute("SELECT COUNT(*) FROM pending_memory_actions").fetchone()[0]
        memories = connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert memories == 1
    assert pending == 0
    grants = client.get(f"/api/v1/agents/{agent['id']}/tools").json()
    assert grants["allowed_tools"] == ["calculator", "datetime"]


def test_max_calls_and_repeated_calls_stop(client, monkeypatch):
    monkeypatch.setenv("TOOL_MAX_CALLS_PER_TURN", "1")
    get_settings.cache_clear()
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    grant(client, agent["id"], ["calculator"])
    expressions = iter(["1+1", "2+2", "3+3"])

    def respond(item, calls):
        if "Do not call any tools" in (item["system_prompt"] or ""):
            return tool_call("calculator", {"expression": "9+9"})
        return tool_call("calculator", {"expression": next(expressions)})

    calls = scripted_chat(monkeypatch, respond)
    response = client.post("/api/v1/chat", json={"message": "Add some numbers.", "agent_id": agent["id"]})
    assert response.status_code == 200
    assert response.json()["answer"] == _STOP
    first_results = _result_texts(calls[-1]["history"])
    assert len(first_results) == 1
    assert '"value": 2' in first_results[0]
    assert get_tool_registry().get("calculator").calls == 0

    monkeypatch.setenv("TOOL_MAX_CALLS_PER_TURN", "3")
    get_settings.cache_clear()

    def repeat(item, calls):
        if "Do not call any tools" in (item["system_prompt"] or ""):
            return "stopped"
        return tool_call("calculator", {"expression": "2+2"})

    repeat_calls = scripted_chat(monkeypatch, repeat)
    repeated = client.post("/api/v1/chat", json={"message": "Add two and two again.", "agent_id": agent["id"]})
    assert repeated.status_code == 200
    assert repeated.json()["answer"] == "stopped"
    repeated_results = _result_texts(repeat_calls[-1]["history"])
    assert len(repeated_results) == 1
    assert '"value": 4' in repeated_results[0]
    assert get_tool_registry().get("calculator").calls == 0


def test_disabled_tool_is_rejected_during_a_turn(client, monkeypatch):
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    grant(client, agent["id"], ["calculator", "datetime"])
    get_tool_registry().get("calculator").enabled = False

    def respond(item, calls):
        if len(calls) == 1:
            return tool_call("calculator", {"expression": "2+2"})
        return "I could not calculate that."

    calls = scripted_chat(monkeypatch, respond)
    response = client.post("/api/v1/chat", json={"message": "What is 2 + 2?", "agent_id": agent["id"]})
    assert response.status_code == 200
    assert get_tool_registry().get("calculator").calls == 0
    assert "turned off" in calls[1]["history"][-1]["content"]
    assert "calculator" not in calls[0]["system_prompt"]


def test_agent_without_grants_keeps_the_original_prompt(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "settings-chat-model")
    agent = create_agent(client, system_prompt="Be exact.")
    response = client.post("/api/v1/chat", json={"message": "Hello", "agent_id": agent["id"]})
    assert response.status_code == 200
    assert ollama_chat == [
        {
            "model": agent["model"],
            "message": "Hello",
            "system_prompt": "Be exact.",
            "history": [{"role": "user", "content": "Hello"}],
        }
    ]


def test_default_assistant_tools_are_configurable(client, monkeypatch):
    monkeypatch.setenv("DEFAULT_ASSISTANT_TOOLS", "calculator")
    get_settings.cache_clear()
    use_settings_model(monkeypatch, "tool-model")

    def respond(item, calls):
        if len(calls) == 1:
            return "```json\n" + tool_call("calculator", {"expression": "2+2"}) + "\n```"
        return "The answer is 4."

    calls = scripted_chat(monkeypatch, respond)
    response = client.post(
        "/api/v1/chat",
        json={"message": "What is 2 + 2?", "system_prompt": "Be brief."},
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "The answer is 4."
    assert calls[0]["system_prompt"].startswith("Be brief.")
    assert "Available tools:" in calls[0]["system_prompt"]
    assert '"value": 4' in calls[1]["history"][-1]["content"]
    assert get_tool_registry().get("calculator").calls == 0


def test_tool_results_do_not_replace_memory_or_routing(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "route-model")
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["calculator"])
    create_memory(client)
    response = client.post(
        "/api/v1/chat",
        json={"message": "What is my answer style?", "agent_id": agent["id"]},
    )
    assert response.status_code == 200
    prompt = ollama_chat[0]["system_prompt"]
    assert prompt.startswith("Be exact.")
    assert MEMORY_REFERENCE_NOTICE in prompt
    assert "I prefer concise answers." in prompt
    assert prompt.index(MEMORY_REFERENCE_NOTICE) < prompt.index("Available tools:")
    assert "You are the local agent router." not in prompt

    routed = client.post("/api/v1/chat", json={"message": "Compare the last two quarterly reports."})
    assert routed.status_code == 200
    assert ollama_chat[1]["system_prompt"].startswith("You are the local agent router.")
    assert "Available tools:" not in ollama_chat[1]["system_prompt"]
    assert get_tool_registry().get("calculator").calls == 0


def test_memory_commands_still_run_before_tools(client, monkeypatch, ollama_chat):
    use_settings_model(monkeypatch, "tool-model")
    agent = create_agent(client)
    grant(client, agent["id"], ["calculator"])
    response = client.post(
        "/api/v1/chat",
        json={"message": "Remember that I prefer tea.", "agent_id": agent["id"]},
    )
    assert response.status_code == 200
    assert response.json()["confirmation_required"] is True
    assert ollama_chat == []
    assert get_tool_registry().get("calculator").calls == 0
    assert client.get("/api/v1/memories").json()["count"] == 0


def test_create_and_replace_grants_without_changing_agent_read(client):
    rejected = client.post(
        "/api/v1/agents",
        json={
            "name": "Bad Tools",
            "description": "Tries to grant a missing tool.",
            "role": "Testing.",
            "system_prompt": "Test.",
            "model": "tool-model",
            "allowed_tools": ["shell"],
        },
    )
    assert rejected.status_code == 422
    assert client.get("/api/v1/agents").json()["count"] == 0

    agent = create_agent(client, allowed_tools=["calculator", "calculator"])
    assert "allowed_tools" not in agent
    listed = client.get(f"/api/v1/agents/{agent['id']}/tools").json()
    assert listed["allowed_tools"] == ["calculator"]
    cleared = client.put(f"/api/v1/agents/{agent['id']}/tools", json={"allowed_tools": []})
    assert cleared.status_code == 200
    assert cleared.json()["allowed_tools"] == []
    unknown = client.put(f"/api/v1/agents/{agent['id']}/tools", json={"allowed_tools": ["shell"]})
    assert unknown.status_code == 422


def test_schema_v5_adds_grants_without_dropping_agent_rows(tmp_path, monkeypatch):
    path = tmp_path / "v4.db"
    agent_id = str(uuid4())
    with open_sqlite(path) as connection:
        connection.execute(
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
            )
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
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (4, ?)",
            ("2026-01-01T00:00:00",),
        )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()
    reset_database()
    try:
        init_database()
        with TestClient(app) as client:
            response = client.get(f"/api/v1/agents/{agent_id}")
            assert response.status_code == 200
            assert response.json()["name"] == "Research Agent"
            assert response.json()["system_prompt"] == "Keep this prompt."
            assert "allowed_tools" not in response.json()
        with open_sqlite(path) as connection:
            versions = connection.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ).fetchall()
            columns = [row[1] for row in connection.execute("PRAGMA table_info(agents)")]
            grants = connection.execute("SELECT COUNT(*) FROM agent_tool_grants").fetchone()[0]
            agents = connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        assert versions == [(4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,)]
        assert agents == 1
        assert grants == 0
        assert "system_prompt" in columns
        assert "allowed_tools" not in columns
    finally:
        reset_database()
        get_settings.cache_clear()


_PROBE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["marker", "delay"],
    "properties": {
        "marker": {"type": "string", "minLength": 1, "maxLength": 400},
        "delay": {"type": "number", "minimum": 0, "maximum": 2},
        "started": {"type": "string", "minLength": 1, "maxLength": 400},
    },
}


class BlockingMarkerTool(Tool):
    name = "blocking_marker"
    description = "Blocking read-only probe. Its only write happens after the delay."
    category = "test"
    requires_confirmation = False
    execution_policy = BLOCKING_READ_ONLY
    isolation_key = "blocking_probe"
    timeout_seconds = 2.0
    enabled = True
    input_schema = _PROBE_SCHEMA

    def __init__(self) -> None:
        self.ran_on_loop = False

    async def run(self, arguments: dict) -> dict:
        self.ran_on_loop = True
        return {"ran_on_loop": True}


class IsolatedSideEffectTool(BlockingMarkerTool):
    name = "isolated_side_effect"
    description = "A side effect that is allowed to run only in a killable process."
    requires_confirmation = True
    execution_policy = SIDE_EFFECT


class UnisolatedBlockingTool(Tool):
    name = "unisolated_blocker"
    description = "Blocking work with no killable process."
    category = "test"
    requires_confirmation = False
    execution_policy = BLOCKING_READ_ONLY
    isolation_key = None
    timeout_seconds = 1.0
    enabled = True
    input_schema = {"type": "object", "additionalProperties": False, "properties": {}, "required": []}

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, arguments: dict) -> dict:
        self.calls += 1
        return {"ran": True}


class PauseTool(Tool):
    name = "pause_probe"
    description = "A short async read-only pause."
    category = "test"
    requires_confirmation = False
    timeout_seconds = 2.0
    enabled = True
    input_schema = {"type": "object", "additionalProperties": False, "properties": {}, "required": []}

    def __init__(self) -> None:
        self.finished = False

    async def run(self, arguments: dict) -> dict:
        await asyncio.sleep(0.05)
        self.finished = True
        return {"done": True}


def _probe_args(marker: Path, delay: float, started: Path | None = None) -> dict:
    payload = {"marker": str(marker), "delay": delay}
    if started is not None:
        payload["started"] = str(started)
    return payload


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path.name} was not created")


def _autocommit_session() -> Session:
    return Session(get_engine().execution_options(isolation_level="AUTOCOMMIT"))


@pytest.mark.asyncio
async def test_blocking_timeout_kills_the_worker_before_its_write(tmp_path):
    tool = BlockingMarkerTool()
    tool.timeout_seconds = 0.25
    marker = tmp_path / "late.txt"
    executor = ToolExecutor()
    started = time.monotonic()
    outcome = await executor.run(tool, _probe_args(marker, 1.5), confirmed=False)
    assert outcome.status == "timeout"
    assert outcome.output is None
    assert time.monotonic() - started < 1.0
    assert not marker.exists()
    assert tool.ran_on_loop is False
    record = executor.reaped_workers[-1]
    assert record.returncode == -signal.SIGKILL
    assert not _alive(record.pid)


@pytest.mark.asyncio
async def test_successful_worker_is_reaped(tmp_path):
    tool = BlockingMarkerTool()
    marker = tmp_path / "done.txt"
    executor = ToolExecutor()
    outcome = await executor.run(tool, _probe_args(marker, 0.05), confirmed=False)
    assert outcome.status == "ok"
    assert outcome.output == {"wrote": True}
    assert marker.read_text(encoding="utf-8") == "done"
    assert tool.ran_on_loop is False
    record = executor.reaped_workers[-1]
    assert record.returncode == 0
    assert not _alive(record.pid)


@pytest.mark.asyncio
async def test_blocking_tool_does_not_stall_the_event_loop(tmp_path):
    tool = BlockingMarkerTool()
    executor = ToolExecutor()
    tick_at = None

    async def tick() -> None:
        nonlocal tick_at
        await asyncio.sleep(0.05)
        tick_at = time.monotonic()

    tick_task = asyncio.create_task(tick())
    finished_at = None
    outcome = await executor.run(tool, _probe_args(tmp_path / "loop.txt", 0.35), confirmed=False)
    finished_at = time.monotonic()
    await tick_task
    assert outcome.status == "ok"
    assert tick_at is not None
    assert finished_at is not None
    assert tick_at < finished_at - 0.05


@pytest.mark.asyncio
async def test_concurrent_isolated_and_async_execution(tmp_path):
    blocking = BlockingMarkerTool()
    pause = PauseTool()
    executor = ToolExecutor()
    times: dict[str, float] = {}

    async def run_blocking():
        result = await executor.run(blocking, _probe_args(tmp_path / "both.txt", 0.3), confirmed=False)
        times["blocking"] = time.monotonic()
        return result

    async def run_pause():
        result = await executor.run(pause, {}, confirmed=False)
        times["pause"] = time.monotonic()
        return result

    blocked, paused = await asyncio.gather(run_blocking(), run_pause())
    assert blocked.status == "ok"
    assert paused.status == "ok"
    assert pause.finished is True
    assert blocking.ran_on_loop is False
    assert times["pause"] < times["blocking"] - 0.05


@pytest.mark.asyncio
async def test_concurrent_timeouts_reap_both_workers(tmp_path):
    first = BlockingMarkerTool()
    second = BlockingMarkerTool()
    first.timeout_seconds = 0.25
    second.timeout_seconds = 0.25
    executor = ToolExecutor()
    started = time.monotonic()
    left, right = await asyncio.gather(
        executor.run(first, _probe_args(tmp_path / "left.txt", 1.5), confirmed=False),
        executor.run(second, _probe_args(tmp_path / "right.txt", 1.5), confirmed=False),
    )
    assert time.monotonic() - started < 1.0
    assert left.status == "timeout"
    assert right.status == "timeout"
    assert not (tmp_path / "left.txt").exists()
    assert not (tmp_path / "right.txt").exists()
    assert len(executor.reaped_workers) == 2
    assert {record.returncode for record in executor.reaped_workers} == {-signal.SIGKILL}
    assert all(not _alive(record.pid) for record in executor.reaped_workers)


@pytest.mark.asyncio
async def test_cancellation_kills_the_isolated_worker(tmp_path):
    tool = BlockingMarkerTool()
    marker = tmp_path / "cancelled.txt"
    started = tmp_path / "cancelled-started.txt"
    executor = ToolExecutor()
    task = asyncio.create_task(
        executor.run(tool, _probe_args(marker, 1.5, started), confirmed=False)
    )
    await _wait_for_file(started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not marker.exists()
    assert tool.ran_on_loop is False
    assert executor.reaped_workers
    assert not _alive(executor.reaped_workers[-1].pid)


@pytest.mark.asyncio
async def test_async_cancellation_stops_at_an_await():
    slow = SlowTool()
    slow.timeout_seconds = 2.0
    task = asyncio.create_task(ToolExecutor().run(slow, {}, confirmed=False))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slow.calls == 1
    assert slow.finished is False


@pytest.mark.asyncio
async def test_unisolated_blocking_tool_fails_closed():
    tool = UnisolatedBlockingTool()
    executor = ToolExecutor()
    outcome = await executor.run(tool, {}, confirmed=False)
    assert outcome.status == "error"
    assert outcome.error == "This tool cannot be isolated, so it was not run."
    assert tool.calls == 0
    assert executor.reaped_workers == []


@pytest.mark.asyncio
async def test_side_effect_runs_only_when_confirmed_and_isolated(tmp_path):
    tool = IsolatedSideEffectTool()
    executor = ToolExecutor()
    blocked_marker = tmp_path / "blocked.txt"
    blocked = await executor.run(tool, _probe_args(blocked_marker, 0.05), confirmed=False)
    assert blocked.status == "confirmation_required"
    assert executor.reaped_workers == []
    assert not blocked_marker.exists()
    assert tool.ran_on_loop is False

    completed_marker = tmp_path / "completed.txt"
    completed = await executor.run(tool, _probe_args(completed_marker, 0.05), confirmed=True)
    assert completed.status == "ok"
    assert completed_marker.read_text(encoding="utf-8") == "done"
    assert tool.ran_on_loop is False

    tool.timeout_seconds = 0.25
    late = tmp_path / "late-side-effect.txt"
    started = time.monotonic()
    timed_out = await executor.run(tool, _probe_args(late, 1.5), confirmed=True)
    assert timed_out.status == "timeout"
    assert time.monotonic() - started < 1.0
    assert not late.exists()
    assert not _alive(executor.reaped_workers[-1].pid)


def test_worker_rejects_unknown_tools_without_running_code():
    source = Path("app/tools/worker.py").read_text(encoding="utf-8")
    executor_source = Path("app/tools/executor.py").read_text(encoding="utf-8")
    for text in (source, executor_source):
        assert "eval(" not in text
        assert "pickle" not in text
        assert "shell=True" not in text
        assert "os.system(" not in text
    completed = subprocess.run(
        [sys.executable, "-m", "app.tools.worker"],
        input=b'{"tool":"shell","arguments":{"command":"ls"},"effect":"blocking_read_only"}',
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["ok"] is False


@pytest.mark.asyncio
async def test_grant_revocation_does_not_stop_a_running_tool(client, database_file, tmp_path):
    tool = BlockingMarkerTool()
    get_tool_registry().register(tool)
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    grant(client, agent["id"], ["blocking_marker"])
    marker = tmp_path / "revoked.txt"
    started = tmp_path / "revoked-started.txt"
    session = _autocommit_session()
    service = ToolService(
        get_tool_registry(),
        AgentToolGrantRepository(session),
        AgentService(AgentRepository(session)),
    )
    task = asyncio.create_task(
        service.test("blocking_marker", _probe_args(marker, 0.4, started), agent["id"])
    )
    revoked = threading.Event()

    def revoke() -> None:
        with open_sqlite(database_file) as connection:
            connection.execute(
                "DELETE FROM agent_tool_grants WHERE agent_id = ?",
                (agent["id"],),
            )
        revoked.set()

    remover = threading.Thread(target=revoke)
    try:
        await _wait_for_file(started)
        remover.start()
        outcome = await task
        assert revoked.wait(timeout=2)
        assert outcome.status == "ok"
        assert marker.read_text(encoding="utf-8") == "done"
        assert tool.ran_on_loop is False
        with pytest.raises(ToolPermissionError):
            await service.test("blocking_marker", _probe_args(tmp_path / "after.txt", 0), agent["id"])
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        session.close()
        if remover.is_alive():
            remover.join(timeout=2)
    follow = client.post(
        "/api/v1/tools/blocking_marker/test",
        json={"arguments": _probe_args(tmp_path / "api-after.txt", 0), "agent_id": agent["id"]},
    )
    assert follow.status_code == 403
    assert not (tmp_path / "after.txt").exists()


@pytest.mark.asyncio
async def test_disabling_agent_or_tool_does_not_stop_a_running_tool(client, database_file, tmp_path):
    tool = BlockingMarkerTool()
    get_tool_registry().register(tool)
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    grant(client, agent["id"], ["blocking_marker"])
    marker = tmp_path / "disabled-agent.txt"
    started = tmp_path / "disabled-agent-started.txt"
    session = _autocommit_session()
    service = ToolService(
        get_tool_registry(),
        AgentToolGrantRepository(session),
        AgentService(AgentRepository(session)),
    )
    task = asyncio.create_task(
        service.test("blocking_marker", _probe_args(marker, 0.4, started), agent["id"])
    )
    disabled = threading.Event()

    def disable_agent() -> None:
        with open_sqlite(database_file) as connection:
            connection.execute("UPDATE agents SET enabled = 0 WHERE id = ?", (agent["id"],))
        disabled.set()

    worker = threading.Thread(target=disable_agent)
    try:
        await _wait_for_file(started)
        worker.start()
        outcome = await task
        assert disabled.wait(timeout=2)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        session.close()
        if worker.is_alive():
            worker.join(timeout=2)
    assert outcome.status == "ok"
    assert marker.read_text(encoding="utf-8") == "done"
    denied = client.post(
        "/api/v1/tools/blocking_marker/test",
        json={"arguments": _probe_args(tmp_path / "agent-after.txt", 0), "agent_id": agent["id"]},
    )
    assert denied.status_code == 409

    client.patch(f"/api/v1/agents/{agent['id']}", json={"enabled": True})
    fresh = _autocommit_session()
    try:
        fresh_service = ToolService(
            get_tool_registry(),
            AgentToolGrantRepository(fresh),
            AgentService(AgentRepository(fresh)),
        )
        tool.enabled = False
        with pytest.raises(ToolDisabledError):
            await fresh_service.test(
                "blocking_marker",
                _probe_args(tmp_path / "tool-off.txt", 0),
                agent["id"],
            )
    finally:
        fresh.close()
    assert not (tmp_path / "tool-off.txt").exists()


@pytest.mark.asyncio
async def test_disabling_a_tool_does_not_stop_the_call_already_running(client, tmp_path):
    tool = BlockingMarkerTool()
    get_tool_registry().register(tool)
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    grant(client, agent["id"], ["blocking_marker"])
    marker = tmp_path / "tool-disabled.txt"
    started = tmp_path / "tool-disabled-started.txt"
    session = _autocommit_session()
    service = ToolService(
        get_tool_registry(),
        AgentToolGrantRepository(session),
        AgentService(AgentRepository(session)),
    )
    task = asyncio.create_task(
        service.test("blocking_marker", _probe_args(marker, 0.3, started), agent["id"])
    )
    try:
        await _wait_for_file(started)
        tool.enabled = False
        outcome = await task
        assert outcome.status == "ok"
        assert marker.read_text(encoding="utf-8") == "done"
        with pytest.raises(ToolDisabledError):
            await service.test("blocking_marker", _probe_args(tmp_path / "still-off.txt", 0), agent["id"])
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        session.close()
    assert not (tmp_path / "still-off.txt").exists()
