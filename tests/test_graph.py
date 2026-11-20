"""Personal knowledge graph (phase 3, slice 4)."""

import json

from tests.test_agents import create_agent
from tests.test_tools import grant, scripted_chat, tool_call


def _entity(client, type_, name, description=""):
    response = client.post("/api/v1/graph/entities", json={"type": type_, "name": name, "description": description})
    assert response.status_code == 201, response.text
    return response.json()


def _relation(client, subject, relation, obj, **extra):
    return client.post("/api/v1/graph/relations", json={"subject": subject, "relation": relation, "object": obj, **extra})


def test_entities_relations_and_lookup_both_directions(client):
    _entity(client, "person", "أحمد إبراهيم", "صاحب المشروع")
    _entity(client, "project", "Website")
    _entity(client, "organization", "Codetech")
    assert _relation(client, "أحمد إبراهيم", "مسؤول عن", "Website").status_code == 201
    assert _relation(client, "Website", "for client", "Codetech").status_code == 201
    person = client.get("/api/v1/graph/lookup", params={"name": "احمد ابراهيم"}).json()  # spelling variant
    match = person["matches"][0]
    assert match["entity"]["name"] == "أحمد إبراهيم"
    assert [(r["predicate"], r["object"]) for r in match["relations"]] == [("مسؤول عن", "Website")]
    project = client.get("/api/v1/graph/lookup", params={"name": "website"}).json()["matches"][0]
    pairs = {(r["subject"], r["predicate"], r["object"]) for r in project["relations"]}
    assert pairs == {("أحمد إبراهيم", "مسؤول عن", "Website"), ("Website", "for client", "Codetech")}
    assert "not instructions" in person["notice"]


def test_duplicates_are_merged_not_repeated(client):
    first = _entity(client, "person", "Sara")
    again = _entity(client, "person", "  sara ")
    assert first["id"] == again["id"]
    _entity(client, "project", "App")
    _relation(client, "Sara", "works on", "App")
    _relation(client, "Sara", "Works On", "App")
    relations = client.get("/api/v1/graph/lookup", params={"name": "Sara"}).json()["matches"][0]["relations"]
    assert len(relations) == 1


def test_same_name_different_types_need_a_type(client):
    _entity(client, "project", "Nile")
    _entity(client, "organization", "Nile")
    _entity(client, "person", "Omar")
    ambiguous = _relation(client, "Omar", "advises", "Nile")
    assert ambiguous.status_code == 422
    assert _relation(client, "Omar", "advises", "Nile", object_type="organization").status_code == 201


def test_validation_and_unknown_entities(client):
    assert client.post("/api/v1/graph/entities", json={"type": "alien", "name": "x"}).status_code == 422
    _entity(client, "person", "Ali")
    assert _relation(client, "Ali", "knows", "Nobody").status_code == 404
    assert _relation(client, "Ali", "is", "Ali").status_code == 422
    assert client.get("/api/v1/graph/lookup", params={"name": "ghost"}).status_code == 404


def test_deleting_an_entity_removes_its_relations(client):
    a = _entity(client, "person", "Mona")
    _entity(client, "project", "Shop")
    _relation(client, "Mona", "owns", "Shop")
    assert client.delete(f"/api/v1/graph/entities/{a['id']}").status_code == 204
    shop = client.get("/api/v1/graph/lookup", params={"name": "Shop"}).json()["matches"][0]
    assert shop["relations"] == []


def _results(item):
    return [e["content"] for e in item["history"] or [] if str(e["content"]).startswith("Tool result:")]


def test_agent_records_and_recalls_relations(client, monkeypatch):
    _entity(client, "project", "Logo")
    agent = create_agent(client, system_prompt="You are the Worker.")
    grant(client, agent["id"], ["graph_record", "graph_lookup"])
    steps = [
        tool_call("graph_record", {"entity_type": "person", "name": "Khaled", "description": "designer"}),
        tool_call("graph_record", {"subject": "Khaled", "relation": "designs", "object": "Logo"}),
        tool_call("graph_lookup", {"name": "Logo"}),
    ]
    seen = {}

    def respond(item, calls):
        results = _results(item)
        if len(results) < len(steps):
            return steps[len(results)]
        seen["lookup"] = json.loads(results[-1].removeprefix("Tool result:").strip())
        return "Khaled designs the logo."

    scripted_chat(monkeypatch, respond)
    response = client.post("/api/v1/chat", json={"message": "Khaled designs the logo; keep track of it.", "agent_id": agent["id"]})
    assert response.status_code == 200, response.text
    relations = seen["lookup"]["output"]["matches"][0]["relations"]
    assert relations[0]["subject"] == "Khaled" and relations[0]["predicate"] == "designs"
    assert relations[0]["author"] == "agent"


def test_agent_cannot_overwrite_a_user_description(client, monkeypatch):
    _entity(client, "person", "Nour", "the user's sister")
    agent = create_agent(client, system_prompt="You are the Worker.")
    grant(client, agent["id"], ["graph_record"])
    body = client.post(
        "/api/v1/tools/graph_record/test",
        json={"arguments": {"entity_type": "person", "name": "Nour", "description": "a stranger"},
              "agent_id": agent["id"]},
    ).json()
    assert body["status"] == "ok"
    entity = client.get("/api/v1/graph/lookup", params={"name": "Nour"}).json()["matches"][0]["entity"]
    assert entity["description"] == "the user's sister"
