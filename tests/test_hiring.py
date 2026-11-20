"""Hiring studio (phase 2, slice 4)."""

from app.agents.templates import TEMPLATES
from app.core.config import get_settings
from app.tools.registry import get_tool_registry
from tests.test_files import register


def _hire(client, **payload):
    payload.setdefault("model", "qwen3.5:9b")
    return client.post("/api/v1/agents/hire", json=payload)


def test_templates_are_listed(client):
    body = client.get("/api/v1/agent-templates").json()
    ids = {item["id"] for item in body["templates"]}
    assert {"marketing", "developer", "researcher", "secretary", "analyst", "manager"} <= ids
    marketing = next(item for item in body["templates"] if item["id"] == "marketing")
    assert marketing["title_ar"] == "مسؤول الدعاية والتسويق"


def test_every_template_only_lists_real_tools():
    names = {tool.name for tool in get_tool_registry().list_tools()}
    for template in TEMPLATES.values():
        assert set(template.all_tools) <= names, template.id
        assert "Never try to change your own permissions" in template.system_prompt


def test_hiring_creates_a_working_agent_with_template_tools_and_budget(client):
    response = _hire(client, template_id="marketing", name="Ads")
    assert response.status_code == 201, response.text
    body = response.json()
    agent = body["agent"]
    assert agent["name"] == "Ads"
    assert "marketing and advertising specialist" in agent["system_prompt"]
    tools = client.get(f"/api/v1/agents/{agent['id']}/tools").json()
    assert set(body["granted_tools"]) == set(TEMPLATES["marketing"].all_tools)
    assert set(tools["allowed_tools"]) == set(TEMPLATES["marketing"].all_tools)
    budget = client.get(f"/api/v1/control/agents/{agent['id']}/budget").json()
    assert budget["daily_tool_calls"] == 200
    assert budget["daily_chat_turns"] == 100
    assert body["standing_approvals"] == []
    standing = client.get("/api/v1/permissions/standing-approvals", params={"agent_id": agent["id"]}).json()
    assert standing["count"] == 0


def test_hiring_never_grants_change_tools_beyond_the_template(client):
    body = _hire(client, template_id="researcher").json()
    assert not {"update_text_file", "create_text_file", "quarantine_file", "delegate_task"} & set(body["granted_tools"])


def test_hiring_with_a_workspace(client, tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    workspace = register(client, folder)
    body = _hire(client, template_id="developer", workspace_ids=[workspace["id"]]).json()
    assert body["workspace_ids"] == [workspace["id"]]


def test_bad_workspace_rolls_the_hire_back(client):
    missing = "00000000-0000-4000-8000-000000000000"
    response = _hire(client, template_id="developer", name="Dev X", workspace_ids=[missing])
    assert response.status_code == 422
    names = [agent["name"] for agent in client.get("/api/v1/agents").json()["agents"]]
    assert "Dev X" not in names


def test_extra_instructions_and_no_budget(client):
    body = _hire(
        client, template_id="secretary", extra_instructions="Always answer in English.", apply_budget=False
    ).json()
    assert "Always answer in English." in body["agent"]["system_prompt"]
    assert body["budget"] is None


def test_hiring_errors(client, monkeypatch):
    assert _hire(client, template_id="astronaut").status_code == 404
    assert _hire(client, template_id="marketing", name="Same").status_code == 201
    assert _hire(client, template_id="marketing", name="Same").status_code == 409
    monkeypatch.setenv("OLLAMA_MODEL", "")
    get_settings.cache_clear()
    try:
        response = client.post("/api/v1/agents/hire", json={"template_id": "marketing", "name": "NoModel"})
        assert response.status_code == 422
    finally:
        get_settings.cache_clear()


def test_hired_manager_can_get_a_team(client):
    manager = _hire(client, template_id="manager").json()["agent"]
    member = _hire(client, template_id="researcher").json()["agent"]
    response = client.put(f"/api/v1/agents/{manager['id']}/team", json={"member_ids": [member["id"]]})
    assert response.status_code == 200
    assert "delegate_task" in _hire(client, template_id="manager", name="Boss 2").json()["granted_tools"]
