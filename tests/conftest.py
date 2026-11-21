import pytest
from fastapi.testclient import TestClient
from tests.client_support import LocalClient

from app.core.config import get_settings
from app.core.database import init_database, reset_database
from app.main import app


@pytest.fixture
def database_file(tmp_path, monkeypatch):
    path = tmp_path / "agents.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("STUDY_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("ROUTINE_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    reset_database()
    yield path
    reset_database()
    get_settings.cache_clear()


@pytest.fixture
def client(database_file):
    init_database()
    with LocalClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _fresh_controls():
    """Pause state and budget counters must not leak between tests."""
    from app.core.controls import reset_cache

    from app import style

    reset_cache()
    style.reset_cache()
    yield
    reset_cache()
    style.reset_cache()
