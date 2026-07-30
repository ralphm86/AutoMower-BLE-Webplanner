"""
Unit tests for the session store (store.py).

Cover the two things the rest of the app relies on: sessions being opened,
rolled up and closed correctly from sampler ticks, and GPS fixes being turned
into a dwell-time coverage grid.
"""

import datetime as dt

import pytest

import store
from automower_ble.protocol import MowerActivity

MOW = int(MowerActivity.MOWING)
CHARGING = int(MowerActivity.CHARGING)
PARKED = int(MowerActivity.PARKED)
GOING_HOME = int(MowerActivity.GOING_HOME)


def _tick(ts, activity, battery=80, charging=False):
    return store.record_sample(ts, battery, activity, charging, interval_s=60)


# ─── Session lifecycle ────────────────────────────────────────────────────────
class TestSessionLifecycle:
    def test_mowing_opens_a_session(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW, battery=95)
        assert sid is not None
        assert store.open_session_row()["id"] == sid

    def test_mow_seconds_accumulate(self):
        t0 = int(dt.datetime.now().timestamp())
        for i in range(5):
            _tick(t0 + i * 60, MOW)
        # 4 intervals credited between 5 samples
        assert store.get_session(store.latest_session_id())["mow_sec"] == 240

    def test_idle_does_not_open_a_session(self):
        t0 = int(dt.datetime.now().timestamp())
        _tick(t0, PARKED)
        assert store.open_session_row() is None

    def test_charge_cycle_stays_in_one_session(self):
        """A mow → charge → mow cycle must not be split into two sessions."""
        t0 = int(dt.datetime.now().timestamp())
        for i in range(10):                       # 10 min mowing
            _tick(t0 + i * 60, MOW)
        for i in range(10, 50):                   # 40 min charging
            _tick(t0 + i * 60, CHARGING, charging=True)
        for i in range(50, 60):                   # 10 min mowing again
            _tick(t0 + i * 60, MOW)

        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["charge_sec"] == 40 * 60

    def test_long_idle_closes_the_session(self):
        t0 = int(dt.datetime.now().timestamp())
        _tick(t0, MOW)
        _tick(t0 + 60, MOW)
        _tick(t0 + 60 + store.SESSION_END_IDLE_S + 60, PARKED)

        assert store.open_session_row() is None
        session = store.list_sessions()[0]
        assert session["complete"] == 1
        assert session["end_reason"] == "idle"
        assert session["ended_ts"] == t0 + 60      # closed at the last mow tick

    def test_battery_range_is_tracked(self):
        t0 = int(dt.datetime.now().timestamp())
        _tick(t0, MOW, battery=100)
        _tick(t0 + 60, MOW, battery=60)
        _tick(t0 + 120, MOW, battery=75)
        session = store.get_session(store.latest_session_id())
        assert session["battery_start"] == 100
        assert session["battery_min"] == 60

    def test_close_stale_sessions_recovers_after_a_crash(self):
        t0 = int(dt.datetime.now().timestamp()) - store.SESSION_END_IDLE_S - 3600
        _tick(t0, MOW)
        assert store.open_session_row() is not None
        store.close_stale_sessions()
        assert store.open_session_row() is None
        assert store.list_sessions()[0]["end_reason"] == "interrupted"


# ─── Mow-history projection ───────────────────────────────────────────────────
class TestMowHistoryProjection:
    def test_today_includes_the_open_session(self):
        t0 = int(dt.datetime.now().timestamp())
        _tick(t0, MOW)
        _tick(t0 + 60, MOW)
        _tick(t0 + 120, MOW)
        # Still mowing, yet the total is already visible to the planner.
        assert store.open_session_row() is not None
        assert store.get_mow_sec_today() == 120

    def test_legacy_record_creates_a_day_row(self):
        yesterday = dt.date.today() - dt.timedelta(days=1)
        store.record_mowing_day(yesterday, 1800)
        assert store.get_last_mow_date() == yesterday
        assert store.load_mow_history()[yesterday.isoformat()]["mow_sec"] == 1800

    def test_legacy_records_are_additive(self):
        today = dt.date.today()
        store.record_mowing_day(today, 600)
        store.record_mowing_day(today, 900)
        assert store.get_mow_sec_today() == 1500


