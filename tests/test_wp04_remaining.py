"""WP-04 leftovers: R-01 (EINTR in ChildProcess) and R-17 (configurable MCP limit)."""

import os
import subprocess
import sys

import pytest

from app.core.config import get_settings
from app.mcp import process as process_module
from app.mcp import session as session_module
from app.mcp.process import ChildProcess


def _child(pid: int) -> ChildProcess:
    return ChildProcess(
        pid=pid,
        pgid=pid,
        stdin_fd=-1,
        stdout_fd=-1,
        stderr_fd=-1,
        facts=None,
        generation=1,
    )


def _interrupt_first_wait(monkeypatch):
    real = os.waitpid
    calls = {"n": 0}

    def flaky(pid, flags):
        calls["n"] += 1
        if calls["n"] == 1:
            raise InterruptedError()
        return real(pid, flags)

    monkeypatch.setattr(process_module.os, "waitpid", flaky)
    return calls


def test_reap_retries_after_eintr(monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    child = _child(proc.pid)
    calls = _interrupt_first_wait(monkeypatch)
    assert child.reap(wait_seconds=5) is True
    assert child.reaped is True
    assert calls["n"] >= 2
    proc.returncode = 0  # already reaped by ChildProcess; keep Popen quiet


def test_alive_retries_after_eintr(monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    child = _child(proc.pid)
    try:
        calls = _interrupt_first_wait(monkeypatch)
        assert child.alive() is True
        assert calls["n"] == 2
    finally:
        monkeypatch.undo()
        proc.kill()
        proc.wait()
        child.reaped = True


def test_bounded_reap_still_honors_its_deadline_when_interrupted(monkeypatch):
    def always_interrupted(pid, flags):
        raise InterruptedError()

    monkeypatch.setattr(process_module.os, "waitpid", always_interrupted)
    child = _child(999_999)
    assert child.reap(wait_seconds=0.05) is False
    assert child.reaped is False


@pytest.fixture
def settings_env(monkeypatch):
    get_settings.cache_clear()
    session_module.reset_test_timing()
    yield monkeypatch
    get_settings.cache_clear()
    session_module.reset_test_timing()


def test_mcp_process_limit_defaults_to_two(settings_env):
    assert session_module._max_processes() == 2


def test_mcp_process_limit_comes_from_settings(settings_env):
    settings_env.setenv("MCP_MAX_PROCESSES", "5")
    get_settings.cache_clear()
    assert session_module._max_processes() == 5


def test_mcp_process_limit_rejects_out_of_range_values(settings_env):
    settings_env.setenv("MCP_MAX_PROCESSES", "0")
    get_settings.cache_clear()
    with pytest.raises(Exception):
        get_settings()


def test_explicit_test_timing_still_wins(settings_env):
    settings_env.setenv("MCP_MAX_PROCESSES", "5")
    get_settings.cache_clear()
    session_module.set_test_timing(max_processes=1)
    assert session_module._max_processes() == 1
