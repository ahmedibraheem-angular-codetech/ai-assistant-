"""n8n integration (phase 7, slice 2): registry routes, validation, approval, execution."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.core.config import get_settings
from app.n8n import base_url
from app.permissions.manifests import manifest_for_tool
from app.permissions.standing import eligible
from app.tools.registry import get_tool_registry
from tests.approval_support import finish_tool_test
from tests.test_agents import create_agent
from tests.test_tools import grant, scripted_chat, tool_call

URL = "/api/v1/n8n/workflows"
GOOD = {"name": "send-report", "description": "Mails the report", "webhook_path": "hooks/report", "method": "POST"}


def _add(client, **over):
    return client.post(URL, json={**GOOD, **over})


class _Hook(BaseHTTPRequestHandler):
    seen: list = []
    reply = (200, b"done")

    def _answer(self, body):
        type(self).seen.append((self.command, self.path, body, self.headers.get("Content-Type")))
        status, text = type(self).reply
        self.send_response(status)
        self.end_headers()
        self.wfile.write(text)

    def do_POST(self):
        self._answer(self.rfile.read(int(self.headers.get("Content-Length") or 0)))

    def do_GET(self):
        self._answer(b"")

    def log_message(self, *args):
        pass


@pytest.fixture
def n8n_server(monkeypatch):
    _Hook.seen = []
    _Hook.reply = (200, b"done")
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("N8N_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    get_settings.cache_clear()
    yield _Hook
    server.shutdown()
    get_settings.cache_clear()


def _ask(client, agent_id, **arguments):
    return client.post("/api/v1/tools/run_n8n_workflow/test",
                       json={"arguments": arguments, "agent_id": agent_id})


def test_crud_and_unique_name(client):
    created = _add(client)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    assert _add(client).status_code == 409
    assert [w["name"] for w in client.get(URL).json()["workflows"]] == ["send-report"]
    patched = client.patch(f"{URL}/{wid}", json={"enabled": False, "method": "GET"})
    assert patched.json()["enabled"] is False and patched.json()["method"] == "GET"
    assert client.patch(f"{URL}/{wid}", json={}).status_code == 422
    assert client.patch(f"{URL}/{wid}", json={"bogus": 1}).status_code == 422
    assert client.delete(f"{URL}/{wid}").status_code == 204
    assert client.delete(f"{URL}/{wid}").status_code == 404


@pytest.mark.parametrize("over", [
    {"webhook_path": "../etc/passwd"}, {"webhook_path": "a/../b"}, {"webhook_path": "/abs"},
    {"webhook_path": "a b"}, {"webhook_path": "a?x=1"}, {"webhook_path": "x" * 201},
    {"name": "Bad Name"}, {"name": "x" * 41}, {"name": "under_score"},
    {"method": "DELETE"}, {"description": "d" * 301}, {"extra": 1},
])
def test_validation_rejects(client, over):
    assert _add(client, **over).status_code == 422


@pytest.mark.parametrize("bad", ["http://example.com:5678", "ftp://127.0.0.1", "http://10.0.0.5:5678",
                                 "http://127.0.0.1:5678/x", "http://user:p@127.0.0.1:5678"])
def test_base_url_must_be_loopback(monkeypatch, bad):
    monkeypatch.setenv("N8N_BASE_URL", bad)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError):
            base_url()
    finally:
        get_settings.cache_clear()


def test_status_refuses_non_loopback_and_reports_health(client, monkeypatch, n8n_server):
    assert client.get("/api/v1/n8n/status").json()["ok"] is True
    monkeypatch.setenv("N8N_BASE_URL", "http://example.com")
    get_settings.cache_clear()
    body = client.get("/api/v1/n8n/status").json()
    assert body["ok"] is False and "loopback" in body["error"]


def test_manifest_always_asks_and_no_standing_approval(client):
    manifest = manifest_for_tool(get_tool_registry().get("run_n8n_workflow"))
    assert manifest.risk == "sensitive" and manifest.approval_required and not manifest.read_only
    assert eligible(manifest) is False
    agent = create_agent(client, system_prompt="Be exact.")
    response = client.post("/api/v1/permissions/standing-approvals",
                           json={"agent_id": agent["id"], "tool_name": "run_n8n_workflow"})
    assert response.status_code == 422


def test_run_needs_approval_then_calls_webhook(client, n8n_server):
    _add(client)
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["run_n8n_workflow"])
    proposed = _ask(client, agent["id"], workflow="send-report", payload={"to": "me", "n": 2})
    assert proposed.json()["status"] == "confirmation_required"
    assert n8n_server.seen == []  # nothing ran before approval
    approval_id = proposed.json()["output"]["approval_id"]
    card = client.get(f"/api/v1/approvals/{approval_id}").json()
    assert card["arguments"]["workflow"] == "send-report" and card["arguments"]["payload"]["n"] == 2
    assert card["workflow"]["description"] == "Mails the report"
    assert card["workflow"]["webhook_path"] == "hooks/report" and card["workflow"]["method"] == "POST"
    done = finish_tool_test(client, proposed)
    assert done.json()["status"] == "ok", done.json()
    method, path, body, ctype = n8n_server.seen[0]
    assert (method, path, ctype) == ("POST", "/webhook/hooks/report", "application/json")
    assert json.loads(body) == {"to": "me", "n": 2}
    out = done.json()["output"]
    assert out["status_code"] == 200 and out["response_text"] == "done"
    assert out["data_boundary"] == "untrusted_n8n_response"


def test_get_method_sends_no_body_and_response_is_truncated(client, n8n_server, monkeypatch):
    monkeypatch.setenv("N8N_MAX_RESPONSE_CHARS", "100")
    get_settings.cache_clear()
    _add(client, method="GET")
    n8n_server.reply = (200, b"x" * 500)
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["run_n8n_workflow"])
    done = finish_tool_test(client, _ask(client, agent["id"], workflow="send-report", payload={"a": 1}))
    out = done.json()["output"]
    assert n8n_server.seen[0][0] == "GET" and n8n_server.seen[0][2] == b""
    assert len(out["response_text"]) == 100 and out["truncated"] is True


def test_unknown_or_disabled_workflow_is_refused(client, n8n_server):
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["run_n8n_workflow"])
    unknown = finish_tool_test(client, _ask(client, agent["id"], workflow="nope"))
    assert unknown.json()["status"] == "error" and "not registered" in unknown.json()["error"]
    _add(client, enabled=False)
    off = finish_tool_test(client, _ask(client, agent["id"], workflow="send-report"))
    assert off.json()["status"] == "error" and "turned off" in off.json()["error"]
    assert n8n_server.seen == []


def test_payload_cap(client, n8n_server, monkeypatch):
    monkeypatch.setenv("N8N_MAX_PAYLOAD_BYTES", "200")
    get_settings.cache_clear()
    _add(client)
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["run_n8n_workflow"])
    done = finish_tool_test(client, _ask(client, agent["id"], workflow="send-report", payload={"t": "x" * 300}))
    assert done.json()["status"] == "error" and "larger than the limit" in done.json()["error"]
    assert n8n_server.seen == []
    huge = _ask(client, agent["id"], workflow="send-report", payload={"t": "x" * 300_000})
    assert huge.status_code == 422
    assert _ask(client, agent["id"], workflow="send-report", payload=[1]).status_code == 422


def test_redirects_are_not_followed(client, n8n_server):
    _add(client)
    n8n_server.reply = (302, b"")
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["run_n8n_workflow"])
    out = finish_tool_test(client, _ask(client, agent["id"], workflow="send-report")).json()["output"]
    assert out["status_code"] == 302 and len(n8n_server.seen) == 1


def test_chat_tool_call_waits_for_approval(client, n8n_server, monkeypatch):
    _add(client)
    agent = create_agent(client, model="tool-model", system_prompt="Be exact.")
    grant(client, agent["id"], ["run_n8n_workflow"])

    def respond(item, calls):
        if len(calls) == 1:
            return tool_call("run_n8n_workflow", {"workflow": "send-report", "payload": {"k": 1}})
        return "Waiting for approval."

    scripted_chat(monkeypatch, respond)
    reply = client.post("/api/v1/chat", json={"message": "Run it.", "agent_id": agent["id"]})
    assert reply.status_code == 200
    assert n8n_server.seen == []
    pending = client.get("/api/v1/approvals").json()
    assert pending["count"] == 1
    assert pending["approvals"][0]["tool_name"] == "run_n8n_workflow"


def test_not_granted_by_default_to_templates():
    from app.agents.templates import TEMPLATES

    assert all("run_n8n_workflow" not in t.tools for t in TEMPLATES.values())
