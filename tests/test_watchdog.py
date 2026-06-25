"""
Unit tests for WeatherWatchdog and its coordination with the planner's
session executor (planner.py).

These focus on the watchdog ↔ executor interaction fixes:
  * Fix 1 — no unplanned mowing after a session window has ended.
  * Fix 2 — schedule-aware resume (re-issue override, not ClearOverride).
  * Fix 3 — respect a user park (_user_inhibited).

All BLE and network access is mocked.
"""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

import planner
from planner import WeatherWatchdog
from automower_ble.protocol import MowerActivity


# ─── Fixtures / helpers ───────────────────────────────────────────────────────
@pytest.fixture
def patch_config(monkeypatch):
    """Return a setter that installs a config dict for load_config()."""
    def _set(**over):
        cfg = {
            "location_lat": 0.0,
            "location_lon": 0.0,
            "max_rain_mm_h": 0.5,
            "max_wind_speed_ms": 10.0,
            "watchdog_enabled": True,
            "enabled": True,  # smart mode on by default
        }
        cfg.update(over)
        monkeypatch.setattr(planner, "load_config", lambda: cfg)
        return cfg
    return _set


def _patch_weather(monkeypatch, *, rain=0.0, wind=0.0, wmo=0):
    async def _fake(lat, lon):
        return {"current": {"precipitation": rain, "windspeed_10m": wind,
                            "weathercode": wmo}}
    monkeypatch.setattr(planner, "fetch_current_weather", _fake)


def _make_mower(activity=MowerActivity.MOWING, connected=True):
    m = MagicMock()
    m.is_connected.return_value = connected
    m.mower_activity = AsyncMock(return_value=activity)
    m.mower_park_home = AsyncMock()
    m.mower_resume = AsyncMock()
    m.mower_override_seconds = AsyncMock()
    return m


def _session(start_offset_min, duration_min):
    """Build a planned-session dict starting start_offset_min from now."""
    start = dt.datetime.now() + dt.timedelta(minutes=start_offset_min)
    return {
        "date": start.date().isoformat(),
        "start_dt": start,
        "duration_sec": duration_min * 60,
        "dow_name": "Mon",
    }


def _make_planner(active=None, inhibited=False):
    p = MagicMock()
    p._active_session = active
    p._user_inhibited = inhibited
    p.run_once = AsyncMock(return_value="replanned")
    return p


def _wire(mower=None, planner_obj=None):
    wd = WeatherWatchdog()
    if mower is not None:
        wd.set_mower_provider(lambda: mower)
    if planner_obj is not None:
        wd.set_planner_provider(lambda: planner_obj)
    return wd


# ─── Park behaviour ───────────────────────────────────────────────────────────
async def test_parks_on_thunderstorm_while_mowing(patch_config, monkeypatch):
    patch_config()
    _patch_weather(monkeypatch, wmo=95)  # thunderstorm
    mower = _make_mower(activity=MowerActivity.MOWING)
    wd = _wire(mower, _make_planner())

    await wd.check_once()

    mower.mower_park_home.assert_awaited_once()
    assert wd._parked_by_watchdog is True


async def test_does_not_park_when_already_home(patch_config, monkeypatch):
    patch_config()
    _patch_weather(monkeypatch, rain=5.0)  # heavy rain
    mower = _make_mower(activity=MowerActivity.PARKED)
    wd = _wire(mower, _make_planner())

    await wd.check_once()

    mower.mower_park_home.assert_not_awaited()
    assert wd._parked_by_watchdog is False


# ─── Fix 1: no unplanned mowing after the session window ──────────────────────
async def test_no_resume_when_no_active_session(patch_config, monkeypatch):
    """Weather clears but no active session -> replan instead of mowing."""
    patch_config()
    _patch_weather(monkeypatch)  # good weather
    mower = _make_mower(activity=MowerActivity.MOWING)
    plan = _make_planner(active=None)
    wd = _wire(mower, plan)
    wd._parked_by_watchdog = True

    status = await wd.check_once()

    mower.mower_resume.assert_not_awaited()
    mower.mower_override_seconds.assert_not_awaited()
    plan.run_once.assert_awaited_once()
    assert wd._parked_by_watchdog is False
    assert "replanned" in status


async def test_no_resume_when_active_session_expired(patch_config, monkeypatch):
    """Active session whose window already passed -> replan, no direct restart."""
    patch_config()
    _patch_weather(monkeypatch)
    mower = _make_mower(activity=MowerActivity.MOWING)
    expired = _session(start_offset_min=-120, duration_min=30)  # ended 90 min ago
    plan = _make_planner(active=expired)
    wd = _wire(mower, plan)
    wd._parked_by_watchdog = True

    await wd.check_once()

    mower.mower_resume.assert_not_awaited()
    mower.mower_override_seconds.assert_not_awaited()
    plan.run_once.assert_awaited_once()
    assert wd._parked_by_watchdog is False


# ─── Fix 2: schedule-aware resume ─────────────────────────────────────────────
async def test_resume_reissues_override_for_active_session(patch_config, monkeypatch):
    """Weather clears mid-window -> re-issue override, not ClearOverride."""
    patch_config()
    _patch_weather(monkeypatch)
    mower = _make_mower(activity=MowerActivity.MOWING)
    active = _session(start_offset_min=-10, duration_min=60)  # 50 min remaining
    wd = _wire(mower, _make_planner(active=active))
    wd._parked_by_watchdog = True

    await wd.check_once()

    mower.mower_resume.assert_not_awaited()
    mower.mower_override_seconds.assert_awaited_once()
    remaining = mower.mower_override_seconds.await_args.args[0]
    assert 0 < remaining <= 60 * 60
    assert wd._parked_by_watchdog is False


