"""
Use-case / integration tests for the FastAPI web layer (web_app.py).

These exercise the real ASGI app end-to-end (routing, request validation,
auth middleware, response shaping) while the BLE ``Mower`` is replaced by an
``AsyncMock`` — so no Bluetooth hardware is required.
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient


# ─── Connection guard ─────────────────────────────────────────────────────────
class TestConnectionGuard:
    def test_status_requires_connection(self, client):
        assert client.get("/api/status").status_code == 400

    def test_statistics_requires_connection(self, client):
        assert client.get("/api/statistics").status_code == 400

    def test_schedule_requires_connection(self, client):
        assert client.get("/api/schedule").status_code == 400


# ─── Status use case ──────────────────────────────────────────────────────────
class TestStatus:
    def test_reports_mowing(self, connected_client):
        r = connected_client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is True
        assert body["activity"] == "MOWING"
        assert body["activity_description"] == "Mowing lawn"
        assert body["battery_level"] == 80
        assert body["charging"] is False
        assert body["model"] == "Automower 305"
        assert body["control_mode"] == "classic"

    def test_charging_adds_estimated_next_start(self, web, connected_client):
        web._mower.is_charging.return_value = True

        async def _cmd(name, **kw):
            if name == "GetRemainingChargingTime":
                return 3600
            return {
                "GetUserMowerNameAsAsciiString": "Sir Schnittalot",
                "GetSerialNumber": 1,
                "GetError": 0,
                "GetRestrictionReason": 0,
            }.get(name)

        web._mower.command.side_effect = _cmd
        body = connected_client.get("/api/status").json()
        assert body["charging"] is True
        assert body["remaining_charging_min"] == 60.0
        assert body["estimated_next_start_time"] is not None


# ─── Connection lifecycle ─────────────────────────────────────────────────────
class TestConnection:
    def test_connection_status_disconnected(self, client):
        body = client.get("/api/connection").json()
        assert body["connected"] is False
        assert body["address"] is None

    def test_connection_status_connected(self, connected_client):
        body = connected_client.get("/api/connection").json()
        assert body["connected"] is True
        assert body["address"] == "AA:BB:CC:DD:EE:FF"

    def test_stale_connection_is_cleaned_up(self, web, connected_client, monkeypatch):
        # Simulate a BLE drop that bypassed /api/disconnect.
        web._mower.is_connected.return_value = False
        monkeypatch.setattr(web.subprocess, "run", lambda *a, **k: None)
        body = connected_client.get("/api/connection").json()
        assert body["connected"] is False
        assert web._connected is False

    def test_disconnect(self, web, connected_client):
        r = connected_client.post("/api/disconnect")
        assert r.status_code == 200
        assert r.json()["status"] == "disconnected"
        web._mower_was = None  # mower is cleared
        assert web._connected is False


# ─── Commands: classic mode ───────────────────────────────────────────────────
class TestClassicCommands:
    def test_mow_calls_override(self, connected_client, web):
        r = connected_client.post("/api/command/mow", json={"duration_hours": 2})
        assert r.status_code == 200
        body = r.json()
        assert body["control_mode"] == "classic"
        web._mower.mower_override.assert_awaited_once_with(2)

    def test_mow_rejects_zero_duration(self, connected_client):
        r = connected_client.post("/api/command/mow", json={"duration_hours": 0})
        assert r.status_code == 422

    def test_pause(self, connected_client, web):
        r = connected_client.post("/api/command/pause")
        assert r.status_code == 200
        web._mower.mower_pause.assert_awaited_once()

    def test_resume(self, connected_client, web):
        r = connected_client.post("/api/command/resume")
        assert r.status_code == 200
        web._mower.mower_resume.assert_awaited_once()

    def test_park(self, connected_client, web):
        r = connected_client.post("/api/command/park")
        assert r.status_code == 200
        assert r.json()["action"] == "park_until_next_start"
        web._mower.mower_park.assert_awaited_once()

    def test_park_home(self, connected_client, web):
        r = connected_client.post("/api/command/park_home")
        assert r.status_code == 200
        web._mower.mower_park_home.assert_awaited_once()


# ─── Commands: smart (planner) mode ───────────────────────────────────────────
@pytest.fixture
def smart(web, monkeypatch):
    """Switch the app into smart/planner mode and reset planner runtime state."""
    monkeypatch.setattr(web, "planner_load_config", lambda: {"enabled": True})
    web.planner._user_inhibited = False
    web.planner._active_session = None
    return web.planner


class TestSmartCommands:
    def test_mow_sets_synthetic_session_and_clears_inhibit(
        self, connected_client, web, smart
    ):
        smart._user_inhibited = True
        r = connected_client.post("/api/command/mow", json={"duration_hours": 1.5})
        assert r.status_code == 200
        assert r.json()["control_mode"] == "smart"
        assert smart._user_inhibited is False
        assert smart._active_session is not None
        assert smart._active_session["dow_name"] == "Manual"
        assert smart._active_session["duration_sec"] == int(1.5 * 3600)

    def test_pause_sets_user_inhibit(self, connected_client, web, smart):
        r = connected_client.post("/api/command/pause")
        assert r.status_code == 200
        assert smart._user_inhibited is True

    def test_resume_clears_user_inhibit(self, connected_client, web, smart):
        smart._user_inhibited = True
        r = connected_client.post("/api/command/resume")
        assert r.status_code == 200
        assert smart._user_inhibited is False

    def test_park_no_active_session_is_noop(self, connected_client, web, smart):
        r = connected_client.post("/api/command/park")
        assert r.status_code == 200
        assert r.json()["action"] == "no_active_session"
        web._mower.mower_park_home.assert_not_awaited()

    def test_park_cancels_active_session(self, connected_client, web, smart):
        import datetime as dt

        smart._active_session = {
            "date": str(dt.date.today()),
            "dow_name": "Monday",
            "start_dt": dt.datetime.now(),
            "duration_sec": 3600,
        }
        r = connected_client.post("/api/command/park")
        assert r.status_code == 200
        assert r.json()["action"] == "session_cancelled"
        assert smart._active_session is None
        web._mower.mower_park_home.assert_awaited_once()

    def test_park_home_inhibits_and_clears_session(self, connected_client, web, smart):
        smart._active_session = {"x": 1}
        r = connected_client.post("/api/command/park_home")
        assert r.status_code == 200
        assert smart._user_inhibited is True
        assert smart._active_session is None


# ─── Schedule ─────────────────────────────────────────────────────────────────
class TestSchedule:
    def test_set_schedule_blocked_in_planner_mode(self, connected_client, web, monkeypatch):
        monkeypatch.setattr(web, "planner_load_config", lambda: {"enabled": True})
        r = connected_client.post("/api/schedule", json={"tasks": []})
        assert r.status_code == 409

    def test_set_schedule_classic(self, connected_client, web):
        payload = {
            "tasks": [
                {
                    "start_seconds": 36000,
                    "duration_seconds": 7200,
                    "monday": True,
                    "wednesday": True,
                }
            ]
        }
        r = connected_client.post("/api/schedule", json=payload)
        assert r.status_code == 200
        assert r.json()["tasks_set"] == 1
        web._mower.set_schedule.assert_awaited_once()

    def test_get_schedule_empty(self, connected_client):
        body = connected_client.get("/api/schedule").json()
        assert body["task_count"] == 0
        assert body["tasks"] == []


# ─── Statistics ───────────────────────────────────────────────────────────────
class TestStatistics:
    def test_derived_metrics(self, connected_client):
        body = connected_client.get("/api/statistics").json()
        assert body["total_running_hours"] == 100.0
        assert body["number_of_charging_cycles"] == 40
        # cutting_ratio = 300000/360000*100
        assert body["cutting_ratio_pct"] == pytest.approx(83.3, abs=0.1)
        assert body["blade_wear_pct"] == pytest.approx(25.0, abs=0.1)


# ─── Runtime estimate ─────────────────────────────────────────────────────────
class TestRuntimeEstimate:
    def test_rejects_zero_duration(self, connected_client):
        assert connected_client.get("/api/runtime_estimate?duration_hours=0").status_code == 422

    def test_falls_back_to_statistics(self, connected_client):
        body = connected_client.get("/api/runtime_estimate?duration_hours=3").json()
        # No samples loaded → statistics-based source.
        assert body["data_source"] in ("statistics", "insufficient_data")

    def test_clear_samples(self, connected_client):
        r = connected_client.delete("/api/runtime_samples")
        assert r.status_code == 200
        assert r.json()["sample_count"] == 0


# ─── Auth ─────────────────────────────────────────────────────────────────────
class TestAuth:
    @pytest.fixture
    def secured(self, web):
        web._auth_hash = bcrypt.hashpw(b"hunter2", bcrypt.gensalt())
        return TestClient(web.app)

    def test_api_requires_auth(self, secured):
        # follow_redirects off so the 401 from middleware is observed directly.
        r = secured.get("/api/connection")
        assert r.status_code == 401

    def test_page_redirects_to_login(self, secured):
        r = secured.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_login_wrong_password(self, secured):
        r = secured.post("/login", data={"password": "nope"}, follow_redirects=False)
        assert r.status_code == 401
        assert "Incorrect password" in r.text

    def test_login_success_sets_cookie(self, web, secured):
        r = secured.post("/login", data={"password": "hunter2"}, follow_redirects=False)
        assert r.status_code == 302
        assert web._SESSION_COOKIE in r.cookies

    def test_login_rate_limited(self, web, secured):
        # Exhaust the limiter then assert the next attempt is throttled.
        web._login_attempts.clear()
        for _ in range(web._LOGIN_MAX_ATTEMPTS):
            secured.post("/login", data={"password": "x"}, follow_redirects=False)
        r = secured.post("/login", data={"password": "x"}, follow_redirects=False)
        assert r.status_code == 429


# ─── HTML page rendering (smoke) ──────────────────────────────────────────────
class TestPagesRender:
    def test_index_renders(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Automower BLE Control" in r.text
        assert 'id="nav-conn-badge"' in r.text

    def test_login_page_renders(self, web):
        web._auth_hash = bcrypt.hashpw(b"pw", bcrypt.gensalt())
        c = TestClient(web.app)
        r = c.get("/login")
        assert r.status_code == 200
        assert "Sign in to continue" in r.text
