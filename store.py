"""
Session-centric persistence layer for AutoMower-BLE.

Replaces the ad-hoc JSON stores (runtime_samples.json, mow_history.json) with a
single SQLite database organised around the *mowing session* — the period from
the mower leaving the charging station until it finally parks again, including
any intermediate charge cycles.

Everything observed during a session hangs off that one entity:

    session ──┬── sample   (battery / activity time series)
              ├── fix      (GPS positions, source for the coverage heat map)
              └── event    (watchdog parks, errors, overrides, plan dispatch)

Design notes
------------
* Rollup columns (mow_sec, charge_sec, battery_*) are maintained incrementally
  on the session row, so history survives pruning of the raw sample/fix rows
  and ``get_mow_sec_today()`` is accurate even while a session is still open.
* All functions are synchronous.  SQLite writes here are single-row and
  sub-millisecond under WAL, but callers on the asyncio event loop should wrap
  scanning queries in ``asyncio.to_thread()``.
* The legacy JSON files are imported once on first init and then left alone.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from automower_ble.error_codes import ErrorCodes
from automower_ble.protocol import MowerActivity

logger = logging.getLogger(__name__)

DB_PATH = Path("automower.db")
SCHEMA_VERSION = 1

# A session is considered finished once the mower has not been MOWING for this
# long.  It must comfortably exceed a full recharge so that a mow → charge → mow
# cycle stays inside a single session.
SESSION_END_IDLE_S = 5400        # 90 minutes

# A mower sample older than this tells us nothing about the current activity,
# so GPS fixes will not infer "mowing" from it.
_SAMPLE_STALE_S = 300

# Retention for raw time-series rows.  Session summary rows are kept forever
# (they are tiny), so long-term history survives without unbounded growth.
SAMPLE_RETAIN_DAYS = 60
FIX_RETAIN_DAYS = 60
EVENT_RETAIN_DAYS = 180

# How long after a planner dispatch a newly opened session may still be
# attributed to that plan.  The mower needs a few minutes to leave the station
# and reach MOWING, which is when the sampler opens the session.
PLAN_LINK_WINDOW_S = 1800        # 30 minutes

_MOW = int(MowerActivity.MOWING)
_CHARGING = int(MowerActivity.CHARGING)
_SEARCHING = (int(MowerActivity.GOING_OUT), int(MowerActivity.GOING_HOME))

_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan (
    id            INTEGER PRIMARY KEY,
    created_ts    INTEGER NOT NULL,
    start_ts      INTEGER NOT NULL,
    duration_sec  INTEGER NOT NULL,
    reason        TEXT,
    weather_json  TEXT,
    dispatched_ts INTEGER,
    outcome       TEXT
);

CREATE TABLE IF NOT EXISTS session (
    id            INTEGER PRIMARY KEY,
    started_ts    INTEGER NOT NULL,
    ended_ts      INTEGER,
    last_mow_ts   INTEGER,
    local_date    TEXT NOT NULL,
    plan_id       INTEGER REFERENCES plan(id),
    end_reason    TEXT,
    mow_sec       INTEGER NOT NULL DEFAULT 0,
    charge_sec    INTEGER NOT NULL DEFAULT 0,
    search_sec    INTEGER NOT NULL DEFAULT 0,
    battery_start INTEGER,
    battery_end   INTEGER,
    battery_min   INTEGER,
    covered_m2    REAL,
    complete      INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL DEFAULT 'sampler'
);
CREATE INDEX IF NOT EXISTS idx_session_date ON session(local_date);
CREATE INDEX IF NOT EXISTS idx_session_open ON session(ended_ts);

CREATE TABLE IF NOT EXISTS sample (
    ts         INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES session(id),
    battery    INTEGER,
    activity   INTEGER,
    state      INTEGER,
    charging   INTEGER,
    error_code INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sample_session ON sample(session_id, ts);

CREATE TABLE IF NOT EXISTS fix (
    ts         INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES session(id),
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    accuracy_m REAL,
    mowing     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fix_session ON fix(session_id, ts);

CREATE TABLE IF NOT EXISTS event (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES session(id),
    ts         INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_session ON event(session_id, ts);
"""


