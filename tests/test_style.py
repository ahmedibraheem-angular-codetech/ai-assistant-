import json
import stat

from app import style
from tests.test_tools import scripted_chat

PROFILE = {"enabled": True, "description": "short sentences, Egyptian Arabic", "samples": ["ازيك يا باشا"]}


def _chat(client, monkeypatch, **extra):
    calls = scripted_chat(monkeypatch, lambda item, calls: "ok")
    assert client.post("/api/v1/chat", json={"message": "hello there", **extra}).status_code == 200
    return calls[0]["system_prompt"] or ""


def test_profile_round_trip_and_validation(client):
    assert client.get("/api/v1/style").json()["enabled"] is False
    saved = client.put("/api/v1/style", json=PROFILE).json()
    assert saved["updated_at"]
    got = client.get("/api/v1/style").json()
    assert got["description"] == PROFILE["description"] and got["samples"] == PROFILE["samples"]
    assert client.put("/api/v1/style", json={**PROFILE, "extra": 1}).status_code == 422
    assert client.put("/api/v1/style", json={**PROFILE, "description": "x" * 2001}).status_code == 422
    assert client.put("/api/v1/style", json={**PROFILE, "samples": ["a"] * 11}).status_code == 422
    assert client.put("/api/v1/style", json={**PROFILE, "samples": ["a" * 2001]}).status_code == 422


def test_injection_default_enabled_disabled(client, monkeypatch):
    assert "<style_sample>" not in _chat(client, monkeypatch)
    client.put("/api/v1/style", json=PROFILE)
    prompt = _chat(client, monkeypatch)
    assert "Writing style of the user" in prompt and "<style_sample>" in prompt
    assert "short sentences" in prompt
    client.put("/api/v1/style", json={**PROFILE, "enabled": False})
    assert "Writing style" not in _chat(client, monkeypatch)


def test_injection_agent_path(client, monkeypatch):
    agent = client.post(
        "/api/v1/agents",
        json={"name": "Styled", "description": "d", "role": "r", "system_prompt": "Be exact.", "model": "m"},
    ).json()
    client.put("/api/v1/style", json=PROFILE)
    prompt = _chat(client, monkeypatch, agent_id=agent["id"])
    assert prompt.startswith("Be exact.") and "<style_sample>" in prompt


def test_injection_is_capped(client):
    client.put("/api/v1/style", json={"enabled": True, "description": "d" * 2000, "samples": ["s" * 2000] * 10})
    assert len(style.style_block()) <= 6000


def test_dataset_count_and_export(client, monkeypatch, database_file):
    scripted_chat(monkeypatch, lambda item, calls: "reply " + item["message"])
    token_like = "A" * 43
    for text in ("one", "two", token_like, "remember that I like tea"):
        assert client.post("/api/v1/chat", json={"message": text}).status_code == 200
    status = client.get("/api/v1/style/dataset").json()
    assert status == {"pairs": 2, "min_needed": 300, "ready": False}
    out = client.post("/api/v1/style/dataset/export").json()
    assert out["pairs"] == 2
    path = database_file.parent / "training" / out["path"].split("/")[-1]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert {m["role"] for line in lines for m in line["messages"]} == {"user", "assistant"}
    assert lines[0]["messages"][0]["content"] in ("one", "two")
