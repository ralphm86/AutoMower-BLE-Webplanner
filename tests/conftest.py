"""
Shared pytest fixtures for the AutoMower-BLE test suite.

The web-app and planner tests never touch real Bluetooth hardware: the BLE
``Mower`` object is replaced with an ``AsyncMock`` and the FastAPI app is driven
through Starlette's ``TestClient`` (in-process ASGI, no network).
"""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── BLE mower double ─────────────────────────────────────────────────────────
@pytest.fixture
def fake_mower():
    """A fully stubbed Mower whose every coroutine returns canned data."""
    from automower_ble.protocol import MowerState, MowerActivity

    m = MagicMock(name="FakeMower")
    m.address = "AA:BB:CC:DD:EE:FF"
    m.is_connected = MagicMock(return_value=True)

    # Typed status coroutines
    m.mower_state = AsyncMock(return_value=MowerState.IN_OPERATION)
    m.mower_activity = AsyncMock(return_value=MowerActivity.MOWING)
    m.mower_mode = AsyncMock(return_value=None)
    m.mower_next_start_time = AsyncMock(return_value=None)
    m.battery_level = AsyncMock(return_value=80)
    m.is_charging = AsyncMock(return_value=False)
    m.get_manufacturer = AsyncMock(return_value="Husqvarna")
    m.get_model = AsyncMock(return_value="Automower 305")
    m.get_supports_cutting_height = AsyncMock(return_value=False)

    # Command coroutines (write/no-op)
    m.set_time = AsyncMock()
    m.disconnect = AsyncMock()
    m.connect = AsyncMock()
    m.mower_override = AsyncMock()
    m.mower_override_seconds = AsyncMock()
    m.mower_pause = AsyncMock()
    m.mower_resume = AsyncMock()
    m.mower_park = AsyncMock()
    m.mower_park_home = AsyncMock()
    m.mower_park_duration = AsyncMock()
    m.set_schedule = AsyncMock()
    m.get_task = AsyncMock(return_value=None)

    # Generic command() dispatcher used by /api/status, /api/statistics, …
    _canned = {
        "GetUserMowerNameAsAsciiString": "Sir Schnittalot",
        "GetSerialNumber": 123456,
        "GetError": 0,
        "GetRestrictionReason": 0,
        "GetNumberOfTasks": 0,
        "GetNumberOfMessages": 0,
        "GetRemainingChargingTime": None,
        "GetOverride": {"action": 0, "duration": 0},
        "GetAllStatistics": {
            "totalRunningTime": 360000,
            "totalCuttingTime": 300000,
            "totalChargingTime": 50000,
            "totalSearchingTime": 10000,
            "numberOfCollisions": 120,
            "numberOfChargingCycles": 40,
            "cuttingBladeUsageTime": 180000,
        },
    }

    async def _command(name, **kwargs):
        return _canned.get(name)

    m.command = AsyncMock(side_effect=_command)
    return m


# ─── FastAPI test client ──────────────────────────────────────────────────────
@pytest.fixture
def web(monkeypatch):
    """
    Import web_app and return the module with global state reset and auth off.

    Tests mutate ``web.module`` globals (``_mower``, ``_connected`` …) and read
    them back; the fixture guarantees a clean slate per test.
    """
    import web_app

    # Disable auth so API routes are reachable without a session cookie.
    monkeypatch.setattr(web_app, "_auth_hash", None, raising=False)

    # Default planner config = classic mode (planner disabled). Individual
    # tests override this via monkeypatch when they need smart mode.
    monkeypatch.setattr(web_app, "planner_load_config", lambda: {"enabled": False})

    # Reset connection-related globals.
    monkeypatch.setattr(web_app, "_mower", None, raising=False)
    monkeypatch.setattr(web_app, "_connected", False, raising=False)
    monkeypatch.setattr(web_app, "_reconnect_cfg", None, raising=False)
    monkeypatch.setattr(web_app, "_reconnect_enabled", False, raising=False)
    monkeypatch.setattr(web_app, "_server_start_time", dt.datetime.now(), raising=False)

    return web_app


@pytest.fixture
def client(web):
    """A TestClient that does NOT trigger the lifespan (no background BLE tasks)."""
    from fastapi.testclient import TestClient

    # Instantiating without the `with` block means lifespan startup/shutdown
    # (and therefore the reconnect/sampler/idle loops) never run.
    return TestClient(web.app)


@pytest.fixture
def connected_client(web, client, fake_mower):
    """A TestClient with a fake mower already connected."""
    web._mower = fake_mower
    web._connected = True
    return client
