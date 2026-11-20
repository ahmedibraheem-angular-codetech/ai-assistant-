"""Two messages sent at once must both be answered (found by the Mac app, phase 5).

A chat turn keeps its database transaction open while the model answers. A
second turn that starts meanwhile used to fail at once with "The conversation
could not be saved." (HTTP 500): SQLite refuses the second writer.
"""

import asyncio

import httpx

from app.main import app
from app.providers.ollama import OllamaProvider


def _slow_model(monkeypatch, delay=0.3):
    async def chat(self, model, message, system_prompt=None, history=None):
        await asyncio.sleep(delay)
        return f"reply to {message}"

    monkeypatch.setattr(OllamaProvider, "chat", chat)


async def _send_together(headers, bodies):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", headers=headers) as client:
        return await asyncio.gather(*(client.post("/api/v1/chat", json=body) for body in bodies))


def test_two_messages_to_one_conversation_are_both_answered(client, monkeypatch):
    _slow_model(monkeypatch)
    first = client.post("/api/v1/chat", json={"message": "start"})
    assert first.status_code == 200
    conversation_id = first.json()["conversation_id"]

    replies = asyncio.run(_send_together(dict(client.headers), [
        {"message": "one", "conversation_id": conversation_id},
        {"message": "two", "conversation_id": conversation_id},
    ]))

    assert [reply.status_code for reply in replies] == [200, 200], [reply.text for reply in replies]
    messages = client.get(f"/api/v1/conversations/{conversation_id}/messages").json()["messages"]
    assert [m["sequence_number"] for m in messages] == [1, 2, 3, 4, 5, 6]
    # Turns do not interleave: each user message is followed by its own answer.
    pairs = [(messages[i]["content"], messages[i + 1]["content"]) for i in range(0, 6, 2)]
    assert all(answer == f"reply to {question}" for question, answer in pairs)


def test_messages_to_different_conversations_at_once_are_both_answered(client, monkeypatch):
    _slow_model(monkeypatch)
    replies = asyncio.run(_send_together(dict(client.headers), [{"message": "alpha"}, {"message": "beta"}]))
    assert [reply.status_code for reply in replies] == [200, 200], [reply.text for reply in replies]
    assert len({reply.json()["conversation_id"] for reply in replies}) == 2
