"""Folder ingest with one sealed approval (phase 3, slice 3)."""

from tests.test_knowledge import _prepare


def _propose(client, agent, workspace, **extra):
    arguments = {"workspace_id": workspace["id"], "scope": "project", **extra}
    return client.post("/api/v1/tools/ingest_folder/test", json={"arguments": arguments, "agent_id": agent["id"]})


def _setup(client, tmp_path, monkeypatch):
    from tests.test_tools import grant

    folder, workspace, agent, child = _prepare(client, tmp_path, monkeypatch)
    grant(client, agent["id"], ["ingest_folder", "search_knowledge", "get_document"])
    (folder / "docs").mkdir()
    (folder / "docs" / "a.md").write_text("# Prices\n\nThe widget price is 10 dollars.\n", encoding="utf-8")
    (folder / "docs" / "b.txt").write_text("Shipping takes three days.", encoding="utf-8")
    (folder / "docs" / "deep").mkdir()
    (folder / "docs" / "deep" / "c.py").write_text("def price():\n    return 10\n", encoding="utf-8")
    (folder / "docs" / ".env").write_text("SECRET=1", encoding="utf-8")
    (folder / "docs" / "key.pem").write_text("-----BEGIN-----", encoding="utf-8")
    (folder / "docs" / "tool.exe").write_bytes(b"MZ")
    return folder, workspace, agent


def test_one_approval_ingests_every_supported_file(client, tmp_path, monkeypatch):
    folder, workspace, agent = _setup(client, tmp_path, monkeypatch)
    proposed = _propose(client, agent, workspace, folder="docs")
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["status"] == "confirmation_required"
    approval_id = proposed.json()["output"]["approval_id"]
    approved = client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "COMPLETED", body
    output = body["output"]
    names = sorted(item["relative_path"] for item in output["files"])
    assert names == ["docs/a.md", "docs/b.txt", "docs/deep/c.py"]
    assert output["ready"] == 3 and output["skipped"] == 0
    found = client.post(
        "/api/v1/tools/search_knowledge/test",
        json={"arguments": {"query": "shipping days"}, "agent_id": agent["id"]},
    ).json()
    assert found["output"]["citations"][0]["filename"] == "b.txt"


def test_sensitive_and_unsupported_files_are_never_sealed(client, tmp_path, monkeypatch):
    _folder, workspace, agent = _setup(client, tmp_path, monkeypatch)
    proposed = _propose(client, agent, workspace, folder="docs").json()
    approved = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve").json()
    paths = [item["relative_path"] for item in approved["output"]["files"]]
    assert not any(p.endswith((".env", ".pem", ".exe")) for p in paths)


def test_a_file_changed_after_approval_is_skipped(client, tmp_path, monkeypatch):
    folder, workspace, agent = _setup(client, tmp_path, monkeypatch)
    proposed = _propose(client, agent, workspace, folder="docs").json()
    (folder / "docs" / "b.txt").write_text("Changed after the approval!", encoding="utf-8")
    approved = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve").json()
    by_path = {item["relative_path"]: item for item in approved["output"]["files"]}
    assert by_path["docs/b.txt"]["status"] == "skipped"
    assert by_path["docs/a.md"]["status"] == "ready"
    assert approved["output"]["ready"] == 2


def test_a_file_added_after_approval_is_not_ingested(client, tmp_path, monkeypatch):
    folder, workspace, agent = _setup(client, tmp_path, monkeypatch)
    proposed = _propose(client, agent, workspace, folder="docs").json()
    (folder / "docs" / "late.txt").write_text("Added later.", encoding="utf-8")
    approved = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve").json()
    assert "docs/late.txt" not in [item["relative_path"] for item in approved["output"]["files"]]


def test_non_recursive_and_max_files(client, tmp_path, monkeypatch):
    _folder, workspace, agent = _setup(client, tmp_path, monkeypatch)
    proposed = _propose(client, agent, workspace, folder="docs", recursive=False, max_files=1).json()
    approved = client.post(f"/api/v1/approvals/{proposed['output']['approval_id']}/approve").json()
    paths = [item["relative_path"] for item in approved["output"]["files"]]
    assert paths == ["docs/a.md"]


def test_empty_or_ungranted_folder_is_refused(client, tmp_path, monkeypatch):
    folder, workspace, agent = _setup(client, tmp_path, monkeypatch)
    (folder / "empty").mkdir()
    assert _propose(client, agent, workspace, folder="empty").status_code in {400, 403, 409, 422}
    from tests.test_agents import create_agent
    from tests.test_tools import grant

    stranger = create_agent(client, name="Stranger", system_prompt="x")
    grant(client, stranger["id"], ["ingest_folder"])
    assert _propose(client, stranger, workspace, folder="docs").status_code in {400, 403, 409, 422}


def test_ingest_folder_needs_approval_every_time(client):
    from app.permissions.manifests import manifest_for_tool
    from app.permissions.standing import eligible
    from app.tools.registry import get_tool_registry

    manifest = manifest_for_tool(get_tool_registry().get("ingest_folder"))
    assert manifest.risk == "sensitive" and manifest.approval_required
    assert eligible(manifest) is False
