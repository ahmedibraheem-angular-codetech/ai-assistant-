"""Pausing stops a model reply that is already running."""

import asyncio
import time

import httpx
import pytest

from app.core import controls
from app.core.controls import AssistantBlockedError
from app.providers.ollama import OllamaProvider


def _slow_post(started: list, cancelled: list):
    async def post(self, url, **kwargs):
        started.append(url)
        try:
            await asyncio.sleep(30)  # a long local generation
        except asyncio.CancelledError:
            cancelled.append(url)
            raise
        raise AssertionError("the request should have been cancelled")

    return post


def test_pause_cancels_a_running_model_call(monkeypatch):
    started: list = []
    cancelled: list = []
    paused = {"value": False}
    monkeypatch.setattr(controls, "is_paused", lambda: paused["value"])
    monkeypatch.setattr(httpx.AsyncClient, "post", _slow_post(started, cancelled))

    async def scenario():
        async def pause_soon():
            await asyncio.sleep(0.3)
            paused["value"] = True

        asyncio.ensure_future(pause_soon())
        began = time.monotonic()
        with pytest.raises(AssistantBlockedError):
            await OllamaProvider().chat(model="m", message="hi")
        return time.monotonic() - began

    elapsed = asyncio.run(scenario())
    assert started and cancelled, "the HTTP request must be cancelled, not left running"
    assert elapsed < 2.0


def test_finished_call_is_returned_when_not_paused(monkeypatch):
    monkeypatch.setattr(controls, "is_paused", lambda: False)

    async def post(self, url, **kwargs):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"message": {"content": "done"}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert asyncio.run(OllamaProvider().chat(model="m", message="hi")) == "done"
