"""WP-05A: local control-plane authentication."""

import os
import stat

import pytest
from fastapi.testclient import TestClient

from app.control_plane import main as cli
from app.core.auth import (
    LOCK_NAME,
    TOKEN_NAME,
    AuthStoreError,
    ServeLease,
    ServerRunningError,
    bearer_matches,
    data_dir,
    read_token,
    rotate,
)
from app.main import app
from tests.client_support import LocalClient

LOCAL = "http://127.0.0.1"


@pytest.fixture
def raw(database_file):
    """Started app, loopback host, NO Authorization header."""
    with TestClient(app, base_url=LOCAL) as test_client:
        yield test_client


def _token() -> str:
    return app.state.auth_token


def test_health_is_public_and_everything_else_needs_the_token(raw):
    assert raw.get("/api/v1/health").status_code == 200
    for method, path in [
        ("GET", "/api/v1/agents"),
        ("POST", "/api/v1/chat"),
        ("GET", "/api/v1/permissions"),
        ("PUT", "/api/v1/permissions/mode"),
        ("GET", "/api/v1/approvals"),
        ("GET", "/api/v1/models"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/does-not-exist"),
        ("POST", "/api/v1/health"),
    ]:
        response = raw.request(method, path)
        assert response.status_code == 401, (method, path, response.status_code)
        assert response.headers["www-authenticate"] == "Bearer"


def test_public_match_is_exact_not_a_prefix(raw):
    for path in ["/api/v1/health/", "/api/v1//health", "/api/v1/health/../agents", "/API/V1/HEALTH"]:
        assert raw.get(path).status_code == 401, path


def test_valid_token_is_accepted_and_wrong_ones_are_not(raw):
    token = _token()
    assert raw.get("/api/v1/agents", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert raw.get("/api/v1/agents", headers={"Authorization": f"bearer {token}"}).status_code == 200
    wrong = "A" * 43 if token != "A" * 43 else "B" * 43
    for header in [f"Bearer {wrong}", token, f"Basic {token}", f"Bearer {token} ", f"Bearer {token[:-1]}", ""]:
        response = raw.get("/api/v1/agents", headers={"Authorization": header})
        assert response.status_code == 401, header


def test_duplicate_authorization_headers_are_rejected(raw):
    token = _token()
    response = raw.get(
        "/api/v1/agents",
        headers=[("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")],
    )
    assert response.status_code == 401


def test_non_loopback_host_is_refused_even_with_the_token(raw):
    token = _token()
    for host in ["evil.example", "127.0.0.1.evil.example", "192.168.1.5:8000", "localhost.evil"]:
        response = raw.get("/api/v1/agents", headers={"Host": host, "Authorization": f"Bearer {token}"})
        assert response.status_code == 421, host
        assert raw.get("/api/v1/health", headers={"Host": host}).status_code == 421, host
    for host in ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "LOCALHOST"]:
        response = raw.get("/api/v1/agents", headers={"Host": host, "Authorization": f"Bearer {token}"})
        assert response.status_code == 200, host


def test_cross_site_origin_is_refused(raw):
    token = _token()
    auth = {"Authorization": f"Bearer {token}"}
    for origin in ["https://evil.example", "null", "http://127.0.0.1.evil.example", "file://"]:
        response = raw.get("/api/v1/agents", headers={**auth, "Origin": origin})
        assert response.status_code == 403, origin
    assert raw.get("/api/v1/agents", headers={**auth, "Origin": "http://localhost:4200"}).status_code == 200


def test_token_store_is_private(client):
    folder = data_dir()
    token_path = folder / TOKEN_NAME
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(token_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(folder / LOCK_NAME).st_mode) == 0o600
    token = read_token()
    assert len(token) == 43
    assert token == _token()


def test_rotate_is_refused_while_the_server_runs_and_works_after(database_file):
    with LocalClient(app) as first:
        old = _token()
        with pytest.raises(ServerRunningError):
            rotate()
        assert read_token() == old
        assert first.get("/api/v1/agents").status_code == 200
    new = rotate()
    assert new != old
    with TestClient(app, base_url=LOCAL) as second:
        assert second.get("/api/v1/agents", headers={"Authorization": f"Bearer {old}"}).status_code == 401
        assert second.get("/api/v1/agents", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_token_is_stable_across_restarts(database_file):
    with LocalClient(app):
        first = _token()
    with LocalClient(app):
        assert _token() == first


def test_unsafe_token_file_stops_startup(database_file):
    with LocalClient(app):
        pass
    path = data_dir() / TOKEN_NAME
    os.chmod(path, 0o644)
    with pytest.raises(AuthStoreError):
        ServeLease.acquire()
    os.chmod(path, 0o600)
    path.write_text("short", encoding="ascii")
    with pytest.raises(AuthStoreError):
        ServeLease.acquire()


def test_symlinked_token_file_is_refused(database_file, tmp_path):
    with LocalClient(app):
        pass
    path = data_dir() / TOKEN_NAME
    target = tmp_path / "elsewhere"
    target.write_text(path.read_text(encoding="ascii"), encoding="ascii")
    os.chmod(target, 0o600)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises((AuthStoreError, OSError)):
        read_token()


def test_file_tools_cannot_read_the_token():
    from app.files.changes import _BLOCKED_NAMES

    assert TOKEN_NAME in _BLOCKED_NAMES
    assert LOCK_NAME in _BLOCKED_NAMES


def test_bearer_compare_rejects_malformed_values():
    token = "A" * 43
    assert bearer_matches(f"Bearer {token}", token)
    assert not bearer_matches(f"Bearer {token}x", token)
    assert not bearer_matches("Bearer ", token)
    assert not bearer_matches(f"Bearer {'é' * 43}", token)


def test_cli_prints_token_and_refuses_rotate_while_serving(database_file, capsys):
    with LocalClient(app):
        assert cli(["token"]) == 0
        assert capsys.readouterr().out.strip() == _token()
        assert cli(["rotate"]) == 2
    assert cli(["rotate"]) == 0
    assert cli(["nonsense"]) == 64
