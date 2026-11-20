"""Team delegation (phase 2, slice 3)."""

import json

from app.tools.registry import get_tool_registry
from tests.test_agents import create_agent
from tests.test_tools import grant, scripted_chat, tool_call

MANAGER_PROMPT = "You are the Manager."
WORKER_PROMPT = "You are the Worker."


def _team(client, manager, members):
    return client.put(f"/api/v1/agents/{manager['id']}/team", json={"member_ids": [m["id"] for m in members]})


def _setup(client, *, worker_tools=("calculator",), manager_tools=("delegate_task",)):
    manager = create_agent(client, name="Manager", system_prompt=MANAGER_PROMPT)
    worker = create_agent(client, name="Worker", system_prompt=WORKER_PROMPT)
    grant(client, manager["id"], list(manager_tools))
    grant(client, worker["id"], list(worker_tools))
    return manager, worker


def _results(item):
    return [e["content"] for e in item["history"] or [] if str(e["content"]).startswith("Tool result:")]


def _script(monkeypatch, *, delegate_to="Worker", worker_call=("calculator", {"expression": "2+2"})):
    seen = {"manager_results": [], "worker_results": []}

    def respond(item, calls):
        prompt = item["system_prompt"] or ""
        results = _results(item)
        if MANAGER_PROMPT in prompt:
            if results:
                seen["manager_results"] = results
                return "Team answer received."
            return tool_call("delegate_task", {"agent": delegate_to, "task": "What is 2+2?"})
        if WORKER_PROMPT in prompt:
            if results:
                seen["worker_results"] = results
                return "The answer is 4."
            if worker_call is None:
                return "The answer is 4."
            return tool_call(worker_call[0], worker_call[1])
        return "unexpected"

    scripted_chat(monkeypatch, respond)
    return seen


def _chat(client, agent):
    return client.post("/api/v1/chat", json={"message": "Ask your team: what is 2+2?", "agent_id": agent["id"]})


def _payload(result_text: str) -> dict:
    return json.loads(result_text.removeprefix("Tool result:").strip())


def test_manager_delegates_and_gets_the_answer(client, monkeypatch):
    manager, worker = _setup(client)
    assert _team(client, manager, [worker]).status_code == 200
    seen = _script(monkeypatch)
    before = get_tool_registry().get("delegate_task").calls
    response = _chat(client, manager)
    assert response.status_code == 200, response.text
    assert response.json()["answer"] == "Team answer received."
    assert any('"value": 4' in r or '"value":4' in r for r in seen["worker_results"])
    payload = _payload(seen["manager_results"][0])
    assert payload["status"] == "ok"
    assert payload["output"]["agent_name"] == "Worker"
    assert payload["output"]["answer"] == "The answer is 4."
    assert payload["output"]["data_boundary"] == "untrusted_agent_output"
    assert get_tool_registry().get("delegate_task").calls - before == 1


def test_only_team_members_can_receive_tasks(client, monkeypatch):
    manager, worker = _setup(client)
    outsider = create_agent(client, name="Outsider", system_prompt=WORKER_PROMPT)
    _team(client, manager, [worker])
    seen = _script(monkeypatch, delegate_to="Outsider")
    assert _chat(client, manager).status_code == 200
    payload = _payload(seen["manager_results"][0])
    assert payload["status"] == "error"
    assert "not on this team" in payload["error"]
    assert outsider  # created but never run


def test_no_team_means_no_delegation(client, monkeypatch):
    manager, _worker = _setup(client)
    seen = _script(monkeypatch)
    _chat(client, manager)
    payload = _payload(seen["manager_results"][0])
    assert payload["status"] == "error"
    assert "no team" in payload["error"]


def test_member_uses_its_own_permissions_not_the_managers(client, monkeypatch):
    # The manager may use the calculator; the worker may not.
    manager, worker = _setup(client, worker_tools=(), manager_tools=("delegate_task", "calculator"))
    _team(client, manager, [worker])
    seen = _script(monkeypatch)
    before = get_tool_registry().get("calculator").calls
    _chat(client, manager)
    # The worker has no tools, so it answers directly and never runs the calculator.
    assert seen["worker_results"] == []
    assert get_tool_registry().get("calculator").calls == before


def test_members_cannot_delegate_further(client, monkeypatch):
    manager, worker = _setup(client, worker_tools=("delegate_task",))
    third = create_agent(client, name="Third", system_prompt=WORKER_PROMPT)
    _team(client, manager, [worker])
    _team(client, worker, [third])
    seen = _script(monkeypatch, worker_call=("delegate_task", {"agent": "Third", "task": "x"}))
    before = get_tool_registry().get("delegate_task").calls
    _chat(client, manager)
    worker_result = _payload(seen["worker_results"][0])
    assert worker_result["status"] == "error"
    assert "cannot delegate further" in worker_result["error"]
    assert get_tool_registry().get("delegate_task").calls - before == 2  # manager's + refused worker's


def test_member_budget_applies(client, monkeypatch):
    manager, worker = _setup(client)
    _team(client, manager, [worker])
    client.put(f"/api/v1/control/agents/{worker['id']}/budget", json={"daily_chat_turns": 1})
    _script(monkeypatch)
    assert _chat(client, manager).status_code == 200
    seen = _script(monkeypatch)
    _chat(client, manager)
    payload = _payload(seen["manager_results"][0])
    assert payload["status"] == "error"
    assert "daily chat limit" in payload["error"]


def test_pause_stops_delegation(client, monkeypatch):
    manager, worker = _setup(client)
    _team(client, manager, [worker])
    _script(monkeypatch)
    client.post("/api/v1/control/pause")
    assert _chat(client, manager).status_code == 423


def test_team_admin_rules(client):
    manager, worker = _setup(client)
    assert _team(client, manager, [manager]).status_code == 422
    missing = {"id": "00000000-0000-4000-8000-000000000000"}
    assert _team(client, manager, [missing]).status_code == 404
    assert _team(client, manager, [worker, worker]).json()["member_ids"] == [worker["id"]]
    assert client.get(f"/api/v1/agents/{manager['id']}/team").json()["member_ids"] == [worker["id"]]
    assert _team(client, manager, []).json()["member_ids"] == []


def test_deleting_a_member_removes_it_from_teams(client):
    manager, worker = _setup(client)
    _team(client, manager, [worker])
    assert client.delete(f"/api/v1/agents/{worker['id']}").status_code in {200, 204}
    assert client.get(f"/api/v1/agents/{manager['id']}/team").json()["member_ids"] == []


def test_member_approval_is_created_inside_the_managers_request(client, monkeypatch, tmp_path):
    """The member's approval row is written in the same request without a lock wait."""
    from tests.test_files import grant_workspace, register

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.txt").write_text("alpha", encoding="utf-8")
    workspace = register(client, folder)
    manager, worker = _setup(client, worker_tools=("read_file",))
    grant_workspace(client, worker["id"], workspace["id"])
    _team(client, manager, [worker])
    seen = _script(
        monkeypatch,
        worker_call=("read_file", {"workspace_id": workspace["id"], "relative_path": "a.txt"}),
    )
    response = _chat(client, manager)
    assert response.status_code == 200, response.text
    worker_result = _payload(seen["worker_results"][0])
    assert worker_result["status"] == "confirmation_required"
    pending = client.get("/api/v1/approvals").json()["approvals"]
    assert len(pending) == 1
    assert pending[0]["agent_id"] == worker["id"]
    assert pending[0]["tool_name"] == "read_file"
