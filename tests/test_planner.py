"""
Unit tests for the planner's pure logic (planner.py).

These cover the core use cases of the smart scheduler: time windows, mowing
interval enforcement, weather gating, cycle-aware window sizing and the mow
history round-trip — all without network or Bluetooth access.
"""

import datetime as dt

import pytest

import planner
from planner import (
    plan_schedule,
    parse_forecast,
    _effective_window_sec,
    _snap_to_cycle_boundary,
    record_mowing_day,
    get_mow_sec_today,
    get_last_mow_date,
)


# ─── Isolate file-backed state ────────────────────────────────────────────────
@pytest.fixture
def tmp_history(tmp_path, monkeypatch):
    """Redirect mow-history persistence to a temp file."""
    p = tmp_path / "mow_history.json"
    monkeypatch.setattr(planner, "MOW_HISTORY_PATH", p)
    return p


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _all_day_windows(start=8, end=20):
    return [{"day": d, "start_hour": start, "end_hour": end} for d in range(7)]


def _cfg(**over):
    cfg = {
        "available_windows": _all_day_windows(),
        "target_hours_per_day": 2.0,
        "min_duration_minutes": 30,
        "max_duration_minutes": 180,
        "mowing_interval_days": 1,
        "max_wind_speed_ms": 10.0,
        "max_rain_mm_h": 0.5,
        "min_temp_celsius": 5.0,
    }
    cfg.update(over)
    return cfg


def _good_forecast(days=7):
    """Hourly periods with mow-friendly weather for the next *days* days."""
    periods = []
    base = dt.datetime.combine(dt.date.today(), dt.time(0, 0))
    for d in range(days):
        for h in range(24):
            periods.append(
                {
                    "dt": base + dt.timedelta(days=d, hours=h),
                    "rain_mm": 0.0,
                    "temp_c": 18.0,
                    "wind_ms": 2.0,
                    "description": "clear sky",
                    "icon": "0",
                    "dew_point_c": 8.0,
                    "humidity_pct": 50.0,
                }
            )
    return periods


# ─── plan_schedule ────────────────────────────────────────────────────────────
class TestPlanSchedule:
    def test_no_windows_schedules_nothing(self):
        sessions, log = plan_schedule(_cfg(available_windows=[]), _good_forecast())
        assert sessions == []
        assert all(entry["status"] == "no_window" for entry in log)

    def test_good_weather_produces_sessions(self):
        sessions, _ = plan_schedule(_cfg(), _good_forecast())
        assert len(sessions) >= 1
        for s in sessions:
            assert s["duration_sec"] > 0
            assert isinstance(s["start_dt"], dt.datetime)

    def test_interval_blocks_consecutive_days(self):
        # Mowed today, interval 2 → tomorrow must be blocked by interval.
        sessions, log = plan_schedule(
            _cfg(mowing_interval_days=2),
            _good_forecast(),
            seeded_last_mow_date=dt.date.today(),
        )
        tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        entry = next(e for e in log if e["date"] == tomorrow)
        assert entry["status"] == "interval"

    def test_rain_blocks_window(self):
        rainy = _good_forecast()
        for p in rainy:
            p["rain_mm"] = 5.0  # well above max_rain_mm_h
        sessions, log = plan_schedule(_cfg(), rainy)
        assert sessions == []
        assert any(e["status"] == "weather" for e in log)

    def test_cold_blocks_window(self):
        cold = _good_forecast()
        for p in cold:
            p["temp_c"] = -2.0  # below min_temp_celsius
        sessions, _ = plan_schedule(_cfg(), cold)
        assert sessions == []

    def test_high_wind_blocks_window(self):
        windy = _good_forecast()
        for p in windy:
            p["wind_ms"] = 25.0  # above max_wind_speed_ms
        sessions, _ = plan_schedule(_cfg(), windy)
        assert sessions == []

    def test_today_session_never_starts_in_the_past(self):
        # A same-day (re)plan must schedule today's session from now onward,
        # never at a window-start hour that has already passed.
        now = dt.datetime.now()
        sessions, _ = plan_schedule(_cfg(), _good_forecast())
        today = dt.date.today().isoformat()
        for s in sessions:
            if s["date"] == today:
                assert s["start_dt"] >= now.replace(microsecond=0) - dt.timedelta(seconds=1)

    def test_already_mowed_today_marks_complete(self):
        sessions, log = plan_schedule(
            _cfg(target_hours_per_day=1.0, mowing_interval_days=2),
            _good_forecast(),
            already_mowed_today_sec=3600,  # full target already mowed
        )
        today = dt.date.today().isoformat()
        entry = next(e for e in log if e["date"] == today)
        assert entry["status"] == "complete"