# ─── Fix 3: respect user park ─────────────────────────────────────────────────
async def test_does_not_resume_when_user_inhibited(patch_config, monkeypatch):
    patch_config()
    _patch_weather(monkeypatch)
    mower = _make_mower(activity=MowerActivity.MOWING)
    active = _session(start_offset_min=-10, duration_min=60)
    wd = _wire(mower, _make_planner(active=active, inhibited=True))
    wd._parked_by_watchdog = True

    status = await wd.check_once()

    mower.mower_resume.assert_not_awaited()
    mower.mower_override_seconds.assert_not_awaited()
    assert wd._parked_by_watchdog is False
    assert "parked by user" in status


# ─── Classic mode still uses mower_resume() ───────────────────────────────────
async def test_classic_mode_uses_mower_resume(patch_config, monkeypatch):
    patch_config(enabled=False)  # smart mode off
    _patch_weather(monkeypatch)
    mower = _make_mower(activity=MowerActivity.PARKED)
    wd = _wire(mower, _make_planner())
    wd._parked_by_watchdog = True

    await wd.check_once()

    mower.mower_resume.assert_awaited_once()
    mower.mower_override_seconds.assert_not_awaited()
    assert wd._parked_by_watchdog is False


# ─── Drying delay ─────────────────────────────────────────────────────────────
async def test_drying_delay_holds_before_resume(patch_config, monkeypatch):
    """Weather clears but the drying delay has not elapsed -> stay parked."""
    patch_config(watchdog_dry_delay_minutes=30)
    _patch_weather(monkeypatch)  # good weather
    mower = _make_mower(activity=MowerActivity.MOWING)
    active = _session(start_offset_min=-10, duration_min=120)  # still in window
    plan = _make_planner(active=active)
    wd = _wire(mower, plan)
    wd._parked_by_watchdog = True

    # First clear: starts the drying clock, no resume yet.
    status = await wd.check_once()

    mower.mower_override_seconds.assert_not_awaited()
    mower.mower_resume.assert_not_awaited()
    plan.run_once.assert_not_awaited()
    assert wd._parked_by_watchdog is True
    assert wd._weather_cleared_at is not None
    assert "drying" in status.lower()


async def test_drying_delay_resumes_after_elapsed(patch_config, monkeypatch):
    """Once the drying delay has elapsed, an in-window session resumes."""
    patch_config(watchdog_dry_delay_minutes=30)
    _patch_weather(monkeypatch)
    mower = _make_mower(activity=MowerActivity.MOWING)
    active = _session(start_offset_min=-10, duration_min=180)  # plenty of window left
    plan = _make_planner(active=active)
    wd = _wire(mower, plan)
    wd._parked_by_watchdog = True
    # Pretend the weather cleared 31 minutes ago.
    wd._weather_cleared_at = dt.datetime.now() - dt.timedelta(minutes=31)

    await wd.check_once()

    mower.mower_override_seconds.assert_awaited_once()
    assert wd._parked_by_watchdog is False
    assert wd._weather_cleared_at is None


async def test_drying_delay_replans_when_window_passed(patch_config, monkeypatch):
    """Drying delay outlasts the session window -> replan a make-up session."""
    patch_config(watchdog_dry_delay_minutes=30)
    _patch_weather(monkeypatch)
    mower = _make_mower(activity=MowerActivity.MOWING)
    expired = _session(start_offset_min=-60, duration_min=30)  # ended 30 min ago
    plan = _make_planner(active=expired)
    wd = _wire(mower, plan)
    wd._parked_by_watchdog = True
    wd._weather_cleared_at = dt.datetime.now() - dt.timedelta(minutes=31)

    await wd.check_once()

    mower.mower_override_seconds.assert_not_awaited()
    plan.run_once.assert_awaited_once()
    assert wd._parked_by_watchdog is False


async def test_drying_clock_resets_when_weather_turns_bad(patch_config, monkeypatch):
    """A clear→bad→clear flap restarts the drying delay."""
    patch_config(watchdog_dry_delay_minutes=30)
    # Already parked, weather turns bad again while parked.
    _patch_weather(monkeypatch, rain=5.0)
    mower = _make_mower(activity=MowerActivity.PARKED)
    plan = _make_planner(active=None)
    wd = _wire(mower, plan)
    wd._parked_by_watchdog = True
    wd._weather_cleared_at = dt.datetime.now() - dt.timedelta(minutes=10)

    await wd.check_once()

    assert wd._weather_cleared_at is None


async def test_zero_dry_delay_is_immediate(patch_config, monkeypatch):
    """watchdog_dry_delay_minutes == 0 keeps the original immediate behaviour."""
    patch_config(watchdog_dry_delay_minutes=0)
    _patch_weather(monkeypatch)
    mower = _make_mower(activity=MowerActivity.MOWING)
    active = _session(start_offset_min=-10, duration_min=60)
    plan = _make_planner(active=active)
    wd = _wire(mower, plan)
    wd._parked_by_watchdog = True

    await wd.check_once()

    mower.mower_override_seconds.assert_awaited_once()
    assert wd._parked_by_watchdog is False