# ─── Plans and events ─────────────────────────────────────────────────────────
class TestPlansAndEvents:
    def test_session_claims_a_recent_plan(self):
        t0 = int(dt.datetime.now().timestamp())
        plan_id = store.record_plan(t0, 3600, reason="interval due", dispatched_ts=t0 - 300)
        sid = _tick(t0, MOW)
        assert store.get_session(sid)["plan_id"] == plan_id
        assert store.get_plan(plan_id)["reason"] == "interval due"

    def test_stale_plan_is_not_claimed(self):
        t0 = int(dt.datetime.now().timestamp())
        store.record_plan(t0, 3600, dispatched_ts=t0 - store.PLAN_LINK_WINDOW_S - 1)
        sid = _tick(t0, MOW)
        assert store.get_session(sid)["plan_id"] is None

    def test_a_plan_is_claimed_only_once(self):
        t0 = int(dt.datetime.now().timestamp())
        plan_id = store.record_plan(t0, 3600, dispatched_ts=t0)
        first = _tick(t0, MOW)
        store.close_session(first, t0 + 60)
        second = _tick(t0 + 120, MOW)
        assert store.get_session(first)["plan_id"] == plan_id
        assert store.get_session(second)["plan_id"] is None

    def test_plan_weather_round_trips(self):
        plan_id = store.record_plan(0, 60, weather={"rain": 0.2})
        assert store.get_plan(plan_id)["weather"] == {"rain": 0.2}

    def test_only_activity_transitions_are_recorded(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW)
        _tick(t0 + 60, MOW)
        _tick(t0 + 120, MOW)
        _tick(t0 + 180, GOING_HOME)
        details = [e["detail"] for e in store.events_for_session(sid)]
        assert details == ["MOWING", "GOING_HOME"]

    def test_error_codes_become_events(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = store.record_sample(t0, 80, MOW, False, error_code=1)
        store.record_sample(t0 + 60, 80, MOW, False, error_code=1)
        kinds = [(e["kind"], e["detail"]) for e in store.events_for_session(sid)]
        # Repeats of the same error must not spam the timeline.
        assert kinds.count(("error", store._error_name(1))) == 1

    def test_session_detail_bundles_everything(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW, battery=90)
        _tick(t0 + 60, CHARGING, battery=85)
        detail = store.session_detail(sid)
        assert detail["session"]["id"] == sid
        assert [s["battery"] for s in detail["samples"]] == [90, 85]
        assert detail["samples"][0]["activity_name"] == "MOWING"
        assert len(detail["events"]) == 2

    def test_session_detail_is_none_for_unknown_id(self):
        assert store.session_detail(999) is None


# ─── GPS fixes and coverage ───────────────────────────────────────────────────
class TestCoverage:
    def test_fix_binds_to_the_open_session(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW)
        assert store.record_fix(t0 + 1, 52.5200, 13.4050) == sid

    def test_explicit_mowing_fix_opens_a_session(self):
        """GPS alone must be able to start a session while BLE is asleep."""
        t0 = int(dt.datetime.now().timestamp())
        sid = store.record_fix(t0, 52.52, 13.405, mowing=True)
        assert sid is not None
        assert store.open_session_row()["source"] == "gps"

    def test_gps_only_session_accumulates_mow_time(self):
        t0 = int(dt.datetime.now().timestamp())
        for i in range(4):
            store.record_fix(t0 + i * 10, 52.52, 13.405, mowing=True)
        assert store.get_session(store.latest_session_id())["mow_sec"] == 30

    def test_sampler_session_is_not_double_counted(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW)
        _tick(t0 + 60, MOW)
        for i in range(6):
            store.record_fix(t0 + i * 10, 52.52, 13.405, mowing=True)
        assert store.get_session(sid)["mow_sec"] == 60

    def test_stale_sample_does_not_imply_mowing(self):
        t0 = int(dt.datetime.now().timestamp())
        _tick(t0, MOW)
        store.close_session(store.latest_session_id(), t0, "manual")
        fix_ts = t0 + store._SAMPLE_STALE_S + 60
        store.record_fix(fix_ts, 52.52, 13.405)
        assert store.fixes(mowing_only=True) == []

    def test_grid_accumulates_dwell_time(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW)
        # Three fixes 10 s apart in the same square metre.
        for i in range(3):
            store.record_fix(t0 + i * 10, 52.5200000, 13.4050000, mowing=True)

        grid = store.coverage_grid(sid, cell_m=1.0)
        assert len(grid["cells"]) == 1
        assert grid["cells"][0]["sec"] == 20      # 2 gaps × 10 s
        assert grid["covered_m2"] == 1.0

    def test_separate_cells_for_separate_ground(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW)
        # ~5 m apart in latitude → different 1 m cells.  The final fix has no
        # successor so it contributes no dwell time and no cell.
        for i in range(4):
            store.record_fix(t0 + i * 10, 52.5200000 + i * 0.000045, 13.4050000,
                             mowing=True)

        grid = store.coverage_grid(sid, cell_m=1.0)
        assert len(grid["cells"]) == 3
        assert grid["covered_m2"] == 3.0

    def test_non_mowing_fixes_are_excluded(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW)
        store.record_fix(t0, 52.52, 13.405, mowing=True)
        store.record_fix(t0 + 10, 52.52, 13.405, mowing=True)
        store.record_fix(t0 + 20, 52.53, 13.415, mowing=False)

        grid = store.coverage_grid(sid, cell_m=1.0)
        assert grid["fix_count"] == 2

    def test_gps_dropout_is_not_credited(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW)
        store.record_fix(t0, 52.52, 13.405, mowing=True)
        store.record_fix(t0 + 3600, 52.52, 13.405, mowing=True)   # 1 h gap
        grid = store.coverage_grid(sid, cell_m=1.0)
        assert grid["cells"][0]["sec"] == store._MAX_DWELL_GAP_S

    def test_accuracy_filter(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW)
        store.record_fix(t0, 52.52, 13.405, accuracy_m=1.0, mowing=True)
        store.record_fix(t0 + 10, 52.52, 13.405, accuracy_m=25.0, mowing=True)
        store.record_fix(t0 + 20, 52.52, 13.405, accuracy_m=2.0, mowing=True)

        assert store.coverage_grid(sid, max_accuracy_m=5.0)["fix_count"] == 2

    def test_empty_grid_is_well_formed(self):
        grid = store.coverage_grid(None)
        assert grid["cells"] == []
        assert grid["covered_m2"] == 0.0
        assert grid["bounds"] is None

    def test_coverage_is_persisted_on_close(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW)
        store.record_fix(t0, 52.52, 13.405, mowing=True)
        store.record_fix(t0 + 10, 52.52, 13.405, mowing=True)
        store.close_session(sid, t0 + 20, "manual")
        assert store.get_session(sid)["covered_m2"] == 1.0

    def test_delete_session_removes_its_fixes(self):
        t0 = int(dt.datetime.now().timestamp())
        sid = _tick(t0, MOW)
        store.record_fix(t0, 52.52, 13.405, mowing=True)
        store.delete_session(sid)
        assert store.get_session(sid) is None
        assert store.fix_count() == 0


# ─── Legacy import ────────────────────────────────────────────────────────────
class TestLegacyImport:
    def test_reconstructs_sessions_from_samples(self, tmp_path):
        import json

        t0 = int(dt.datetime(2026, 6, 1, 10, 0).timestamp())
        samples = (
            [{"ts": t0 + i * 60, "battery": 100 - i, "activity": MOW,
              "charging": False} for i in range(30)]
            + [{"ts": t0 + i * 60, "battery": 70, "activity": GOING_HOME,
                "charging": False} for i in range(30, 35)]
            + [{"ts": t0 + i * 60, "battery": 70 + i, "activity": CHARGING,
                "charging": True} for i in range(35, 100)]
        )
        p = tmp_path / "runtime_samples.json"
        p.write_text(json.dumps({"samples": samples}))

        store.import_legacy_json(p, tmp_path / "missing.json")

        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["mow_sec"] == 29 * 60
        assert sessions[0]["search_sec"] == 5 * 60
        assert sessions[0]["source"] == "import"
        assert len(store.recent_samples()) == len(samples)

    def test_history_days_already_covered_are_skipped(self, tmp_path):
        import json

        t0 = int(dt.datetime(2026, 6, 1, 10, 0).timestamp())
        samples = [{"ts": t0 + i * 60, "battery": 90, "activity": MOW,
                    "charging": False} for i in range(10)]
        (tmp_path / "s.json").write_text(json.dumps({"samples": samples}))
        (tmp_path / "h.json").write_text(json.dumps({
            "2026-06-01": {"mow_sec": 99999, "complete": True},
            "2026-05-30": {"mow_sec": 1800, "complete": True},
        }))

        store.import_legacy_json(tmp_path / "s.json", tmp_path / "h.json")

        history = store.load_mow_history()
        assert history["2026-06-01"]["mow_sec"] == 9 * 60   # samples win
        assert history["2026-05-30"]["mow_sec"] == 1800     # gap filled

    def test_import_is_idempotent(self, tmp_path):
        import json

        t0 = int(dt.datetime(2026, 6, 1, 10, 0).timestamp())
        samples = [{"ts": t0 + i * 60, "battery": 90, "activity": MOW,
                    "charging": False} for i in range(10)]
        p = tmp_path / "runtime_samples.json"
        p.write_text(json.dumps({"samples": samples}))

        store.import_legacy_json(p, tmp_path / "missing.json")
        store.import_legacy_json(p, tmp_path / "missing.json")

        assert len(store.list_sessions()) == 1

    def test_import_is_not_repeated_after_the_samples_are_pruned(self, tmp_path):
        """
        prune() legitimately empties the sample table once imported data ages
        out, which must not make the importer think it has work to do again.
        """
        import json

        t0 = int(dt.datetime(2026, 6, 1, 10, 0).timestamp())
        samples = [{"ts": t0 + i * 60, "battery": 90, "activity": MOW,
                    "charging": False} for i in range(10)]
        p = tmp_path / "runtime_samples.json"
        p.write_text(json.dumps({"samples": samples}))

        store.import_legacy_json(p, tmp_path / "missing.json")
        store.prune(sample_days=0, fix_days=0)
        assert store.recent_samples() == []

        store.import_legacy_json(p, tmp_path / "missing.json")
        assert len(store.list_sessions()) == 1
