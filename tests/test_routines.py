"""Scheduled routines (phase 7, slice 1)."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.routines import is_due, next_due, run_due
from tests.test_agents import create_agent
from tests.test_tools import scripted_chat

TZ = ZoneInfo("Africa/Cairo")  # UTC+2 in January, no DST at the dates used


def _r(**kw):
    base = dict(time_of_day="08:00", days="", enabled=True, last_run_at=None,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    base.update(kw)
    return SimpleNamespace(**base)


def _at(day, hh, mm=0):  # local Cairo time on 2026-01-<day>
    return datetime(2026, 1, day, hh, mm, tzinfo=TZ)


def test_not_due_before_time_due_after_and_window():
    r = _r()
    assert not is_due(r, _at(10, 7, 59), TZ)
    assert is_due(r, _at(10, 8, 0), TZ)
    assert is_due(r, _at(10, 14, 0), TZ)
    assert not is_due(r, _at(10, 14, 1), TZ)  # catch-up window (6h) is over
    assert is_due(r, _at(10, 14, 1), TZ, catchup_hours=7)


def test_already_ran_and_disabled_and_new_routine():
    assert not is_due(_r(last_run_at=_at(10, 8, 5)), _at(10, 9), TZ)
    assert is_due(_r(last_run_at=_at(9, 8, 5)), _at(10, 9), TZ)  # yesterday's run does not count
    assert not is_due(_r(enabled=False), _at(10, 9), TZ)
    assert not is_due(_r(created_at=_at(10, 8, 30)), _at(10, 9), TZ)  # made after today's moment


def test_day_filter_monday_is_zero():
    jan12, jan13 = 12, 13  # Monday, Tuesday in 2026
    r = _r(days="0,2")
    assert is_due(r, _at(jan12, 9), TZ)
    assert not is_due(r, _at(jan13, 9), TZ)
    assert next_due(r, _at(jan13, 9), TZ) == _at(14, 8)  # Wednesday


def test_catchup_runs_once_not_backfill():
    r = _r()
    assert is_due(r, _at(10, 10), TZ)
    r.last_run_at = _at(10, 10)
    assert not is_due(r, _at(10, 11), TZ)
    # Two days asleep: only today's moment counts.
    r2 = _r(last_run_at=_at(7, 8, 1))
    assert is_due(r2, _at(10, 8, 30), TZ)
    assert next_due(r2, _at(10, 8, 30), TZ) == _at(11, 8)


def test_utc_input_is_converted():
    r = _r()
    assert is_due(r, datetime(2026, 1, 10, 6, 30, tzinfo=timezone.utc), TZ)  # 08:30 Cairo


def _make(client, **kw):
    body = {"name": "ملخص", "prompt": "قل مرحبا", "time_of_day": "08:00", "days": []}
    body.update(kw)
    response = client.post("/api/v1/routines", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_routes_crud_and_validation(client):
    routine = _make(client, days=[2, 0, 0])
    assert routine["days"] == [0, 2] and routine["enabled"] is True and routine["next_run_at"]
    assert client.post("/api/v1/routines", json={"name": "x", "prompt": "y", "time_of_day": "8:00"}).status_code == 422
    assert client.post("/api/v1/routines", json={"name": "x", "prompt": "y", "time_of_day": "24:00"}).status_code == 422
    assert client.post("/api/v1/routines", json={"name": "x", "prompt": "y", "time_of_day": "08:00", "days": [7]}).status_code == 422
    assert client.post("/api/v1/routines", json={"name": "x", "prompt": "y", "time_of_day": "08:00", "bogus": 1}).status_code == 422
    patched = client.patch(f"/api/v1/routines/{routine['id']}", json={"enabled": False, "time_of_day": "09:30", "days": []})
    assert patched.json()["enabled"] is False and patched.json()["time_of_day"] == "09:30" and patched.json()["days"] == []
    assert client.patch(f"/api/v1/routines/{routine['id']}", json={}).status_code == 422
    assert [r["id"] for r in client.get("/api/v1/routines").json()["routines"]] == [routine["id"]]
    assert client.delete(f"/api/v1/routines/{routine['id']}").status_code == 204
    assert client.delete(f"/api/v1/routines/{routine['id']}").status_code == 404
    assert client.post(f"/api/v1/routines/{routine['id']}/run").status_code == 404


def test_run_now_creates_titled_conversation(client, monkeypatch):
    prompts = []
    scripted_chat(monkeypatch, lambda item, calls: prompts.append(item["message"]) or "صباح الخير")
    routine = _make(client)
    done = client.post(f"/api/v1/routines/{routine['id']}/run").json()
    assert done["last_status"] == "ok" and done["last_error"] is None and done["last_run_at"]
    assert prompts == ["قل مرحبا"]
    conversation = client.get(f"/api/v1/conversations/{done['last_conversation_id']}").json()
    assert conversation["title"] == "⏰ ملخص"


def test_run_now_with_agent(client, monkeypatch):
    scripted_chat(monkeypatch, lambda item, calls: "ok")
    agent = create_agent(client)
    done = client.post(f"/api/v1/routines/{_make(client, agent_id=agent['id'])['id']}/run").json()
    assert done["last_status"] == "ok"
    assert client.get(f"/api/v1/conversations/{done['last_conversation_id']}").json()["agent_id"] == agent["id"]


def test_paused_is_refused(client, monkeypatch):
    scripted_chat(monkeypatch, lambda item, calls: "ok")
    assert client.post("/api/v1/control/pause", json={"reason": "t"}).status_code == 200
    done = client.post(f"/api/v1/routines/{_make(client)['id']}/run").json()
    assert done["last_status"] == "refused" and "paused" in done["last_error"]
    assert done["last_conversation_id"] is None


def test_model_error_is_recorded(client, monkeypatch):
    def boom(item, calls):
        raise RuntimeError("model down")

    scripted_chat(monkeypatch, boom)
    done = client.post(f"/api/v1/routines/{_make(client)['id']}/run").json()
    assert done["last_status"] == "error" and "model down" in done["last_error"]


def test_scheduler_runs_due_once(client, monkeypatch):
    calls = scripted_chat(monkeypatch, lambda item, calls: "ok")
    routine = _make(client, time_of_day="08:00")
    # Make the routine old enough to be due, then tick at 09:00 Cairo.
    from sqlalchemy.orm import Session

    from app.core.database import get_engine
    from app.routines import RoutineRecord

    with Session(get_engine()) as session:
        session.get(RoutineRecord, routine["id"]).created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        session.commit()
    now = _at(10, 9)
    first = asyncio.run(run_due(now))
    second = asyncio.run(run_due(now + timedelta(minutes=1)))
    assert len(first) == 1 and first[0]["last_status"] == "ok"
    assert second == [] and len(calls) == 1
