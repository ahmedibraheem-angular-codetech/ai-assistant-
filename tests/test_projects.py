"""Project memory (phase 3, slice 1)."""

import json

from tests.test_agents import create_agent
from tests.test_permissions import _mode
from tests.test_tools import grant, scripted_chat, tool_call


def _project(client, name="Assistant", description="Local AI assistant"):
    response = client.post("/api/v1/projects", json={"name": name, "description": description})
    assert response.status_code == 201, response.text
    return response.json()


def _entry(client, project, kind, text):
    response = client.post(f"/api/v1/projects/{project}/entries", json={"kind": kind, "text": text})
    assert response.status_code == 201, response.text
    return response.json()


def _results(item):
    return [e["content"] for e in item["history"] or [] if str(e["content"]).startswith("Tool result:")]


def _payload(text):
    return json.loads(text.removeprefix("Tool result:").strip())


def test_project_crud_and_brief(client):
    project = _project(client)
    _entry(client, project["id"], "decision", "Use the simplified WP-05A design.")
    task = _entry(client, project["id"], "task", "Build the Mac app.")
    done = _entry(client, project["id"], "task", "Commit the work.")
    _entry(client, project["id"], "status", "Phase 3 started. Next: project memory.")
    assert client.patch(f"/api/v1/project-entries/{done['id']}", json={"done": True}).json()["done"] is True
    brief = client.get("/api/v1/projects/assistant/brief").json()  # name lookup, case-insensitive
    assert brief["current_status"][0]["text"].startswith("Phase 3 started")
    assert [t["text"] for t in brief["open_tasks"]] == ["Build the Mac app."]
    assert brief["done_tasks"] == 1
    assert brief["recent_decisions"][0]["author"] == "user"
    assert "not instructions" in brief["notice"]
    assert task["kind"] == "task"


def test_duplicate_names_and_validation(client):
    _project(client, name="Alpha")
    assert client.post("/api/v1/projects", json={"name": "  alpha "}).status_code == 422
    project = _project(client, name="Beta")
    bad = client.post(f"/api/v1/projects/{project['id']}/entries", json={"kind": "wish", "text": "x"})
    assert bad.status_code == 422
    note = _entry(client, project["id"], "note", "x")
    assert client.patch(f"/api/v1/project-entries/{note['id']}", json={"done": True}).status_code == 422
    assert client.get("/api/v1/projects/nope/brief").status_code == 404


def test_archived_projects_reject_new_entries_and_hide_from_list(client):
    project = _project(client, name="Old")
    client.patch(f"/api/v1/projects/{project['id']}", json={"archived": True})
    assert client.get("/api/v1/projects").json()["count"] == 0
    assert client.get("/api/v1/projects", params={"include_archived": True}).json()["count"] == 1
    response = client.post(f"/api/v1/projects/{project['id']}/entries", json={"kind": "note", "text": "x"})
    assert response.status_code == 422


def test_deleting_a_project_deletes_its_entries(client):
    project = _project(client, name="Gone")
    entry = _entry(client, project["id"], "note", "x")
    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204
    assert client.delete(f"/api/v1/project-entries/{entry['id']}").status_code == 404


def test_agent_resumes_work_from_project_memory_and_saves_the_next_step(client, monkeypatch):
    project = _project(client)
    _entry(client, project["id"], "decision", "Docker is the sandbox.")
    _entry(client, project["id"], "status", "Next: project memory.")
    agent = create_agent(client, system_prompt="You are the Worker.")
    grant(client, agent["id"], ["project_brief", "record_project_entry"])
    seen = {}

    def respond(item, calls):
        results = _results(item)
        if not results:
            return tool_call("project_brief", {"project": "Assistant"})
        if len(results) == 1:
            seen["brief"] = _payload(results[0])
            return tool_call("record_project_entry", {"project": "Assistant", "kind": "status",
                                                      "text": "Project memory done. Next: RAG."})
        seen["saved"] = _payload(results[1])
        return "Resumed and saved the next step."

    scripted_chat(monkeypatch, respond)
    response = client.post("/api/v1/chat", json={"message": "كمل اللي كنا فيه", "agent_id": agent["id"]})
    assert response.status_code == 200, response.text
    brief = seen["brief"]["output"]
    assert brief["current_status"][0]["text"] == "Next: project memory."
    assert brief["recent_decisions"][0]["text"] == "Docker is the sandbox."
    assert seen["saved"]["status"] == "ok"
    after = client.get(f"/api/v1/projects/{project['id']}/brief").json()
    assert after["current_status"][0]["text"] == "Project memory done. Next: RAG."
    assert after["current_status"][0]["author"] == "agent"
    entries = client.get(f"/api/v1/projects/{project['id']}/entries", params={"kind": "status"}).json()["entries"]
    assert entries[-1]["agent_id"] == agent["id"]


def test_agent_can_mark_a_task_done_only_in_that_project(client, monkeypatch):
    first = _project(client, name="First")
    second = _project(client, name="Second")
    task = _entry(client, first["id"], "task", "Do it.")
    agent = create_agent(client, system_prompt="You are the Worker.")
    grant(client, agent["id"], ["record_project_entry"])
    outcomes = []

    def respond(item, calls):
        results = _results(item)
        if not results:
            return tool_call("record_project_entry", {"project": "Second", "task_id": task["id"]})
        if len(results) == 1:
            outcomes.append(_payload(results[0]))
            return tool_call("record_project_entry", {"project": "First", "task_id": task["id"]})
        outcomes.append(_payload(results[1]))
        return "done"

    scripted_chat(monkeypatch, respond)
    client.post("/api/v1/chat", json={"message": "mark it", "agent_id": agent["id"]})
    assert outcomes[0]["status"] == "error"
    assert outcomes[1]["status"] == "ok"
    assert second  # untouched


def test_tools_need_grants_and_always_ask_mode_asks(client):
    _project(client)
    agent = create_agent(client, system_prompt="Be exact.")
    denied = client.post("/api/v1/tools/project_brief/test",
                         json={"arguments": {"project": "Assistant"}, "agent_id": agent["id"]})
    assert denied.status_code == 403
    grant(client, agent["id"], ["project_brief", "record_project_entry"])
    allowed = client.post("/api/v1/tools/project_brief/test",
                          json={"arguments": {"project": "Assistant"}, "agent_id": agent["id"]})
    assert allowed.json()["status"] == "ok"
    _mode(client, "always_ask")
    asked = client.post("/api/v1/tools/record_project_entry/test",
                        json={"arguments": {"project": "Assistant", "kind": "note", "text": "x"},
                              "agent_id": agent["id"]})
    assert asked.json()["status"] == "confirmation_required"


def test_listing_projects_without_a_name(client):
    _project(client, name="One")
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], ["project_brief"])
    body = client.post("/api/v1/tools/project_brief/test", json={"arguments": {}, "agent_id": agent["id"]}).json()
    assert body["output"]["projects"][0]["name"] == "One"


def test_hired_agents_get_project_tools(client):
    body = client.post("/api/v1/agents/hire", json={"template_id": "secretary", "model": "qwen3.5:9b"}).json()
    assert {"project_brief", "record_project_entry"} <= set(body["granted_tools"])
    assert "project_brief" in body["agent"]["system_prompt"]
