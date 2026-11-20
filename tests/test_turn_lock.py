"""A chat turn must not hold SQLite's write lock while the model answers.

Found in phase 5: the pause button (and approve) waited for the whole turn,
because the turn kept its transaction open across the model call. The kill
switch must take effect while a long turn is still running.
"""

import asyncio
import time

import httpx

from app.main import app
from app.providers.ollama import OllamaError, OllamaProvider
from tests.test_agents import create_agent
from tests.test_tools import grant, tool_call

SLOW = 1.5


async def _turn_and_pause(headers, chat_body, pause_after=0.3):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", headers=headers, timeout=30) as client:
        async def pause():
            await asyncio.sleep(pause_after)
            started = time.monotonic()
            response = await client.post("/api/v1/control/pause", json={"reason": "stop"})
            return response, time.monotonic() - started

        chat, (paused, waited) = await asyncio.gather(client.post("/api/v1/chat", json=chat_body), pause())
        return chat, paused, waited


def test_pause_takes_effect_while_the_model_is_answering(client, monkeypatch):
    async def slow(self, model, message, system_prompt=None, history=None):
        await asyncio.sleep(SLOW)
        return "done"

    monkeypatch.setattr(OllamaProvider, "chat", slow)
    chat, paused, waited = asyncio.run(_turn_and_pause(dict(client.headers), {"message": "long task"}))
    assert paused.status_code == 200, paused.text
    assert waited < 1.0, f"pause waited {waited:.2f}s for the turn"
    assert chat.status_code == 200
    assert client.get("/api/v1/control").json()["paused"] is True


def test_pause_is_not_blocked_by_a_tool_run_earlier_in_the_turn(client, monkeypatch):
    agent = create_agent(client, system_prompt="Use tools.")
    grant(client, agent["id"], ["calculator"])
    calls = []

    async def scripted(self, model, message, system_prompt=None, history=None):
        calls.append(1)
        if len(calls) == 1:
            return tool_call("calculator", {"expression": "6*7"})
        await asyncio.sleep(SLOW)  # the turn's second model call, after the tool wrote its log row
        return "42"

    monkeypatch.setattr(OllamaProvider, "chat", scripted)
    chat, paused, waited = asyncio.run(
        _turn_and_pause(dict(client.headers), {"message": "6*7?", "agent_id": agent["id"]}, pause_after=0.4)
    )
    assert paused.status_code == 200, paused.text
    assert waited < 1.0, f"pause waited {waited:.2f}s for the turn"
    assert chat.status_code == 200


def test_a_failed_new_conversation_leaves_nothing_behind(client, monkeypatch):
    async def fail(self, model, message, system_prompt=None, history=None):
        raise OllamaError("connection refused")

    monkeypatch.setattr(OllamaProvider, "chat", fail)
    before = client.get("/api/v1/conversations").json()["count"]
    assert client.post("/api/v1/chat", json={"message": "hello"}).status_code == 502
    assert client.get("/api/v1/conversations").json()["count"] == before