# ─── Connection management ────────────────────────────────────────────────────

def init_db(path: Path | str = DB_PATH, import_legacy: bool = True) -> None:
    """Open (creating if needed) the database and apply the schema."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode = WAL")
        _conn.execute("PRAGMA synchronous = NORMAL")
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.execute("PRAGMA busy_timeout = 5000")
        _conn.executescript(_SCHEMA)
        version = _conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            _conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _conn.commit()
        logger.info("Store opened: %s (schema v%d)", path, SCHEMA_VERSION)

    if import_legacy:
        import_legacy_json()


def close_db() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("store.init_db() has not been called")
    return _conn


def _local_date(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts).date().isoformat()


# ─── Plans ────────────────────────────────────────────────────────────────────

def record_plan(
    start_ts: int,
    duration_sec: int,
    reason: Optional[str] = None,
    weather: Optional[dict] = None,
    dispatched_ts: Optional[int] = None,
    outcome: str = "dispatched",
) -> int:
    """
    Persist a planner decision so a session can later be traced back to *why*
    the mower went out.  Returns the plan id.
    """
    now = int(dt.datetime.now().timestamp())
    with _lock:
        cur = _db().execute(
            "INSERT INTO plan (created_ts, start_ts, duration_sec, reason, "
            "weather_json, dispatched_ts, outcome) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, start_ts, duration_sec, reason,
             json.dumps(weather) if weather else None,
             dispatched_ts if dispatched_ts is not None else now, outcome),
        )
        _db().commit()
        return int(cur.lastrowid)


def set_plan_outcome(plan_id: int, outcome: str) -> None:
    with _lock:
        _db().execute("UPDATE plan SET outcome = ? WHERE id = ?", (outcome, plan_id))
        _db().commit()


def get_plan(plan_id: int) -> Optional[dict]:
    row = _db().execute("SELECT * FROM plan WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        return None
    plan = dict(row)
    if plan["weather_json"]:
        try:
            plan["weather"] = json.loads(plan["weather_json"])
        except ValueError:
            plan["weather"] = None
    else:
        plan["weather"] = None
    plan.pop("weather_json", None)
    return plan


def _unclaimed_plan_id(ts: int) -> Optional[int]:
    """
    Most recent dispatched plan that no session has claimed yet, if it was
    dispatched recently enough to plausibly have caused the session at *ts*.
    """
    row = _db().execute(
        "SELECT p.id FROM plan p "
        "LEFT JOIN session s ON s.plan_id = p.id "
        "WHERE s.id IS NULL AND p.dispatched_ts IS NOT NULL "
        "AND p.dispatched_ts <= ? AND p.dispatched_ts >= ? "
        "ORDER BY p.dispatched_ts DESC LIMIT 1",
        (ts, ts - PLAN_LINK_WINDOW_S),
    ).fetchone()
    return int(row["id"]) if row else None


# ─── Session lifecycle ────────────────────────────────────────────────────────

def open_session(
    started_ts: int,
    battery: Optional[int] = None,
    plan_id: Optional[int] = None,
    source: str = "sampler",
) -> int:
    """Create a new session row and return its id."""
    with _lock:
        if plan_id is None:
            plan_id = _unclaimed_plan_id(started_ts)
        cur = _db().execute(
            "INSERT INTO session "
            "(started_ts, last_mow_ts, local_date, plan_id, battery_start, "
            " battery_min, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (started_ts, started_ts, _local_date(started_ts), plan_id,
             battery, battery, source),
        )
        _db().commit()
        sid = int(cur.lastrowid)
    logger.info("Session %d opened at %s (plan %s)", sid,
                dt.datetime.fromtimestamp(started_ts), plan_id)
    return sid


def close_session(
    session_id: int,
    ended_ts: int,
    end_reason: str = "unknown",
    battery_end: Optional[int] = None,
) -> None:
    """Mark a session finished."""
    with _lock:
        _db().execute(
            "UPDATE session SET ended_ts = ?, end_reason = ?, complete = 1, "
            "battery_end = COALESCE(?, battery_end) WHERE id = ?",
            (ended_ts, end_reason, battery_end, session_id),
        )
        _db().commit()
        if fix_count(session_id):
            update_session_coverage(session_id)
    logger.info("Session %d closed (%s)", session_id, end_reason)


def open_session_row() -> Optional[dict]:
    """Return the currently open session, or None."""
    row = _db().execute(
        "SELECT * FROM session WHERE ended_ts IS NULL ORDER BY started_ts DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def close_stale_sessions(now_ts: Optional[int] = None) -> None:
    """
    Close any session left open by a crash or restart.

    Called at startup: a session whose last mowing sample is older than
    ``SESSION_END_IDLE_S`` can never be resumed, so it is closed at its last
    known mowing timestamp rather than being carried forward.
    """
    now_ts = now_ts if now_ts is not None else int(dt.datetime.now().timestamp())
    with _lock:
        rows = _db().execute(
            "SELECT id, last_mow_ts, started_ts FROM session WHERE ended_ts IS NULL"
        ).fetchall()
        for row in rows:
            last = row["last_mow_ts"] or row["started_ts"]
            if now_ts - last > SESSION_END_IDLE_S:
                close_session(row["id"], last, end_reason="interrupted")


# ─── Samples ──────────────────────────────────────────────────────────────────

def record_sample(
    ts: int,
    battery: Optional[int],
    activity: int,
    charging: bool,
    state: Optional[int] = None,
    error_code: Optional[int] = None,
    interval_s: int = 60,
) -> Optional[int]:
    """
    Persist one sampler tick, opening/closing/updating the session as required.

    Returns the id of the session the sample belongs to (None when the mower is
    idle and no session is active).
    """
    with _lock:
        prev = _db().execute(
            "SELECT activity, error_code FROM sample ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        session = open_session_row()
        sid: Optional[int] = session["id"] if session else None

        if activity == _MOW:
            if sid is None:
                sid = open_session(ts, battery=battery)
                session = open_session_row()
            # Credit the elapsed interval, clamped so a long BLE outage does not
            # inflate the total with time the mower may not have been mowing.
            elapsed = min(ts - (session["last_mow_ts"] or ts), interval_s * 2) \
                if session else 0
            _db().execute(
                "UPDATE session SET mow_sec = mow_sec + ?, last_mow_ts = ? WHERE id = ?",
                (max(0, elapsed), ts, sid),
            )
            if battery is not None:
                _db().execute(
                    "UPDATE session SET battery_start = COALESCE(battery_start, ?), "
                    "battery_min = MIN(COALESCE(battery_min, ?), ?) WHERE id = ?",
                    (battery, battery, battery, sid),
                )
        elif sid is not None:
            if activity == _CHARGING:
                _db().execute(
                    "UPDATE session SET charge_sec = charge_sec + ? WHERE id = ?",
                    (interval_s, sid),
                )
            elif activity in _SEARCHING:
                _db().execute(
                    "UPDATE session SET search_sec = search_sec + ? WHERE id = ?",
                    (interval_s, sid),
                )
            # Session has been idle long enough to be considered finished.
            last_mow = session["last_mow_ts"] or session["started_ts"]
            if ts - last_mow > SESSION_END_IDLE_S:
                close_session(sid, last_mow, end_reason="idle", battery_end=battery)
                sid = None

        _db().execute(
            "INSERT OR REPLACE INTO sample "
            "(ts, session_id, battery, activity, state, charging, error_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, sid, battery, activity, state, int(bool(charging)), error_code),
        )
        _db().commit()

        # Turn the sample stream into a timeline: only transitions are stored,
        # so a session's event list stays readable however often we sample.
        if prev is None or prev["activity"] != activity:
            record_event("activity", _activity_name(activity), ts=ts, session_id=sid)
        if error_code and (prev is None or prev["error_code"] != error_code):
            record_event("error", _error_name(error_code), ts=ts, session_id=sid)
    return sid


def _activity_name(activity: Optional[int]) -> str:
    try:
        return MowerActivity(activity).name
    except ValueError:
        return f"UNKNOWN_{activity}"


def _error_name(code: int) -> str:
    try:
        return ErrorCodes(code).name
    except ValueError:
        return f"UNKNOWN_{code}"


def recent_samples(limit: int = 2000) -> list[dict]:
    """
    Return the most recent samples, oldest first, in the legacy dict shape
    consumed by ``_analyse_samples()`` and the runtime-estimate endpoints.
    """
    rows = _db().execute(
        "SELECT ts, battery, activity, charging FROM sample "
        "ORDER BY ts DESC LIMIT ?", (limit,),
    ).fetchall()
    return [
        {"ts": r["ts"], "battery": r["battery"],
         "activity": r["activity"], "charging": bool(r["charging"])}
        for r in reversed(rows)
    ]


def samples_for_session(session_id: int) -> list[dict]:
    rows = _db().execute(
        "SELECT ts, battery, activity, charging FROM sample "
        "WHERE session_id = ? ORDER BY ts", (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def clear_samples() -> None:
    """Drop all raw samples, resetting the runtime estimator's learned rates."""
    with _lock:
        _db().execute("DELETE FROM sample")
        _db().commit()