# ─── Cycle-aware sizing ───────────────────────────────────────────────────────
class TestCycleMath:
    def test_effective_window_inflates_for_charging(self):
        # 2 h mowing, 1 h mow per cycle, 0.5 h charge per cycle.
        win = _effective_window_sec(
            target_mow_sec=2 * 3600, mow_sec=3600, chg_sec=1800
        )
        # 2 full mow segments + 1 charge between = 2h + 0.5h = 2.5h
        assert win == pytest.approx(2.5 * 3600, abs=60)

    def test_effective_window_no_data_returns_target(self):
        assert _effective_window_sec(7200, 0, 0) == 7200

    def test_snap_keeps_short_single_segment(self):
        # Window shorter than one full cycle is returned unchanged.
        assert _snap_to_cycle_boundary(1800, 3600, 1800, 1800) == 1800

    def test_snap_trims_tiny_tail(self):
        # mow 1h, charge 0.5h. Session 1h40m → tail of 10m after a charge is
        # shorter than the 30m min partial → snap back to end of charge (1.5h).
        snapped = _snap_to_cycle_boundary(
            int(1.6667 * 3600), 3600, 1800, 30 * 60
        )
        assert snapped == pytest.approx(1.5 * 3600, abs=60)


# ─── parse_forecast ───────────────────────────────────────────────────────────
class TestParseForecast:
    def test_parses_open_meteo_payload(self):
        data = {
            "utc_offset_seconds": 7200,
            "hourly": {
                "time": ["2026-06-25T08:00", "2026-06-25T09:00"],
                "temperature_2m": [15.0, 17.0],
                "precipitation": [0.0, 0.2],
                "windspeed_10m": [3.0, 4.0],
                "weathercode": [0, 61],
                "dew_point_2m": [9.0, 10.0],
                "relative_humidity_2m": [55, 60],
            },
        }
        periods = parse_forecast(data)
        assert len(periods) == 2
        assert periods[0]["temp_c"] == 15.0
        assert periods[0]["description"] == "clear sky"
        assert periods[1]["description"] == "light rain"
        assert periods[0]["dt"].utcoffset() == dt.timedelta(hours=2)

    def test_handles_missing_fields(self):
        periods = parse_forecast({"hourly": {"time": ["2026-06-25T08:00"]}})
        assert len(periods) == 1
        assert periods[0]["temp_c"] == 20.0  # default
        assert periods[0]["rain_mm"] == 0.0


# ─── Mow history round-trip ───────────────────────────────────────────────────
class TestMowHistory:
    def test_record_and_read_today(self, tmp_history):
        assert get_mow_sec_today() == 0
        record_mowing_day(dt.date.today(), 1800)
        assert get_mow_sec_today() == 1800

    def test_records_are_additive(self, tmp_history):
        record_mowing_day(dt.date.today(), 600)
        record_mowing_day(dt.date.today(), 900)
        assert get_mow_sec_today() == 1500

    def test_zero_is_ignored(self, tmp_history):
        record_mowing_day(dt.date.today(), 0)
        assert get_mow_sec_today() == 0

    def test_last_mow_date(self, tmp_history):
        yesterday = dt.date.today() - dt.timedelta(days=1)
        record_mowing_day(yesterday, 1200)
        assert get_last_mow_date() == yesterday
