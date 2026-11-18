def test_health_endpoint(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_chat_route_still_validates_input(client):
    response = client.post("/api/v1/chat", json={})
    assert response.status_code == 422


def test_existing_routes_remain_registered(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/ollama/health" in paths
    assert "/api/v1/models" in paths
    assert "/api/v1/chat" in paths