# ─── GPS fixes ────────────────────────────────────────────────────────────────

def record_fix(
    ts: int,
    lat: float,
    lon: float,
    accuracy_m: Optional[float] = None,
    mowing: Optional[bool] = None,
) -> Optional[int]:
    """
    Persist a GPS fix, binding it to the currently open session.

    When *mowing* is not supplied it is inferred from the most recent mower
    sample, so fixes recorded during the drive out or the search phase can be
    excluded from coverage without the GPS source needing to know the state.
    A fix that is explicitly flagged as mowing opens a session by itself, which
    keeps coverage working while BLE is idle-suspended and no samples arrive.
    """
    with _lock:
        if mowing is None:
            row = _db().execute(
                "SELECT ts, activity FROM sample ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            # A stale sample says nothing about what the mower is doing now.
            mowing = bool(row and row["activity"] == _MOW
                          and ts - row["ts"] <= _SAMPLE_STALE_S)

        session = open_session_row()
        sid = session["id"] if session else None
        if sid is not None and not mowing:
            # Close a session that the sampler is not around to close, e.g. when
            # BLE is idle-suspended and GPS is the only live data source.
            last_mow = session["last_mow_ts"] or session["started_ts"]
            if ts - last_mow > SESSION_END_IDLE_S:
                close_session(sid, last_mow, end_reason="idle")
                sid = None
        if sid is None and mowing:
            sid = open_session(ts, source="gps")
        elif sid is not None and mowing:
            # For a GPS-opened session there are no sampler ticks to maintain
            # the rollup, so credit the elapsed time here instead.  Sampler-owned
            # sessions must not be touched or the total would be counted twice.
            if session is not None and session["source"] == "gps":
                elapsed = min(ts - (session["last_mow_ts"] or ts), _MAX_DWELL_GAP_S)
                _db().execute(
                    "UPDATE session SET mow_sec = mow_sec + ? WHERE id = ?",
                    (max(0, elapsed), sid),
                )
            _db().execute(
                "UPDATE session SET last_mow_ts = MAX(COALESCE(last_mow_ts, 0), ?) "
                "WHERE id = ?", (ts, sid),
            )

        _db().execute(
            "INSERT OR REPLACE INTO fix (ts, session_id, lat, lon, accuracy_m, mowing) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, sid, lat, lon, accuracy_m, int(bool(mowing))),
        )
        _db().commit()
    return sid


def fixes(
    session_id: Optional[int] = None,
    mowing_only: bool = False,
    limit: Optional[int] = None,
) -> list[dict]:
    """Return fixes, oldest first.  *session_id* None means all sessions."""
    where: list[str] = []
    params: list[Any] = []
    if session_id is not None:
        where.append("session_id = ?")
        params.append(session_id)
    if mowing_only:
        where.append("mowing = 1")
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    if limit:
        rows = _db().execute(
            f"SELECT ts, session_id, lat, lon, accuracy_m, mowing FROM fix "
            f"{clause} ORDER BY ts DESC LIMIT ?", (*params, limit),
        ).fetchall()
        rows = list(reversed(rows))
    else:
        rows = _db().execute(
            f"SELECT ts, session_id, lat, lon, accuracy_m, mowing FROM fix "
            f"{clause} ORDER BY ts", params,
        ).fetchall()
    return [dict(r) for r in rows]


def latest_fix() -> Optional[dict]:
    row = _db().execute(
        "SELECT ts, session_id, lat, lon, accuracy_m, mowing FROM fix "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def fix_count(session_id: Optional[int] = None) -> int:
    if session_id is None:
        return int(_db().execute("SELECT COUNT(*) FROM fix").fetchone()[0])
    return int(_db().execute(
        "SELECT COUNT(*) FROM fix WHERE session_id = ?", (session_id,)
    ).fetchone()[0])


def delete_fixes(session_id: Optional[int] = None) -> None:
    with _lock:
        if session_id is None:
            _db().execute("DELETE FROM fix")
        else:
            _db().execute("DELETE FROM fix WHERE session_id = ?", (session_id,))
        _db().commit()


# ─── Coverage ─────────────────────────────────────────────────────────────────

_M_PER_DEG_LAT = 111320.0

# Cap on the time credited to a single fix.  Without it a GPS dropout would
# dump the whole outage into whichever cell the mower happened to be in.
_MAX_DWELL_GAP_S = 30


def coverage_grid(
    session_id: Optional[int] = None,
    cell_m: float = 1.0,
    max_accuracy_m: Optional[float] = None,
) -> dict:
    """
    Bin the mowing fixes of *session_id* (or all sessions) into a metric grid,
    accumulating dwell time per cell.

    Dwell time rather than fix count is used because the GPS reporting rate is
    not guaranteed to be constant — counting fixes would make a slow reporter
    look like sparse coverage.
    """
    points = fixes(session_id=session_id, mowing_only=True)
    if max_accuracy_m is not None:
        points = [p for p in points
                  if p["accuracy_m"] is None or p["accuracy_m"] <= max_accuracy_m]
    if not points:
        return {"cells": [], "cell_size_m": cell_m, "cell_deg": None,
                "covered_m2": 0.0, "peak_sec": 0.0, "fix_count": 0, "bounds": None}

    ref_lat = points[0]["lat"]
    m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians(ref_lat))
    if m_per_deg_lon <= 0:                      # degenerate near the poles
        m_per_deg_lon = _M_PER_DEG_LAT

    grid: dict[tuple[int, int], float] = {}
    for i, p in enumerate(points):
        if i + 1 >= len(points):
            break
        gap = points[i + 1]["ts"] - p["ts"]
        if gap <= 0:
            continue
        key = (math.floor(p["lat"] * _M_PER_DEG_LAT / cell_m),
               math.floor(p["lon"] * m_per_deg_lon / cell_m))
        grid[key] = grid.get(key, 0.0) + min(gap, _MAX_DWELL_GAP_S)

    if not grid:
        return {"cells": [], "cell_size_m": cell_m, "cell_deg": None,
                "covered_m2": 0.0, "peak_sec": 0.0,
                "fix_count": len(points), "bounds": None}

    peak = max(grid.values())
    cells = [
        {
            "lat": (ix + 0.5) * cell_m / _M_PER_DEG_LAT,
            "lon": (iy + 0.5) * cell_m / m_per_deg_lon,
            "sec": round(sec, 1),
            "w": round(sec / peak, 4),
        }
        for (ix, iy), sec in grid.items()
    ]
    lats = [c["lat"] for c in cells]
    lons = [c["lon"] for c in cells]
    d_lat = cell_m / _M_PER_DEG_LAT
    d_lon = cell_m / m_per_deg_lon
    return {
        "cells": cells,
        "cell_size_m": cell_m,
        # Cell extent in degrees, so the map can draw true-to-scale rectangles
        # instead of fixed-pixel blobs that lie about the area covered.
        "cell_deg": [d_lat, d_lon],
        "covered_m2": round(len(grid) * cell_m ** 2, 1),
        "peak_sec": round(peak, 1),
        "fix_count": len(points),
        # Outer edges of the outermost cells, not their centres.
        "bounds": [[min(lats) - d_lat / 2, min(lons) - d_lon / 2],
                   [max(lats) + d_lat / 2, max(lons) + d_lon / 2]],
    }


def update_session_coverage(session_id: int, cell_m: float = 1.0) -> float:
    """Recompute and persist a session's covered area, returning it in m²."""
    covered = coverage_grid(session_id, cell_m=cell_m)["covered_m2"]
    with _lock:
        _db().execute("UPDATE session SET covered_m2 = ? WHERE id = ?",
                      (covered, session_id))
        _db().commit()
    return covered


# ─── Events ───────────────────────────────────────────────────────────────────

def record_event(kind: str, detail: str = "", ts: Optional[int] = None,
                 session_id: Optional[int] = None) -> None:
    """Append a timeline event (watchdog park, error, override, plan dispatch)."""
    ts = ts if ts is not None else int(dt.datetime.now().timestamp())
    with _lock:
        if session_id is None:
            session = open_session_row()
            session_id = session["id"] if session else None
        _db().execute(
            "INSERT INTO event (session_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
            (session_id, ts, kind, detail),
        )
        _db().commit()


def events_for_session(session_id: int) -> list[dict]:
    rows = _db().execute(
        "SELECT ts, kind, detail FROM event WHERE session_id = ? ORDER BY ts",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_events(limit: int = 200) -> list[dict]:
    """Latest events across all sessions, newest first."""
    rows = _db().execute(
        "SELECT ts, session_id, kind, detail FROM event ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ─── Session queries ──────────────────────────────────────────────────────────

def list_sessions(limit: int = 50, with_fixes_only: bool = False) -> list[dict]:
    """Return recent sessions, newest first, with their fix counts."""
    having = "HAVING fix_count > 0" if with_fixes_only else ""
    rows = _db().execute(
        f"""
        SELECT s.*, COUNT(f.ts) AS fix_count
        FROM session s
        LEFT JOIN fix f ON f.session_id = s.id AND f.mowing = 1
        GROUP BY s.id
        {having}
        ORDER BY s.started_ts DESC
        LIMIT ?
        """, (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_session(session_id: int) -> Optional[dict]:
    row = _db().execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def session_detail(session_id: int) -> Optional[dict]:
    """Everything needed to render one session's history page."""
    session = get_session(session_id)
    if session is None:
        return None
    session["fix_count"] = fix_count(session_id)
    samples = [
        {**s, "activity_name": _activity_name(s["activity"])}
        for s in samples_for_session(session_id)
    ]
    return {
        "session": session,
        "plan": get_plan(session["plan_id"]) if session["plan_id"] else None,
        "samples": samples,
        "events": events_for_session(session_id),
    }


def latest_session_id() -> Optional[int]:
    """Id of the open session, or of the most recent one when nothing is running."""
    row = _db().execute(
        "SELECT id FROM session ORDER BY (ended_ts IS NULL) DESC, started_ts DESC LIMIT 1"
    ).fetchone()
    return int(row["id"]) if row else None


def delete_session(session_id: int) -> None:
    with _lock:
        _db().execute("DELETE FROM fix WHERE session_id = ?", (session_id,))
        _db().execute("DELETE FROM event WHERE session_id = ?", (session_id,))
        _db().execute("UPDATE sample SET session_id = NULL WHERE session_id = ?",
                      (session_id,))
        _db().execute("DELETE FROM session WHERE id = ?", (session_id,))
        _db().commit()


# ─── Mow-history compatibility layer ──────────────────────────────────────────
# These mirror the signatures previously provided by planner.py so callers do
# not need to change.  mow_history is now a projection of the session table.

def load_mow_history() -> dict:
    """Return {date_str: {mow_sec, complete, last_updated}} derived from sessions."""
    rows = _db().execute(
        "SELECT local_date, SUM(mow_sec) AS mow_sec, MIN(complete) AS complete, "
        "MAX(COALESCE(ended_ts, last_mow_ts, started_ts)) AS updated "
        "FROM session GROUP BY local_date"
    ).fetchall()
    return {
        r["local_date"]: {
            "mow_sec": int(r["mow_sec"] or 0),
            "complete": bool(r["complete"]),
            "last_updated": dt.datetime.fromtimestamp(
                r["updated"]).isoformat(timespec="seconds") if r["updated"] else None,
        }
        for r in rows
        if (r["mow_sec"] or 0) > 0
    }


def record_mowing_day(date: dt.date, mow_sec: int, complete: bool = True) -> None:
    """
    Legacy entry point: attribute *mow_sec* to *date* without a live session.

    The sampler no longer needs this — it maintains the session rollup directly —
    but it is kept so external callers and the importer can inject history.
    """
    if mow_sec <= 0:
        return
    date_str = date.isoformat()
    with _lock:
        row = _db().execute(
            "SELECT id FROM session WHERE local_date = ? AND source = 'legacy'",
            (date_str,),
        ).fetchone()
        if row:
            _db().execute(
                "UPDATE session SET mow_sec = mow_sec + ?, complete = ? WHERE id = ?",
                (mow_sec, int(complete), row["id"]),
            )
        else:
            noon = int(dt.datetime.combine(date, dt.time(12, 0)).timestamp())
            _db().execute(
                "INSERT INTO session (started_ts, ended_ts, last_mow_ts, local_date, "
                "mow_sec, complete, source, end_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, 'legacy', 'imported')",
                (noon, noon + mow_sec, noon + mow_sec, date_str, mow_sec, int(complete)),
            )
        _db().commit()


def get_last_mow_date(days_back: int = 60) -> Optional[dt.date]:
    """Most recent date before today with recorded mowing activity."""
    today = dt.date.today().isoformat()
    cutoff = (dt.date.today() - dt.timedelta(days=days_back)).isoformat()
    row = _db().execute(
        "SELECT local_date FROM session WHERE local_date < ? AND local_date >= ? "
        "AND mow_sec > 0 ORDER BY local_date DESC LIMIT 1",
        (today, cutoff),
    ).fetchone()
    return dt.date.fromisoformat(row["local_date"]) if row else None


def get_mow_sec_today() -> int:
    """Total mowing seconds recorded for today, including any open session."""
    row = _db().execute(
        "SELECT COALESCE(SUM(mow_sec), 0) FROM session WHERE local_date = ?",
        (dt.date.today().isoformat(),),
    ).fetchone()
    return int(row[0])


# ─── Retention ────────────────────────────────────────────────────────────────

def prune(sample_days: int = SAMPLE_RETAIN_DAYS,
          fix_days: int = FIX_RETAIN_DAYS,
          event_days: int = EVENT_RETAIN_DAYS) -> None:
    """Drop raw time-series rows older than the retention window."""
    now = int(dt.datetime.now().timestamp())
    with _lock:
        _db().execute("DELETE FROM sample WHERE ts < ?", (now - sample_days * 86400,))
        _db().execute("DELETE FROM fix WHERE ts < ?", (now - fix_days * 86400,))
        _db().execute("DELETE FROM event WHERE ts < ?", (now - event_days * 86400,))
        _db().commit()


# ─── Legacy JSON import ───────────────────────────────────────────────────────

_LEGACY_SAMPLES = Path("runtime_samples.json")
_LEGACY_HISTORY = Path("mow_history.json")


def import_legacy_json(
    samples_path: Path = _LEGACY_SAMPLES,
    history_path: Path = _LEGACY_HISTORY,
) -> None:
    """
    One-shot import of the old JSON stores.  Idempotent: the guard is the
    presence of imported *sessions*, not samples, because ``prune()`` legitimately
    empties the sample table once the imported data ages past the retention
    window — re-importing then would duplicate every session.
    """
    already = _db().execute(
        "SELECT COUNT(*) FROM session WHERE source = 'import'"
    ).fetchone()[0]
    if not already:
        _import_samples(samples_path)
    _import_history(history_path)


def _import_samples(path: Path) -> None:
    if not path.exists():
        return
    try:
        samples = json.loads(path.read_text()).get("samples", [])
    except Exception as exc:
        logger.warning("Could not import %s: %s", path, exc)
        return
    if not samples:
        return

    samples = sorted(samples, key=lambda s: s["ts"])
    sessions = _reconstruct_sessions(samples)

    with _lock:
        sid_by_index: dict[int, int] = {}
        for idx, sess in enumerate(sessions):
            cur = _db().execute(
                "INSERT INTO session (started_ts, ended_ts, last_mow_ts, local_date, "
                "mow_sec, charge_sec, search_sec, battery_start, battery_end, "
                "battery_min, complete, source, end_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'import', 'reconstructed')",
                (sess["started_ts"], sess["ended_ts"], sess["last_mow_ts"],
                 _local_date(sess["started_ts"]), sess["mow_sec"], sess["charge_sec"],
                 sess["search_sec"], sess["battery_start"], sess["battery_end"],
                 sess["battery_min"]),
            )
            sid_by_index[idx] = int(cur.lastrowid)

        for s in samples:
            sid = None
            for idx, sess in enumerate(sessions):
                if sess["started_ts"] <= s["ts"] <= sess["ended_ts"]:
                    sid = sid_by_index[idx]
                    break
            _db().execute(
                "INSERT OR IGNORE INTO sample "
                "(ts, session_id, battery, activity, charging) VALUES (?, ?, ?, ?, ?)",
                (s["ts"], sid, s.get("battery"), s.get("activity"),
                 int(bool(s.get("charging")))),
            )
        _db().commit()
    logger.info("Imported %d samples and %d reconstructed sessions from %s",
                len(samples), len(sessions), path)


def _reconstruct_sessions(samples: Iterable[dict]) -> list[dict]:
    """
    Group historical samples into sessions using the same idle-gap rule the
    live sampler applies, so imported history is comparable with new data.
    """
    sessions: list[dict] = []
    cur: Optional[dict] = None
    prev_ts: Optional[int] = None

    for s in samples:
        ts, act, batt = s["ts"], s.get("activity"), s.get("battery")
        interval = min(ts - prev_ts, 300) if prev_ts is not None else 0
        prev_ts = ts

        if act == _MOW:
            if cur is None:
                cur = {"started_ts": ts, "last_mow_ts": ts, "ended_ts": ts,
                       "mow_sec": 0, "charge_sec": 0, "search_sec": 0,
                       "battery_start": batt, "battery_end": batt,
                       "battery_min": batt}
            else:
                cur["mow_sec"] += max(0, min(ts - cur["last_mow_ts"], 300))
            cur["last_mow_ts"] = ts
            cur["ended_ts"] = ts
            if batt is not None:
                cur["battery_min"] = batt if cur["battery_min"] is None \
                    else min(cur["battery_min"], batt)
                cur["battery_end"] = batt
        elif cur is not None:
            if act == _CHARGING:
                cur["charge_sec"] += interval
            elif act in _SEARCHING:
                cur["search_sec"] += interval
            if ts - cur["last_mow_ts"] > SESSION_END_IDLE_S:
                cur["ended_ts"] = cur["last_mow_ts"]
                sessions.append(cur)
                cur = None

    if cur is not None:
        cur["ended_ts"] = cur["last_mow_ts"]
        sessions.append(cur)
    return [s for s in sessions if s["mow_sec"] > 0]


def _import_history(path: Path) -> None:
    """Import daily totals for days the sample import did not already cover."""
    if not path.exists():
        return
    try:
        history = json.loads(path.read_text())
    except Exception as exc:
        logger.warning("Could not import %s: %s", path, exc)
        return

    covered = {
        r["local_date"] for r in
        _db().execute("SELECT DISTINCT local_date FROM session").fetchall()
    }
    imported = 0
    for date_str, entry in history.items():
        if date_str in covered:
            continue
        mow_sec = int(entry.get("mow_sec", 0))
        if mow_sec <= 0:
            continue
        try:
            record_mowing_day(dt.date.fromisoformat(date_str), mow_sec,
                              complete=bool(entry.get("complete", False)))
            imported += 1
        except ValueError:
            continue
    if imported:
        logger.info("Imported %d legacy mow-history days from %s", imported, path)
