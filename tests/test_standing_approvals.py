"""Standing approvals: a lasting yes for reads by one agent (phase 2, slice 1)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.permissions.engine import ALLOW, REQUIRES_APPROVAL, PermissionFacts, decide
from app.permissions.models import StandingApprovalRecord
from tests.test_agents import create_agent
from tests.test_files import grant_workspace, read_tool, register
from tests.test_permissions import _mode
from tests.test_tools import grant

URL = "/api/v1/permissions/standing-approvals"


def _setup(client, tmp_path, tools=("read_file",)):
    folder = tmp_path / "notes"
    (folder / "docs").mkdir(parents=True)
    (folder / "docs" / "a.txt").write_text("alpha", encoding="utf-8")
    (folder / "other.txt").write_text("beta", encoding="utf-8")
    workspace = register(client, folder)
    agent = create_agent(client, system_prompt="Be exact.")
    grant(client, agent["id"], list(tools))
    grant_workspace(client, agent["id"], workspace["id"])
    return folder, workspace, agent


def _standing(client, **payload):
    return client.post(URL, json=payload)


def _read(client, agent, workspace, path):
    return read_tool(client, agent["id"], workspace["id"], path, finish_approval=False)


def test_without_standing_approval_reads_still_ask(client, tmp_path):
    _folder, workspace, agent = _setup(client, tmp_path)
    assert _read(client, agent, workspace, "other.txt").json()["status"] == "confirmation_required"


def test_workspace_standing_approval_lets_reads_run(client, tmp_path):
    _folder, workspace, agent = _setup(client, tmp_path)
    created = _standing(client, agent_id=agent["id"], tool_name="read_file", workspace_id=workspace["id"])
    assert created.status_code == 201, created.text
    body = _read(client, agent, workspace, "other.txt").json()
    assert body["status"] == "ok", body
    assert body["output"]["content"] == "beta"


def test_path_prefix_limits_the_scope(client, tmp_path):
    _folder, workspace, agent = _setup(client, tmp_path)
    _standing(
        client, agent_id=agent["id"], tool_name="read_file", workspace_id=workspace["id"], path_prefix="docs"
    )
    assert _read(client, agent, workspace, "docs/a.txt").json()["status"] == "ok"
    assert _read(client, agent, workspace, "other.txt").json()["status"] == "confirmation_required"
    # "docs" must not match a sibling like "docs2/..."
    (tmp_path / "notes" / "docs2").mkdir()
    (tmp_path / "notes" / "docs2" / "b.txt").write_text("gamma", encoding="utf-8")
    assert _read(client, agent, workspace, "docs2/b.txt").json()["status"] == "confirmation_required"


def test_it_belongs_to_one_agent_and_one_tool(client, tmp_path):
    _folder, workspace, agent = _setup(client, tmp_path, tools=("read_file", "search_files"))
    _standing(client, agent_id=agent["id"], tool_name="read_file", workspace_id=workspace["id"])
    other = create_agent(client, name="Other", system_prompt="Be exact.")
    grant(client, other["id"], ["read_file"])
    grant_workspace(client, other["id"], workspace["id"])
    assert _read(client, other, workspace, "other.txt").json()["status"] == "confirmation_required"
    search = client.post(
        "/api/v1/tools/search_files/test",
        json={"arguments": {"workspace_id": workspace["id"], "query": "beta"}, "agent_id": agent["id"]},
    )
    assert search.json()["status"] == "confirmation_required"


def test_revoked_and_expired_approvals_stop_working(client, tmp_path):
    _folder, workspace, agent = _setup(client, tmp_path)
    created = _standing(client, agent_id=agent["id"], tool_name="read_file", workspace_id=workspace["id"]).json()
    assert client.delete(f"{URL}/{created['id']}").status_code == 204
    assert _read(client, agent, workspace, "other.txt").json()["status"] == "confirmation_required"
    assert client.delete(f"{URL}/{created['id']}").status_code == 404

    second = _standing(
        client, agent_id=agent["id"], tool_name="read_file", workspace_id=workspace["id"], expires_in_days=1
    ).json()
    assert _read(client, agent, workspace, "other.txt").json()["status"] == "ok"
    with Session(get_engine()) as session:
        row = session.get(StandingApprovalRecord, second["id"])
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    assert _read(client, agent, workspace, "other.txt").json()["status"] == "confirmation_required"


def test_always_ask_and_restricted_modes_are_not_overridden(client, tmp_path):
    _folder, workspace, agent = _setup(client, tmp_path)
    _standing(client, agent_id=agent["id"], tool_name="read_file", workspace_id=workspace["id"])
    _mode(client, "always_ask")
    assert _read(client, agent, workspace, "other.txt").json()["status"] == "confirmation_required"
    _mode(client, "restricted_auto")
    assert _read(client, agent, workspace, "other.txt").status_code == 403


def test_workspace_and_tool_grants_still_apply(client, tmp_path):
    _folder, workspace, agent = _setup(client, tmp_path)
    _standing(client, agent_id=agent["id"], tool_name="read_file", workspace_id=workspace["id"])
    grant(client, agent["id"], [])  # tool grant removed
    assert _read(client, agent, workspace, "other.txt").status_code == 403


def test_sensitive_names_stay_blocked(client, tmp_path):
    folder, workspace, agent = _setup(client, tmp_path)
    (folder / ".env").write_text("SECRET=1", encoding="utf-8")
    _standing(client, agent_id=agent["id"], tool_name="read_file", workspace_id=workspace["id"])
    body = _read(client, agent, workspace, ".env").json()
    assert body["status"] != "ok"
    assert "SECRET" not in str(body)


def test_changes_and_sensitive_tools_cannot_get_one(client, tmp_path):
    _folder, workspace, agent = _setup(client, tmp_path, tools=("read_file", "update_text_file"))
    for tool in ["update_text_file", "create_text_file", "quarantine_file", "ingest_document", "no_such_tool"]:
        response = _standing(client, agent_id=agent["id"], tool_name=tool, workspace_id=workspace["id"])
        assert response.status_code == 422, (tool, response.text)


def test_scope_must_fit_the_tool(client, tmp_path):
    _folder, workspace, agent = _setup(client, tmp_path)
    assert _standing(client, agent_id=agent["id"], tool_name="read_file").status_code == 422
    assert (
        _standing(client, agent_id=agent["id"], tool_name="research", workspace_id=workspace["id"]).status_code
        == 422
    )
    assert (
        _standing(
            client, agent_id=agent["id"], tool_name="read_file", workspace_id=workspace["id"], path_prefix="../x"
        ).status_code
        == 422
    )
    assert _standing(client, agent_id="not-an-id", tool_name="read_file", workspace_id=workspace["id"]).status_code in {
        404,
        422,
    }


def test_list_and_audit(client, tmp_path):
    _folder, workspace, agent = _setup(client, tmp_path)
    _standing(client, agent_id=agent["id"], tool_name="read_file", workspace_id=workspace["id"], path_prefix="docs")
    listed = client.get(URL, params={"agent_id": agent["id"]}).json()
    assert listed["count"] == 1
    assert listed["standing_approvals"][0]["path_prefix"] == "docs"
    assert listed["standing_approvals"][0]["active"] is True
    from app.permissions.models import PermissionAuditRecord

    with Session(get_engine()) as session:
        events = [row.event for row in session.query(PermissionAuditRecord).all()]
    assert "standing_approval_added" in events


def test_only_read_only_tools_are_eligible():
    """Every registered tool that could get a standing approval is a pure read."""
    from app.permissions.manifests import manifest_for_tool
    from app.permissions.standing import eligible
    from app.tools.registry import get_tool_registry

    tools = get_tool_registry().list_tools()
    assert tools
    names = set()
    for tool in tools:
        manifest = manifest_for_tool(tool)
        if eligible(manifest):
            names.add(tool.name)
            assert manifest.read_only and manifest.risk == "read"
            assert not {"create", "write", "modify", "delete", "execute_command"} & set(manifest.actions)
    assert "read_file" in names
    assert not names & {"create_text_file", "update_text_file", "quarantine_file", "ingest_document"}


def _facts(**overrides):
    values = dict(
        mode="ask_sensitive",
        agent_id="a",
        agent_found=True,
        agent_enabled=True,
        tool_name="read_file",
        tool_known=True,
        tool_enabled=True,
        tool_granted=True,
        risk="read",
        action="read",
        allowed_actions=("read",),
        resource_type="file",
        workspace_required=True,
        workspace_granted=True,
        path_allowed=True,
        internet=False,
        approval_required=True,
        standing_approved=True,
    )
    values.update(overrides)
    return PermissionFacts(**values)


def test_engine_only_relaxes_reads_in_ask_sensitive():
    assert decide(_facts()).decision == ALLOW
    assert decide(_facts()).code == "standing"
    assert decide(_facts(standing_approved=False)).decision == REQUIRES_APPROVAL
    assert decide(_facts(mode="always_ask")).decision == REQUIRES_APPROVAL
    assert decide(_facts(risk="sensitive", action="execute", allowed_actions=("execute",))).decision == REQUIRES_APPROVAL
    assert decide(_facts(action="write", allowed_actions=("write",))).decision != ALLOW
    assert decide(_facts(workspace_granted=False)).decision != ALLOW
    assert decide(_facts(path_allowed=False)).decision != ALLOW
    assert decide(_facts(tool_granted=False)).decision != ALLOW
