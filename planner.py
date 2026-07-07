"""
Weather-based mowing planner for Husqvarna Automower.

Fetches hourly forecasts from Open-Meteo (free, no API key required) and
generates an optimised mowing schedule that respects:
  - user-defined available time windows per day of week
  - minimum interval between mowing days
  - per-session min/max duration and target hours
  - weather thresholds (rain, temperature, wind speed)

The PlannerAgent class manages a background asyncio task that periodically
re-plans and pushes the updated schedule to the connected mower.
"""

import asyncio
import json
import logging
import datetime as dt
from pathlib import Path
from typing import Optional, Callable
import urllib.request
import urllib.parse
import urllib.error

from automower_ble.protocol import TaskInformation, MowerActivity

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("planner_config.json")
MOW_HISTORY_PATH = Path("mow_history.json")
PLANNED_SESSIONS_PATH = Path("planned_sessions.json")
PLAN_LOG_PATH = Path("plan_log.json")
_MOW_HISTORY_RETAIN_DAYS = 90

# Placeholder task installed in the mower's firmware scheduler when the
# direct-command planner is active.  The firmware requires at least one task
# so we park a harmless 1-minute slot on Monday at 00:01 that will never
# coincide with a real session.  The executor always wins via SetOverrideMow.
_PLACEHOLDER_TASK = TaskInformation(
    next_start_time=60,          # 00:01 Monday
    duration_in_seconds=60,      # 1 minute
    on_monday=True, on_tuesday=False, on_wednesday=False,
    on_thursday=False, on_friday=False, on_saturday=False, on_sunday=False,
)

DAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]

DEFAULT_CONFIG: dict = {
    "enabled": False,
    "location_lat": 52.52,
    "location_lon": 13.40,
    "location_name": "My Location",
    # List of {"day": 0-6, "start_hour": int, "end_hour": int}
    "available_windows": [],
    "target_hours_per_day": 2.0,
    "min_duration_minutes": 30,
    "max_duration_minutes": 180,
    # Minimum days between two consecutive mowing sessions
    "mowing_interval_days": 2,
    # How often the planner re-runs (hours)
    "replan_interval_hours": 6.0,
    # Optional fixed daily replan time, e.g. "06:00" (empty = interval only)
    "replan_time": "",
    # Weather thresholds (mowing is skipped if any threshold is exceeded)
    "max_wind_speed_ms": 10.0,
    "max_rain_mm_h": 0.5,   # max precipitation per hour in the window
    "min_temp_celsius": 5.0,
    # Minutes to wait after a rainy period before starting mowing (0 = disabled)
    "rain_delay_minutes": 0,
    # Weather watchdog: monitors current conditions while the mower is active
    "watchdog_enabled": False,
    "watchdog_interval_minutes": 5,
    # Minutes to wait after the weather clears before the watchdog resumes the
    # mower, so the lawn can dry (0 = resume immediately).  If the drying delay
    # outlasts the interrupted session's window, the planner replans instead.
    "watchdog_dry_delay_minutes": 0,
    # Hedgehog protection: restrict mowing to daylight hours (sunrise–sunset)
    "hedgehog_protection": False,
    # Heat stress protection: avoid mowing during peak heat; reduce frequency on hot days
    "heat_stress_protection": False,
    "heat_stress_temp_celsius": 28.0,        # daily max temp that triggers heat stress mode
    "heat_stress_no_mow_start_hour": 11,    # start of midday no-mow zone
    "heat_stress_no_mow_end_hour": 17,      # end of midday no-mow zone
    "heat_stress_interval_days": 2,         # min days between mows on hot days
    # Dew avoidance: delay mowing start until dew has evaporated
    "dew_avoidance_enabled": False,
    "dew_avoidance_auto": True,             # auto-estimate from forecast; False = fixed offset
    "dew_avoidance_hours_after_sunrise": 2.0,  # cap (auto) or fixed offset (manual)
    # Runtime state (persisted for display only)
    "last_plan_time": None,
    "last_plan_result": "Never run",
}


def load_planned_sessions() -> list[dict]:
    """
    Load persisted planned sessions from disk.  Entries whose start_dt is in
    the past are silently discarded so stale sessions never get dispatched.
    """
    if not PLANNED_SESSIONS_PATH.exists():
        return []
    try:
        raw = json.loads(PLANNED_SESSIONS_PATH.read_text())
        now = dt.datetime.now()
        sessions = []
        for s in raw:
            try:
                s["start_dt"] = dt.datetime.fromisoformat(s["start_dt"])
                if s["start_dt"] > now:
                    sessions.append(s)
            except Exception:
                pass
        return sorted(sessions, key=lambda s: s["start_dt"])
    except Exception as e:
        logger.warning("Could not load planned sessions: %s", e)
        return []


def save_planned_sessions(sessions: list[dict]) -> None:
    """Persist planned sessions to disk (start_dt serialised as ISO string)."""
    try:
        serialisable = [
            {**s, "start_dt": s["start_dt"].isoformat()}
            for s in sessions
        ]
        PLANNED_SESSIONS_PATH.write_text(json.dumps(serialisable, indent=2))
    except Exception as e:
        logger.warning("Could not save planned sessions: %s", e)


def load_plan_log() -> list[dict]:
    """Load the last planning log from disk, or return an empty list."""
    if not PLAN_LOG_PATH.exists():
        return []
    try:
        return json.loads(PLAN_LOG_PATH.read_text())
    except Exception as e:
        logger.warning("Could not load plan log: %s", e)
        return []


def save_plan_log(log: list[dict]) -> None:
    """Persist the planning log to disk."""
    try:
        PLAN_LOG_PATH.write_text(json.dumps(log, indent=2, default=str))
    except Exception as e:
        logger.warning("Could not save plan log: %s", e)


# ─── Config I/O ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open() as f:
                data = json.load(f)
            return {**DEFAULT_CONFIG, **data}
        except Exception as e:
            logger.warning("Could not load planner config: %s", e)
    return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    with CONFIG_PATH.open("w") as f:
        json.dump(config, f, indent=2, default=str)


# ─── Mow history ──────────────────────────────────────────────────────────────
# Tracks actual mowing dates/durations so the planner seeds the interval
# constraint from reality (not from the schedule, which may differ when the
# mower was parked by weather, manual override, or a missed session).

def load_mow_history() -> dict:
    """Load {date_str: {mow_sec, complete, last_updated}} from disk."""
    if MOW_HISTORY_PATH.exists():
        try:
            return json.loads(MOW_HISTORY_PATH.read_text())
        except Exception as e:
            logger.warning("Could not load mow history: %s", e)
    return {}


def save_mow_history(history: dict) -> None:
    """Persist mow history, pruning entries older than _MOW_HISTORY_RETAIN_DAYS."""
    cutoff = (dt.date.today() - dt.timedelta(days=_MOW_HISTORY_RETAIN_DAYS)).isoformat()
    history = {d: v for d, v in history.items() if d >= cutoff}
    try:
        MOW_HISTORY_PATH.write_text(json.dumps(history, indent=2, default=str))
    except Exception as e:
        logger.warning("Could not save mow history: %s", e)


def record_mowing_day(
    date: dt.date,
    mow_sec: int,
    complete: bool = True,
) -> None:
    """
    Accumulate *mow_sec* of actual mowing time for *date* in the persistent
    history.  Multiple calls for the same date are additive so a day with
    two separate runs (or sampler restarts) accumulates the correct total.
    """
    if mow_sec <= 0:
        return
    history = load_mow_history()
    date_str = date.isoformat()
    existing = history.get(date_str, {"mow_sec": 0})
    new_total = existing["mow_sec"] + mow_sec
    history[date_str] = {
        "mow_sec": new_total,
        "complete": complete,
        "last_updated": dt.datetime.now().isoformat(timespec="seconds"),
    }
    save_mow_history(history)
    logger.debug(
        "Mow history: %s += %ds \u2192 total %ds (complete=%s)",
        date_str, mow_sec, new_total, complete,
    )


def get_last_mow_date(days_back: int = 60) -> Optional[dt.date]:
    """
    Return the most recent date within *days_back* days that has recorded
    mowing activity.  Returns None when no history is available.
    """
    history = load_mow_history()
    today = dt.date.today()
    for back in range(1, days_back + 1):
        candidate = today - dt.timedelta(days=back)
        if candidate.isoformat() in history:
            return candidate
    return None


def get_mow_sec_today() -> int:
    """Return total recorded (completed) mowing seconds for today."""
    history = load_mow_history()
    return int(history.get(dt.date.today().isoformat(), {}).get("mow_sec", 0))


# ─── Weather fetching (Open-Meteo — free, no API key) ────────────────────────

# WMO weather interpretation codes (ISO 4677)
_WMO_DESCRIPTIONS: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "icy fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def _fetch_openmeteo_sync(lat: float, lon: float) -> dict:
    """Blocking HTTP call to Open-Meteo — run via asyncio executor."""
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,windspeed_10m,weathercode,dew_point_2m,relative_humidity_2m",
        "daily": "sunrise,sunset,temperature_2m_max,wind_speed_10m_max",
        "wind_speed_unit": "ms",
        "timezone": "auto",
        "forecast_days": 7,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Open-Meteo API {e.code}: {body}") from e


async def fetch_forecast(lat: float, lon: float) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_openmeteo_sync, lat, lon)


def parse_daily(data: dict) -> dict[str, dict]:
    """
    Parse Open-Meteo daily data into {date_str: {"sunrise": datetime, "sunset": datetime}}.
    Times are returned in the local timezone indicated by utc_offset_seconds.
    """
    daily      = data.get("daily", {})
    times      = daily.get("time", [])
    sunrises   = daily.get("sunrise", [])
    sunsets    = daily.get("sunset", [])
    temp_maxes = daily.get("temperature_2m_max", [])
    wind_maxes = daily.get("wind_speed_10m_max", [])
    utc_offset_sec = data.get("utc_offset_seconds", 0)
    tz = dt.timezone(dt.timedelta(seconds=utc_offset_sec))
    result: dict[str, dict] = {}
    for i, d in enumerate(times):
        sr_str = sunrises[i] if i < len(sunrises) else None
        ss_str = sunsets[i]  if i < len(sunsets)  else None
        try:
            sr = dt.datetime.fromisoformat(sr_str).replace(tzinfo=tz) if sr_str else None
            ss = dt.datetime.fromisoformat(ss_str).replace(tzinfo=tz) if ss_str else None
        except Exception:
            sr = ss = None
        result[d] = {
            "sunrise":    sr,
            "sunset":     ss,
            "temp_max_c": float(temp_maxes[i]) if i < len(temp_maxes) and temp_maxes[i] is not None else None,
            "wind_max_ms": float(wind_maxes[i]) if i < len(wind_maxes) and wind_maxes[i] is not None else None,
        }
    return result


def daily_to_serialisable(daily: dict[str, dict]) -> dict[str, dict]:
    """Convert {date: {sunrise/sunset: datetime}} to JSON-serialisable form."""
    return {
        d: {
            "sunrise":        v["sunrise"].isoformat() if v.get("sunrise") else None,
            "sunset":         v["sunset"].isoformat()  if v.get("sunset")  else None,
            "temp_max_c":     v.get("temp_max_c"),
            "wind_max_ms":    v.get("wind_max_ms"),
            "dew_h_estimated": v.get("dew_h_estimated"),  # set by run_once; None before first run
        }
        for d, v in daily.items()
    }


def _estimate_dew_hours(
    periods: list[dict],
    date: dt.date,
    sunrise: dt.datetime,
    max_dew_h: float,
) -> float:
    """
    Two-phase dew duration estimator.

    Phase 1 — Did dew form? (3 h before sunrise)
      Uses temperature–dewpoint gap and relative humidity.
      f_gap  = clamp((2 − gap_min) / 5, 0, 1) × 0.6
      f_rh   = clamp((rh_max − 65) / 35, 0, 1) × 0.4
      dew_score = f_gap + f_rh  (0–1)
      If dew_score < 0.25 → no dew, return 0.0.

    Phase 2 — How fast does it evaporate? (2 h after sunrise)
      f_evap = clamp((T_morn − 8) / 22, 0, 1) × 0.6
             + clamp(W_morn / 8, 0, 1)         × 0.4
      dew_h  = max_dew_h × dew_score × (1 − 0.7 × f_evap)
      Clamped to [0.25 h, max_dew_h] when dew is present.
    """
    pre_start = sunrise - dt.timedelta(hours=3)
    pre_periods = [p for p in periods if pre_start <= p["dt"] < sunrise]

    if not pre_periods:
        return max_dew_h  # no data — conservatively assume dew

    # Phase 1: dew formation score
    gaps = [
        p["temp_c"] - p["dew_point_c"]
        for p in pre_periods
        if p.get("dew_point_c") is not None
    ]
    humids = [p["humidity_pct"] for p in pre_periods if p.get("humidity_pct") is not None]

    gap_min = min(gaps)  if gaps  else 0.0    # no dew-point data → worst case
    rh_max  = max(humids) if humids else 100.0

    f_gap = max(0.0, min(1.0, (2.0 - gap_min) / 5.0)) * 0.6
    f_rh  = max(0.0, min(1.0, (rh_max - 65.0) / 35.0)) * 0.4
    dew_score = f_gap + f_rh

    if dew_score < 0.25:
        return 0.0  # no significant dew formed

    # Phase 2: evaporation rate
    post_end = sunrise + dt.timedelta(hours=2)
    post_periods = [p for p in periods if sunrise <= p["dt"] < post_end]

    if post_periods:
        t_morn = sum(p["temp_c"]  for p in post_periods) / len(post_periods)
        w_morn = sum(p["wind_ms"] for p in post_periods) / len(post_periods)
    else:
        t_morn, w_morn = 15.0, 0.0

    f_evap = (
        max(0.0, min(1.0, (t_morn - 8.0) / 22.0)) * 0.6
        + max(0.0, min(1.0, w_morn / 8.0))         * 0.4
    )

    dew_h = max_dew_h * dew_score * (1.0 - 0.7 * f_evap)
    return max(0.25, min(max_dew_h, dew_h))


def _geocode_openmeteo_sync(city: str) -> list[dict]:
    params = urllib.parse.urlencode({
        "name": city, "count": 5, "language": "en", "format": "json",
    })
    url = f"https://geocoding-api.open-meteo.com/v1/search?{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    return [
        {
            "name": r["name"],
            "lat": r["latitude"],
            "lon": r["longitude"],
            "country": r.get("country", ""),
            "state": r.get("admin1", ""),
        }
        for r in data.get("results", [])
    ]


async def geocode_city(city: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _geocode_openmeteo_sync, city)


def _fetch_current_weather_sync(lat: float, lon: float) -> dict:
    """Fetch current weather conditions from Open-Meteo (blocking)."""
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": "precipitation,windspeed_10m,weathercode",
        "wind_speed_unit": "ms",
        "timezone": "auto",
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Open-Meteo API {e.code}: {body}") from e


async def fetch_current_weather(lat: float, lon: float) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_current_weather_sync, lat, lon)


def parse_forecast(data: dict) -> list[dict]:
    """Convert Open-Meteo hourly forecast to plain dicts with local datetimes."""
    hourly = data.get("hourly", {})
    times  = hourly.get("time", [])
    temps   = hourly.get("temperature_2m", [])
    rains   = hourly.get("precipitation", [])
    winds   = hourly.get("windspeed_10m", [])
    codes   = hourly.get("weathercode", [])
    dew_pts = hourly.get("dew_point_2m", [])
    humids  = hourly.get("relative_humidity_2m", [])

    utc_offset_sec = data.get("utc_offset_seconds", 0)
    tz = dt.timezone(dt.timedelta(seconds=utc_offset_sec))

    periods = []
    for i, t in enumerate(times):
        local_dt = dt.datetime.fromisoformat(t).replace(tzinfo=tz)
        wmo = int(codes[i]) if i < len(codes) else 0
        periods.append({
            "dt":          local_dt,
            "rain_mm":     float(rains[i]) if i < len(rains) else 0.0,
            "temp_c":      float(temps[i]) if i < len(temps) else 20.0,
            "wind_ms":     float(winds[i]) if i < len(winds) else 0.0,
            "description": _WMO_DESCRIPTIONS.get(wmo, f"code {wmo}"),
            "icon":        str(wmo),
            "dew_point_c":  float(dew_pts[i]) if i < len(dew_pts) and dew_pts[i] is not None else None,
            "humidity_pct": float(humids[i])  if i < len(humids) and humids[i]  is not None else None,
        })
    return periods


def forecast_to_serialisable(periods: list[dict]) -> list[dict]:
    return [{**p, "dt": p["dt"].isoformat()} for p in periods]


# ─── Planning logic ────────────────────────────────────────────────────────────

# Minimum useful last mowing stint when snapping a session to a charge-cycle
# boundary.  If the final mowing segment within a session would be shorter than
# this, the session is trimmed back so the mower doesn't bother going out for
# a pointless short run.  Intentionally independent of min_duration_minutes
# (which controls minimum session length, not minimum useful last stint).
_SNAP_MIN_PARTIAL_SEC = 30 * 60  # 30 minutes

def _find_best_subwindow(
    periods: list[dict],
    date: dt.date,
    start_h: int,
    end_h: int,
    max_rain: float,
    max_wind: float,
    min_temp: float,
    target_dur_sec: int,
    min_dur_sec: int,
    max_dur_sec: int,
    rain_delay_sec: int = 0,
    earliest_start_sec: Optional[int] = None,
) -> tuple[Optional[int], int, str]:
    """
    Walk the available window in OWM-aligned 3-hour steps and find the
    longest contiguous run of weather-acceptable slots.

    rain_delay_sec: extra seconds to wait after a rainy slot ends before
    the mower is allowed to start (grass/lawn drying time).
    earliest_start_sec: do not start before this time (seconds from midnight);
    defaults to start_h * 3600.  Use to search for a second session after a
    first one has already been scheduled.

    Returns (start_seconds_from_midnight, duration_seconds, reason).
    Returns (None, 0, reason) if no suitable slot is found.
    """
    # Collect OWM periods that overlap [start_h, end_h) on this date.
    # Open-Meteo returns 1-hour slots in local time (e.g. 06:00, 07:00, ...).
    # Use overlap check so fractional-offset timezones also work correctly.
    overlapping = sorted(
        [p for p in periods
         if p["dt"].date() == date
         and p["dt"].hour < end_h
         and p["dt"].hour + 1 > start_h],
        key=lambda x: x["dt"].hour,
    )

    if not overlapping:
        # Genuinely no forecast data → beyond 5-day OWM window
        window_dur = (end_h - start_h) * 3600
        session_sec = max(min_dur_sec, min(max_dur_sec, target_dur_sec, window_dur))
        return start_h * 3600, session_sec, "Good — beyond 5-day forecast window"

    # Build a timeline of (eff_start, eff_end, ok, description) slots,
    # filling any gaps between OWM periods with "assumed OK".
    raw_slots = []
    for p in overlapping:
        ph = p["dt"].hour
        eff_start = max(ph, start_h)
        eff_end = min(ph + 1, end_h)  # 1-hour slots (Open-Meteo)
        if eff_end <= eff_start:
            continue
        rain_ok = p["rain_mm"] <= max_rain
        wind_ok = p["wind_ms"] <= max_wind
        temp_ok = p["temp_c"] >= min_temp
        ok = rain_ok and wind_ok and temp_ok
        issues = []
        if not rain_ok:
            issues.append(f"rain {p['rain_mm']:.1f} mm")
        if not wind_ok:
            issues.append(f"wind {p['wind_ms']:.1f} m/s")
        if not temp_ok:
            issues.append(f"temp {p['temp_c']:.1f}°C")
        desc = "; ".join(issues) if issues else p["description"]
        raw_slots.append((eff_start, eff_end, ok, desc))

    # Fill gaps at edges and between consecutive periods.
    # Gaps use an empty description (no real forecast data for that hour).
    slots: list[tuple[int, int, bool, str]] = []
    cursor = start_h
    for (es, ee, ok, desc) in raw_slots:
        if es > cursor:
            slots.append((cursor, es, True, ""))  # gap: no OWM data, assumed dry
        slots.append((es, ee, ok, desc))
        cursor = ee
    if cursor < end_h:
        slots.append((cursor, end_h, True, ""))  # trailing gap

    if not slots:
        return None, 0, "No forecast slots in window"

    # Find the longest contiguous run of good slots.
    # Work entirely in seconds so rain_delay_sec can produce non-round-hour starts.
    earliest_mow_sec = earliest_start_sec if earliest_start_sec is not None else start_h * 3600

    # Rain delay: check periods ending at or before the window start so that rain
    # falling in the hour(s) immediately before the window also triggers the delay.
    # Example: rain 05:00–06:00 + 60 min delay → earliest mowing 07:00, not 06:00.
    if rain_delay_sec > 0:
        lookback_h = (rain_delay_sec + 3599) // 3600  # ceiling division
        for p in periods:
            if (p["dt"].date() == date
                    and (start_h - lookback_h) <= p["dt"].hour < start_h
                    and p["rain_mm"] > max_rain):
                rain_ends_sec = (p["dt"].hour + 1) * 3600
                earliest_mow_sec = max(earliest_mow_sec, rain_ends_sec + rain_delay_sec)

    best_start_sec: Optional[int] = None
    best_dur_sec = 0
    best_reason = ""
    run_start_sec: Optional[int] = None
    run_reason = ""

    for (sh, eh, ok, desc) in slots:
        sh_sec = sh * 3600
        eh_sec = eh * 3600
        if not ok:
            # After this rainy slot, enforce the drying delay
            earliest_mow_sec = max(earliest_mow_sec, eh_sec + rain_delay_sec)
            run_start_sec = None
        else:
            usable_start = max(sh_sec, earliest_mow_sec)
            if usable_start >= eh_sec:
                # Delay pushes past the end of this good slot entirely
                run_start_sec = None
                continue
            if run_start_sec is None:
                run_start_sec = usable_start
                run_reason = desc
            elif not run_reason and desc:
                # Upgrade from a gap (no data) to a real weather description
                run_reason = desc
            run_dur = eh_sec - run_start_sec
            # Rank by contribution = min(max_dur, target, run_dur): the actual
            # mowing time this run would deliver.  Break ties by earliest start
            # so a sufficient earlier run is always preferred over an equally-
            # contributing later one — leaving the maximum window for slot 2.
            run_contrib  = min(max_dur_sec, target_dur_sec, run_dur)
            best_contrib = min(max_dur_sec, target_dur_sec, best_dur_sec) if best_dur_sec > 0 else 0
            if (run_contrib > best_contrib or
                    (run_contrib == best_contrib
                     and run_contrib >= min_dur_sec
                     and best_start_sec is not None
                     and run_start_sec < best_start_sec)):
                best_start_sec = run_start_sec
                best_dur_sec = run_dur
                best_reason = run_reason

    if best_start_sec is None or best_dur_sec < min_dur_sec:
        if best_start_sec is None:
            bad = [s[3] for s in slots if not s[2]]
            return None, 0, f"Bad weather: {'; '.join(bad[:2])}"
        return None, 0, f"Best dry run only {best_dur_sec // 60} min < min {min_dur_sec // 60} min"

    session_sec = max(min_dur_sec, min(max_dur_sec, target_dur_sec, best_dur_sec))
    return best_start_sec, session_sec, f"Good — {best_reason}" if best_reason else "Good"


def _effective_window_sec(
    target_mow_sec: int,
    mow_sec: float,
    chg_sec: float,
) -> int:
    """
    Return the total wall-clock window duration (seconds) needed to achieve
    *target_mow_sec* of actual mowing, given the mower's observed charge-cycle
    behaviour.

    The mower always charges to 100 % between mowing runs:
      Mow → home → charge → mow → home → charge → … → final mow

    For N total mowing segments (one per charge cycle), there are (N-1) charges
    between them:
      total = N * mow_sec_per_seg + (N-1) * chg_sec

    where the last segment is a partial one if target is not a whole multiple of
    mow_sec.
    """
    if mow_sec <= 0 or chg_sec < 0:
        return target_mow_sec
    n_full = int(target_mow_sec / mow_sec)
    rem_mow_sec = target_mow_sec - n_full * mow_sec
    # Number of charges: one per completed cycle, plus one more if there is a
    # partial tail segment that requires the mower to charge first.
    if rem_mow_sec > 60:          # meaningful tail → one extra charge
        n_charges = n_full
    else:                         # target is (nearly) a whole multiple
        n_charges = max(0, n_full - 1)
    return int(n_full * mow_sec + rem_mow_sec + n_charges * chg_sec)


def _snap_to_cycle_boundary(
    sess_s: int,
    mow_sec: float,
    chg_sec: float,
    min_partial_sec: int,
) -> int:
    """
    Snap a session duration to a clean charge-cycle boundary.

    The mower's runtime pattern within a session is:
      mow → home → charge → mow → home → charge → … → (partial) mow

    If the final mowing segment would be shorter than *min_partial_sec*, the
    session is shortened so that it ends right after the preceding full charge
    finishes (the mower is home, battery full, and the session simply ends
    without sending it out for the short pointless stint).

    The first mowing segment is never dropped (``elapsed == 0`` guard), so a
    window that is already shorter than one full cycle is returned unchanged.
    """
    if mow_sec <= 0 or chg_sec <= 0:
        return sess_s
    elapsed = 0.0
    while True:
        # --- mowing segment ---
        next_elapsed = elapsed + mow_sec
        if next_elapsed >= sess_s:
            # Partial mowing segment: check if it's long enough
            partial = sess_s - elapsed
            if partial < min_partial_sec and elapsed > 0:
                # Too short — snap session to end right after the preceding charge
                return int(elapsed)
            return sess_s  # acceptable partial (or the only segment)
        elapsed = next_elapsed
        # --- charging segment ---
        next_elapsed = elapsed + chg_sec
        if next_elapsed >= sess_s:
            # Session ends while the mower is still charging — that's fine
            return sess_s
        elapsed = next_elapsed


def plan_schedule(
    config: dict,
    forecast_periods: list[dict],
    daily: Optional[dict] = None,
    seeded_last_mow_date: Optional[dt.date] = None,
    cycle_data: Optional[dict] = None,
    already_mowed_today_sec: int = 0,
) -> tuple[list[dict], list[dict]]:
    """
    Build a mowing schedule for the next 7 days.

    Returns (sessions, planning_log).
    sessions is a list of dicts with keys: start_dt (datetime), duration_sec,
    date (ISO string), dow_name.  planning_log is a list of per-day decision
    dicts for display.

    *seeded_last_mow_date* is the most-recent past date with recorded actual
    mowing (from mow_history.json, not schedule-derived).  When provided it is
    used as the initial ``last_mow_date`` so the interval constraint is
    correctly enforced across replans even when sessions were missed.

    *cycle_data* is an optional dict with keys ``avg_mow_h`` and ``avg_chg_h``
    derived from observed discharge/charge rates.  When present, the scheduled
    window duration is inflated so the actual mowing time matches the target.

    *already_mowed_today_sec* is the total actual mowing seconds accumulated
    today (completed sessions + any ongoing session).  The planner deducts
    this from today's target so a mid-day replan only schedules the remainder.
    """
    today = dt.date.today()

    min_dur_sec = int(config.get("min_duration_minutes", 30)) * 60
    max_dur_sec = int(config.get("max_duration_minutes", 180)) * 60
    target_mow_sec = int(float(config.get("target_hours_per_day", 2.0)) * 3600)

    # When cycle data is available, inflate the window so the mower actually
    # achieves target_mow_sec of cutting time (rather than target_mow_sec of
    # wall-clock time which includes charging pauses).
    _cycle_note = ""
    if cycle_data and cycle_data.get("avg_mow_h") and cycle_data.get("avg_chg_h"):
        mow_s = cycle_data["avg_mow_h"] * 3600
        chg_s = cycle_data["avg_chg_h"] * 3600
        target_dur_sec = _effective_window_sec(target_mow_sec, mow_s, chg_s)
        _h = target_dur_sec // 3600
        _m = (target_dur_sec % 3600) // 60
        _mow_m = target_mow_sec // 60
        _cycle_note = (
            f" [cycle-aware: {_mow_m}m mowing target → "
            f"{_h}h {_m:02d}m window]"
        )
        logger.debug(
            "Planner: cycle-aware window sizing: target_mow=%ds → window=%ds "
            "(avg_mow=%.0fs avg_chg=%.0fs)",
            target_mow_sec, target_dur_sec, mow_s, chg_s,
        )
    else:
        target_dur_sec = target_mow_sec
    interval_days = max(1, int(config.get("mowing_interval_days", 2)))
    max_rain = float(config.get("max_rain_mm_h",
                                config.get("max_rain_mm_3h", 0.5)))  # fallback for old configs
    max_wind = float(config.get("max_wind_speed_ms", 10.0))
    min_temp = float(config.get("min_temp_celsius", 5.0))
    rain_delay_sec = int(config.get("rain_delay_minutes", 0)) * 60

    heat_stress_enabled      = bool(config.get("heat_stress_protection", False))
    heat_stress_temp         = float(config.get("heat_stress_temp_celsius", 28.0))
    heat_stress_no_mow_start = int(config.get("heat_stress_no_mow_start_hour", 11))
    heat_stress_no_mow_end   = int(config.get("heat_stress_no_mow_end_hour", 17))
    heat_stress_interval     = max(interval_days, int(config.get("heat_stress_interval_days", 2)))
    dew_enabled = bool(config.get("dew_avoidance_enabled", False))
    dew_auto    = bool(config.get("dew_avoidance_auto", True))
    max_dew_h   = float(config.get("dew_avoidance_hours_after_sunrise", 2.0))

    windows: list[dict] = config.get("available_windows", [])

    sessions: list[dict] = []
    log: list[dict] = []
    last_mow_date: Optional[dt.date] = seeded_last_mow_date
    now = dt.datetime.now()

    for offset in range(7):
        date = today + dt.timedelta(days=offset)
        dow = date.weekday()  # 0=Mon … 6=Sun

        def _log(status: str, reason: str, task_info=None):
            log.append({
                "date": date.isoformat(),
                "dow_name": DAY_NAMES[dow],
                "status": status,
                "reason": reason,
                "task": task_info,
            })

        # 1. Available window?
        day_windows = [w for w in windows if int(w["day"]) == dow]
        if not day_windows:
            _log("no_window", "No time window configured")
            continue

        # 1b. Heat stress: determine effective mowing interval for this day
        is_heat_stress_day = False
        effective_interval = interval_days
        if heat_stress_enabled and daily:
            t_max = daily.get(date.isoformat(), {}).get("temp_max_c")
            if t_max is not None and t_max >= heat_stress_temp:
                is_heat_stress_day = True
                effective_interval = heat_stress_interval

        # 2. Interval constraint
        if last_mow_date is not None and (date - last_mow_date).days < effective_interval:
            _log(
                "interval",
                f"Interval: {effective_interval}d min"
                + (" ☀️ heat stress" if is_heat_stress_day else "")
                + f", last was {last_mow_date.strftime('%a %d %b')}",
            )
            continue

        # 3-5. Iterate over ALL configured windows for this day and accumulate
        #      sessions until the daily target is met or windows are exhausted.
        day_sessions: list[tuple[int, int, str]] = []  # (start_sec, dur_sec, reason)
        # For today: if some mowing has already happened, deduct it from the
        # target so a mid-day replan only schedules the remaining time rather
        # than the full target again.  Snap / cycle-aware window are
        # recalculated from the reduced mowing target.
        if offset == 0 and already_mowed_today_sec > 0:
            rem_mow = max(0, target_mow_sec - already_mowed_today_sec)
            if rem_mow < min_dur_sec:
                _log(
                    "complete",
                    f"Daily target met: {already_mowed_today_sec // 60}min of "
                    f"{target_mow_sec // 60}min target already mowed today",
                )
                last_mow_date = date  # count today as mow day for interval
                continue
            remaining_sec = (
                _effective_window_sec(rem_mow, mow_s, chg_s)
                if cycle_data and cycle_data.get("avg_mow_h") and cycle_data.get("avg_chg_h")
                else rem_mow
            )
        else:
            remaining_sec = target_dur_sec
        fail_status = "weather"
        fail_reason = "No suitable weather window found"

        for win in day_windows:
            if remaining_sec < min_dur_sec:
                break  # daily target met

            w_start_h = int(win["start_hour"])
            w_end_h   = int(win["end_hour"])
            w_dur_sec = max(0, (w_end_h - w_start_h) * 3600)

            # Skip windows that have already ended
            window_end_dt = dt.datetime.combine(date, dt.time(w_end_h % 24, 0))
            if window_end_dt <= now:
                fail_status = "past_window"
                fail_reason = (
                    f"Window {w_start_h:02d}:00–{w_end_h:02d}:00 already ended"
                    if len(day_windows) > 1
                    else f"Window already ended at {w_end_h:02d}:00 — task would run next week"
                )
                continue

            # Skip windows too short for a minimum session
            if w_dur_sec < min_dur_sec:
                fail_status = "short_window"
                fail_reason = (
                    f"Window {w_end_h - w_start_h}h too short for min "
                    f"{config.get('min_duration_minutes')}min"
                )
                continue

            # Hedgehog protection: trim window to daylight hours
            if config.get("hedgehog_protection") and daily:
                day_info = daily.get(date.isoformat(), {})
                sr = day_info.get("sunrise")
                ss = day_info.get("sunset")
                if sr and ss:
                    sr_sec = sr.hour * 3600 + sr.minute * 60
                    ss_sec = ss.hour * 3600 + ss.minute * 60
                    eff_start_sec = max(w_start_h * 3600, sr_sec)
                    eff_end_sec   = min(w_end_h * 3600, ss_sec)
                    if eff_end_sec - eff_start_sec < min_dur_sec:
                        fail_status = "hedgehog"
                        fail_reason = (
                            f"Hedgehog protection: window outside daylight "
                            f"({sr.strftime('%H:%M')}↑ – {ss.strftime('%H:%M')}↓)"
                        )
                        continue
                    # w_start_h: floor so the sunrise slot is included;
                    #   earliest_start_sec = eff_start_sec pins the actual
                    #   mowing start to the exact sunrise minute.
                    # w_end_h: ceiling so the sunset boundary slot is included;
                    #   the session is clipped to the exact sunset second below.
                    w_start_h = eff_start_sec // 3600
                    w_end_h   = min(
                        (eff_end_sec + 3599) // 3600,  # ceiling division
                        int(win["end_hour"]),           # never exceed configured window
                    )
                    search_start_sec = eff_start_sec
                else:
                    search_start_sec = w_start_h * 3600
            else:
                search_start_sec = w_start_h * 3600

            # ── Dew avoidance: push earliest start past dew evaporation time ──────────
            if dew_enabled and daily:
                sr_dt = daily.get(date.isoformat(), {}).get("sunrise")
                if sr_dt:
                    dew_h = (
                        _estimate_dew_hours(forecast_periods, date, sr_dt, max_dew_h)
                        if dew_auto
                        else max_dew_h
                    )
                    if dew_h > 0.0:
                        dew_clear_sec = (
                            sr_dt.hour * 3600 + sr_dt.minute * 60 + int(dew_h * 3600)
                        )
                        if dew_clear_sec >= w_end_h * 3600:
                            fail_status = "dew"
                            fail_reason = (
                                f"Dew avoidance: dew clears at "
                                f"{dew_clear_sec // 3600:02d}:{(dew_clear_sec % 3600) // 60:02d}"
                                f", past window end"
                            )
                            continue
                        search_start_sec = max(search_start_sec, dew_clear_sec)
                        w_start_h = max(w_start_h, dew_clear_sec // 3600)

            # ── Heat stress: clip no-mow zone from window ───────────────────────────
            if is_heat_stress_day:
                hs_s = heat_stress_no_mow_start
                hs_e = heat_stress_no_mow_end
                if w_end_h > hs_s and w_start_h < hs_e:
                    if w_end_h > hs_e:
                        # Extends past hot zone → prefer afternoon/evening
                        w_start_h = max(w_start_h, hs_e)
                        search_start_sec = max(search_start_sec, hs_e * 3600)
                    elif w_start_h < hs_s:
                        # Starts before hot zone, ends inside → keep morning only
                        w_end_h = hs_s
                    else:
                        # Entirely within hot zone → skip this window
                        fail_status = "heat_stress"
                        fail_reason = (
                            f"Heat stress: window within no-mow zone "
                            f"{hs_s:02d}:00–{hs_e:02d}:00"
                        )
                        continue
                    # Re-check viability after clipping
                    if (w_end_h - w_start_h) * 3600 < min_dur_sec:
                        fail_status = "heat_stress"
                        fail_reason = (
                            f"Heat stress: clipped window {w_start_h:02d}:00–{w_end_h:02d}:00 "
                            f"too short (min {min_dur_sec // 60} min)"
                        )
                        continue

            # Keep finding sub-windows within this window until target is met
            # For today, never search a start time in the past — a mid-day
            # replan (e.g. after a weather park) must schedule from now onward.
            if offset == 0:
                now_sec_of_day = now.hour * 3600 + now.minute * 60 + now.second
                search_start_sec = max(search_start_sec, now_sec_of_day)
            while (remaining_sec >= min_dur_sec
                   and search_start_sec < w_end_h * 3600):
                start_s, sess_s, reason = _find_best_subwindow(
                    forecast_periods, date, w_start_h, w_end_h,
                    max_rain, max_wind, min_temp,
                    remaining_sec, min_dur_sec, max_dur_sec,
                    rain_delay_sec,
                    earliest_start_sec=search_start_sec,
                )
                if start_s is None:
                    fail_status = "weather"
                    fail_reason = reason
                    break
                # Hedgehog: clip session to end at or before the exact sunset second
                if config.get("hedgehog_protection") and daily:
                    day_info = daily.get(date.isoformat(), {})
                    ss_dt = day_info.get("sunset")
                    if ss_dt:
                        sunset_sec = ss_dt.hour * 3600 + ss_dt.minute * 60
                        sess_s = min(sess_s, max(0, sunset_sec - start_s))
                        if sess_s < min_dur_sec:
                            fail_status = "hedgehog"
                            fail_reason = (
                                f"Hedgehog protection: remaining daylight too short "
                                f"({ss_dt.strftime('%H:%M')}\u2193)"
                            )
                            break
                # Cycle boundary snap: avoid a very short last mowing segment.
                # If the session would end with a partial mowing run shorter
                # than min_dur_sec, trim it back so the session ends cleanly
                # right after the preceding charge (mower is home and idle).
                if cycle_data and cycle_data.get("avg_mow_h") and cycle_data.get("avg_chg_h"):
                    logger.debug(
                        "Planner: cycle snap check %s start=%ds dur=%ds "
                        "(min_partial=%ds)",
                        date.isoformat(), start_s, sess_s, _SNAP_MIN_PARTIAL_SEC,
                    )
                    snapped = _snap_to_cycle_boundary(
                        sess_s,
                        cycle_data["avg_mow_h"] * 3600,
                        cycle_data["avg_chg_h"] * 3600,
                        _SNAP_MIN_PARTIAL_SEC,
                    )
                    if snapped != sess_s:
                        logger.debug(
                            "Planner: cycle snap %s %ds → %ds "
                            "(last segment < %ds min)",
                            date.isoformat(), sess_s, snapped, min_dur_sec,
                        )
                        sess_s = snapped
                    if sess_s < min_dur_sec:
                        fail_status = "weather"
                        fail_reason = (
                            "Session too short after cycle-boundary snap "
                            f"({sess_s // 60} min < min {min_dur_sec // 60} min)"
                        )
                        break
                day_sessions.append((start_s, sess_s, reason))
                remaining_sec -= sess_s
                search_start_sec = start_s + sess_s  # next search starts after this session

        if not day_sessions:
            _log(fail_status, fail_reason)
            continue

        # ✓ Record as absolute-datetime sessions
        for (start_s, sess_s, _) in day_sessions:
            start_dt = dt.datetime.combine(date, dt.time(0, 0)) + dt.timedelta(seconds=start_s)
            sessions.append({
                "start_dt": start_dt,
                "duration_sec": sess_s,
                "date": date.isoformat(),
                "dow_name": DAY_NAMES[dow],
            })
        last_mow_date = date

        log_reason = (
            day_sessions[0][2]
            if len(day_sessions) == 1
            else f"{len(day_sessions)} sessions planned"
        )
        if _cycle_note:
            log_reason += _cycle_note
        _log(
            "scheduled",
            log_reason,
            [
                {
                    "start_time": f"{s // 3600:02d}:{(s % 3600) // 60:02d}",
                    "duration_minutes": d // 60,
                }
                for (s, d, _) in day_sessions
            ],
        )

    return sessions, log


# ─── Background agent ─────────────────────────────────────────────────────────

class PlannerAgent:
    """
    Asyncio background agent: periodically runs the weather planner and
    pushes the result to the connected mower.

    The mower reference is supplied via set_mower_provider() to avoid
    circular imports with web_app.py.
    """

    def __init__(self) -> None:
        self._bg_task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._get_mower: Optional[Callable] = None
        # Set by web_app after the first startup reconnect attempt so the
        # planner loop waits for the mower to be reachable before the first push.
        self._startup_event: Optional[asyncio.Event] = None

        self.last_log: list[dict] = load_plan_log()
        self.last_run: Optional[str] = None
        self.last_result: str = "Never run"
        self.next_run_dt: Optional[dt.datetime] = None
        self.last_forecast: list[dict] = []
        self.last_daily: dict = {}
        # Planned sessions (absolute datetimes) computed by the last run_once().
        # The executor loop dispatches these at the right time.
        # Loaded from disk on startup so sessions survive server restarts.
        self._planned_sessions: list[dict] = load_planned_sessions()
        self._active_session: Optional[dict] = None   # currently running session
        self._executor_task: Optional[asyncio.Task] = None
        # True when the executor is within _PRE_CONNECT_S seconds of a session start.
        # Used by the BLE idle-sleep logic to keep the connection alive.
        self._needs_connection: bool = False
        # Last successfully fetched cycle data from the mower (persists across
        # replans so the snap and window sizing still work when the mower is
        # temporarily disconnected at plan time).
        self._cycle_data: Optional[dict] = None
        # Optional callable (set by web_app) that returns sample-derived cycle
        # data {avg_mow_h, avg_chg_h} based on observed discharge/charge rates.
        # Takes priority over the mower's lifetime statistics averages because
        # it reflects recent real behaviour rather than all-time averages.
        self._cycle_data_provider: Optional[Callable] = None
        # Optional callable returning total actual mowing seconds for today
        # (completed sessions from history + any ongoing session tracked by
        # the sampler).  Used to deduct already-mowed time from today's target.
        self._mow_today_provider: Optional[Callable] = None
        # Set to True when the user manually parks the mower in Smart mode.
        # The executor skips dispatching sessions while this is True.
        # Cleared when the user issues a Resume command.
        self._user_inhibited: bool = False

    def set_mower_provider(self, provider: Callable) -> None:
        """Register a callable that returns the current Mower (or None)."""
        self._get_mower = provider

    def set_cycle_data_provider(self, provider: Callable) -> None:
        """
        Register a callable that returns sample-derived cycle data or None.

        The callable must return either
          {"avg_mow_h": float, "avg_chg_h": float}
        or None when not enough sample history is available yet.
        When it returns data it takes priority over GetAllStatistics averages.
        """
        self._cycle_data_provider = provider

    def set_mow_today_provider(self, provider: Callable) -> None:
        """
        Register a callable that returns the total mowing seconds for today
        (completed sessions in mow_history.json + any ongoing session tracked
        by the sampler).  Used by run_once() to deduct progress from today's
        target before building the schedule.
        """
        self._mow_today_provider = provider

    def _has_session_window_active_or_upcoming(self) -> bool:
        """
        Return True if a mowing session for today is currently active (its
        scheduled window has started but not yet ended) or starts within the
        next 60 minutes.

        Used by the BLE idle-sleep logic so the connection stays alive during
        scheduled sessions, enabling the sampler to collect accurate data.
        """
        now = dt.datetime.now()
        # Check currently running session
        if self._active_session:
            end_dt = (
                self._active_session["start_dt"]
                + dt.timedelta(seconds=self._active_session["duration_sec"])
            )
            if now < end_dt:
                return True
        # Check upcoming planned sessions (within 60 min)
        for sess in self._planned_sessions:
            end_dt = sess["start_dt"] + dt.timedelta(seconds=sess["duration_sec"])
            secs_until_start = (sess["start_dt"] - now).total_seconds()
            if now < end_dt and secs_until_start <= 3600:
                return True
        return False

    def set_startup_event(self, event: asyncio.Event) -> None:
        """Supply an event that is set when the startup connect attempt completes."""
        self._startup_event = event

    def is_replan_due(self) -> bool:
        """
        Return True if a replan is due, using the same wakeup logic as ``_loop``:
          - interval elapsed (``replan_interval_hours``), OR
          - the fixed ``replan_time`` (HH:MM) has passed since the last run.
        Always returns True when no run has occurred yet.
        """
        if self.last_run is None:
            return True
        config = load_config()
        interval_h = max(0.1, float(config.get("replan_interval_hours", 6)))
        replan_time_str = config.get("replan_time", "")
        try:
            last = dt.datetime.fromisoformat(self.last_run)
            now = dt.datetime.now()
            if (now - last).total_seconds() / 3600 >= interval_h:
                return True
            # Check if the fixed replan_time has passed since the last run
            if replan_time_str and replan_time_str.strip():
                try:
                    h, m = map(int, replan_time_str.strip().split(":"))
                    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    if target > now:
                        target -= dt.timedelta(days=1)
                    if target > last:
                        return True
                except (ValueError, AttributeError):
                    pass
            return False
        except Exception:
            return True

    def is_running(self) -> bool:
        return bool(self._bg_task and not self._bg_task.done())

    def start(self) -> None:
        if not self.is_running():
            self._stop_event    = asyncio.Event()
            self._bg_task       = asyncio.create_task(self._loop())
            self._executor_task = asyncio.create_task(self._session_executor_loop())
            logger.info("PlannerAgent started")

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        for task in (self._bg_task, self._executor_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("PlannerAgent stopped")

    async def run_once(self) -> str:
        """Fetch weather, compute plan, push to mower. Returns status string."""
        config = load_config()

        if not config.get("available_windows"):
            return self._set_result("No available time windows configured")

        try:
            data = await fetch_forecast(
                float(config["location_lat"]),
                float(config["location_lon"]),
            )
        except Exception as e:
            return self._set_result(f"Weather fetch failed: {e}")

        periods = parse_forecast(data)
        daily   = parse_daily(data)

        # Pre-compute per-day dew hours (same logic as plan_schedule) so that
        # is_dew_phase on each period reflects exactly what the planner enforces.
        dew_enabled_cfg = bool(config.get("dew_avoidance_enabled", False))
        dew_auto_cfg    = bool(config.get("dew_avoidance_auto", True))
        max_dew_h_cfg   = float(config.get("dew_avoidance_hours_after_sunrise", 2.0))
        _day_dew_h: dict[str, float] = {}
        for date_str, info in daily.items():
            sr = info.get("sunrise")
            if dew_enabled_cfg and sr:
                try:
                    _d = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
                    _day_dew_h[date_str] = (
                        _estimate_dew_hours(periods, _d, sr, max_dew_h_cfg)
                        if dew_auto_cfg else max_dew_h_cfg
                    )
                except Exception:
                    _day_dew_h[date_str] = max_dew_h_cfg
            else:
                _day_dew_h[date_str] = 0.0
            info["dew_h_estimated"] = _day_dew_h[date_str]

        # Annotate each hourly period with is_daylight and is_dew_phase for the UI
        for p in periods:
            date_str = p["dt"].strftime("%Y-%m-%d")
            day_info = daily.get(date_str, {})
            sr, ss = day_info.get("sunrise"), day_info.get("sunset")
            p["is_daylight"] = (sr <= p["dt"] < ss) if (sr and ss) else True
            dew_h = _day_dew_h.get(date_str, 0.0)
            if dew_enabled_cfg and sr and dew_h > 0.0:
                p["is_dew_phase"] = p["dt"] < sr + dt.timedelta(hours=dew_h)
            else:
                p["is_dew_phase"] = False
        self.last_forecast = forecast_to_serialisable(periods)
        self.last_daily    = daily_to_serialisable(daily)

        # ── Cycle data: try sample-derived rates first (most accurate), then
        # fall back to mower's lifetime statistics averages.
        cycle_data: Optional[dict] = None
        if self._cycle_data_provider:
            try:
                cycle_data = self._cycle_data_provider()
                if cycle_data:
                    logger.debug(
                        "Planner: using learned cycle data — "
                        "avg_mow=%.2fh avg_chg=%.2fh",
                        cycle_data["avg_mow_h"], cycle_data["avg_chg_h"],
                    )
            except Exception as e:
                logger.debug("Planner: cycle data provider error: %s", e)

        # ── Seed last_mow_date from the persistent mow history (actual mowing
        # dates, not schedule-derived) so the interval constraint is correct
        # even after missed sessions, rain-forced parks, or replans.
        seed_date: Optional[dt.date] = get_last_mow_date()
        if seed_date:
            logger.debug(
                "Planner: seeding last_mow_date from mow history: %s", seed_date,
            )

        # ── Total mowing today (completed + ongoing), provided by the sampler.
        already_mowed_today_sec: int = 0
        if self._mow_today_provider:
            try:
                already_mowed_today_sec = int(self._mow_today_provider() or 0)
            except Exception as e:
                logger.debug("Planner: mow_today_provider error: %s", e)
        else:
            already_mowed_today_sec = get_mow_sec_today()

        mower = self._get_mower() if self._get_mower else None
        if mower and mower.is_connected():
            try:
                if cycle_data is None:
                    stats = await mower.command("GetAllStatistics")
                    if stats and stats.get("numberOfChargingCycles", 0) > 0:
                        n = stats["numberOfChargingCycles"]
                        avg_mow_h = stats["totalCuttingTime"] / n / 3600
                        avg_chg_h = stats["totalChargingTime"] / n / 3600
                        cycle_data = {"avg_mow_h": avg_mow_h, "avg_chg_h": avg_chg_h}
                        logger.debug(
                            "Planner: cycle data from mower stats — "
                            "avg_mow=%.2fh avg_chg=%.2fh",
                            avg_mow_h, avg_chg_h,
                        )
            except Exception as e:
                logger.warning("Planner: could not read mower stats for cycle data: %s", e)

        # Update cache with whatever we obtained this run; fall back to cache
        # when this run yielded nothing (mower offline, provider returned None).
        if cycle_data is not None:
            self._cycle_data = cycle_data
        elif self._cycle_data is not None:
            cycle_data = self._cycle_data
            logger.debug(
                "Planner: using cached cycle data — avg_mow=%.2fh avg_chg=%.2fh",
                cycle_data["avg_mow_h"], cycle_data["avg_chg_h"],
            )

        sessions, log = plan_schedule(config, periods, daily=daily,
                                      seeded_last_mow_date=seed_date,
                                      cycle_data=cycle_data,
                                      already_mowed_today_sec=already_mowed_today_sec)
        self.last_log = log
        save_plan_log(log)

        # Store future sessions; discard any that are already past.
        now_dt = dt.datetime.now()
        self._planned_sessions = sorted(
            [s for s in sessions if s["start_dt"] > now_dt],
            key=lambda s: s["start_dt"],
        )
        save_planned_sessions(self._planned_sessions)
        msg = f"Planned {len(sessions)} session(s) for next 7 days"
        if self._planned_sessions:
            nxt = self._planned_sessions[0]
            msg += (
                f" — next: {nxt['date']} {nxt['start_dt'].strftime('%H:%M')}"
                f" for {nxt['duration_sec'] // 60} min"
            )
        else:
            msg += " — no upcoming sessions"

        # Ensure placeholder task is installed while planner owns the scheduler.
        if mower and mower.is_connected():
            await self._install_placeholder_if_needed(mower)

        now_str = dt.datetime.now().isoformat(timespec="seconds")
        self.last_run = now_str

        config["last_plan_time"] = now_str
        config["last_plan_result"] = msg
        save_config(config)

        return self._set_result(msg)

    def get_next_planned_session(self) -> Optional[dict]:
        """Return the soonest future planned session, or None."""
        now = dt.datetime.now()
        future = [s for s in self._planned_sessions if s["start_dt"] > now]
        if not future:
            return None
        return min(future, key=lambda s: s["start_dt"])

    async def _install_placeholder_if_needed(self, mower) -> None:
        """
        Ensure the mower's firmware scheduler holds exactly the placeholder task.
        Only writes if the current task list differs, avoiding unnecessary BLE traffic.
        """
        try:
            num = await mower.command("GetNumberOfTasks")
            if num == 1:
                existing = await mower.get_task(0)
                if (existing
                        and existing.next_start_time == _PLACEHOLDER_TASK.next_start_time
                        and existing.duration_in_seconds == _PLACEHOLDER_TASK.duration_in_seconds
                        and existing.on_monday == _PLACEHOLDER_TASK.on_monday):
                    return  # already correct
            await mower.set_schedule([_PLACEHOLDER_TASK])
            logger.info("Planner: placeholder task installed in mower scheduler")
        except Exception as e:
            logger.warning("Planner: could not install placeholder task: %s", e)

    async def _session_executor_loop(self) -> None:
        """
        Executor loop: watches _planned_sessions and dispatches mowing commands
        at the right time.

        For each session:
          1. Sleep until start_dt.
          2. Call mower_override_seconds(duration_sec) to start the session.
          3. After duration_sec has elapsed, call mower_park_home() to return
             the mower to HOME state between sessions.

        If a session is found to already be in progress at startup (server
        restart case), the start command is skipped and only park_home is issued
        at the end.
        """
        _CHECK_S = 30          # normal polling interval (seconds)
        _PRE_CONNECT_S = 120   # assert _needs_connection this many seconds ahead

        while self._stop_event and not self._stop_event.is_set():

            # ── Smart sleep: wake earlier near upcoming events ──────────────
            now = dt.datetime.now()
            sleep_s = float(_CHECK_S)
            if self._active_session:
                end_dt = (
                    self._active_session["start_dt"]
                    + dt.timedelta(seconds=self._active_session["duration_sec"])
                )
                secs_to_end = (end_dt - now).total_seconds()
                sleep_s = min(sleep_s, max(5.0, secs_to_end - 5))
            else:
                future = sorted(
                    [s for s in self._planned_sessions if s["start_dt"] >= now],
                    key=lambda s: s["start_dt"],
                )
                if future:
                    secs = (future[0]["start_dt"] - now).total_seconds()
                    if secs < _CHECK_S:
                        sleep_s = max(1.0, secs)

            slept = 0.0
            while slept < sleep_s:
                if self._stop_event.is_set():
                    return
                chunk = min(5.0, sleep_s - slept)
                await asyncio.sleep(chunk)
                slept += chunk

            now = dt.datetime.now()

            # ── Phase A: tail management for the active session ─────────────
            if self._active_session:
                end_dt = (
                    self._active_session["start_dt"]
                    + dt.timedelta(seconds=self._active_session["duration_sec"])
                )
                if now >= end_dt:
                    mower = self._get_mower() if self._get_mower else None
                    if mower and mower.is_connected():
                        try:
                            await mower.mower_park_home()
                            logger.info(
                                "Planner executor: session ended — %s %s, mower parked",
                                self._active_session["date"],
                                self._active_session["start_dt"].strftime("%H:%M"),
                            )
                        except Exception as e:
                            logger.warning(
                                "Planner executor: park_home after session failed: %s", e
                            )
                    else:
                        logger.warning(
                            "Planner executor: session %s ended but mower not connected "
                            "— override will expire on its own",
                            self._active_session["date"],
                        )
                    self._active_session = None
                # Don't dispatch the next session while one is still running.
                continue

            # ── Phase B: find the next session to dispatch ──────────────────
            if self._user_inhibited:
                # User manually parked — skip all dispatching until resumed.
                self._needs_connection = False
                continue

            future = sorted(
                [s for s in self._planned_sessions
                 if s["start_dt"] >= now - dt.timedelta(seconds=5)],
                key=lambda s: s["start_dt"],
            )
            if not future:
                self._needs_connection = False
                continue

            next_sess = future[0]
            secs_until = (next_sess["start_dt"] - now).total_seconds()

            # Update _needs_connection flag for the BLE idle-sleep logic.
            self._needs_connection = secs_until <= _PRE_CONNECT_S

            # Session is in the past (server restart while session was running).
            if secs_until < -5:
                end_dt = next_sess["start_dt"] + dt.timedelta(seconds=next_sess["duration_sec"])
                if next_sess in self._planned_sessions:
                    self._planned_sessions.remove(next_sess)
                if now < end_dt:
                    logger.info(
                        "Planner executor: session %s %s is mid-run (resumed after restart)"
                        " — will park at %s",
                        next_sess["date"],
                        next_sess["start_dt"].strftime("%H:%M"),
                        end_dt.strftime("%H:%M"),
                    )
                    self._active_session = next_sess
                else:
                    logger.warning(
                        "Planner executor: session %s %s already expired — skipping",
                        next_sess["date"],
                        next_sess["start_dt"].strftime("%H:%M"),
                    )
                continue

            # Not yet time — keep sleeping.
            if secs_until > 1.0:
                continue

            # ── Dispatch ────────────────────────────────────────────────────
            if next_sess in self._planned_sessions:
                self._planned_sessions.remove(next_sess)

            mower = self._get_mower() if self._get_mower else None
            if mower is None or not mower.is_connected():
                logger.warning(
                    "Planner executor: mower not connected at session start %s %s — skipping",
                    next_sess["date"],
                    next_sess["start_dt"].strftime("%H:%M"),
                )
                continue

            try:
                await mower.mower_override_seconds(next_sess["duration_sec"])
                self._active_session = next_sess
                self._needs_connection = False
                logger.info(
                    "Planner executor: session started — %s %s for %d min",
                    next_sess["date"],
                    next_sess["start_dt"].strftime("%H:%M"),
                    next_sess["duration_sec"] // 60,
                )
            except Exception as e:
                logger.error(
                    "Planner executor: failed to start session %s %s: %s",
                    next_sess["date"],
                    next_sess["start_dt"].strftime("%H:%M"),
                    e,
                )

    def _set_result(self, msg: str) -> str:
        self.last_result = msg
        logger.info("Planner: %s", msg)
        return msg

    @staticmethod
    def _seconds_until_next_wakeup(interval_h: float, replan_time_str: str) -> float:
        """
        Return seconds to sleep before the next planning run.
        Wakes up at whichever comes first:
          - now + interval_h hours
          - the next occurrence of replan_time_str (HH:MM, local clock)
        """
        now = dt.datetime.now()
        candidates: list[float] = [max(60.0, interval_h * 3600)]

        if replan_time_str and replan_time_str.strip():
            try:
                h, m = map(int, replan_time_str.strip().split(":"))
                target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if target <= now:               # already passed today
                    target += dt.timedelta(days=1)
                candidates.append((target - now).total_seconds())
            except (ValueError, AttributeError):
                logger.warning("Invalid replan_time format '%s', ignoring", replan_time_str)

        return min(candidates)

    async def _loop(self) -> None:
        """Background loop: plan, then sleep until the next scheduled wakeup."""
        # Wait for the startup reconnect attempt to complete so the first
        # run_once() can actually push to the mower immediately.
        if self._startup_event is not None:
            try:
                await asyncio.wait_for(self._startup_event.wait(), timeout=60.0)
                logger.info("Planner: startup connect attempt done, proceeding with first run")
            except asyncio.TimeoutError:
                logger.warning("Planner: timed out waiting for startup connect (60 s), proceeding anyway")
            self._startup_event = None  # one-shot

        while self._stop_event and not self._stop_event.is_set():
            config = load_config()
            if config.get("enabled"):
                try:
                    await self.run_once()
                except Exception as e:
                    logger.error("Planner loop exception: %s", e, exc_info=True)
                    self._set_result(f"Error: {e}")

            interval_h = max(0.1, float(config.get("replan_interval_hours", 6)))
            replan_time = config.get("replan_time", "")
            sleep_s = self._seconds_until_next_wakeup(interval_h, replan_time)

            next_run = dt.datetime.now() + dt.timedelta(seconds=sleep_s)
            self.next_run_dt = next_run
            logger.info(
                "Planner sleeping %.0f s — next run at %s",
                sleep_s,
                next_run.strftime("%H:%M:%S"),
            )

            # Sleep in 30-second chunks so stop_event is checked frequently
            slept = 0.0
            while slept < sleep_s:
                if self._stop_event.is_set():
                    return
                chunk = min(30.0, sleep_s - slept)
                await asyncio.sleep(chunk)
                slept += chunk


# ─── Weather Watchdog ─────────────────────────────────────────────────────────

class WeatherWatchdog:
    """
    Background watchdog: polls *current* weather at a fixed interval while the
    mower is active.  If conditions deteriorate beyond the configured thresholds
    (or a thunderstorm is detected via WMO code ≥ 95), the mower is parked until
    the next scheduled start.  Once conditions normalise it resumes automatically.

    Uses the same lat/lon and weather thresholds as PlannerAgent
    (planner_config.json).
    """

    def __init__(self) -> None:
        self._bg_task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._get_mower: Optional[Callable] = None
        self._get_planner: Optional[Callable] = None
        self._parked_by_watchdog: bool = False
        # Timestamp when good weather first returned after a watchdog park, used
        # to enforce the configurable drying delay before resuming.
        self._weather_cleared_at: Optional[dt.datetime] = None

        self.last_check: Optional[str] = None
        self.last_status: str = "Never checked"
        self.last_weather: Optional[dict] = None

    def set_mower_provider(self, provider: Callable) -> None:
        """Register a callable that returns the current Mower (or None)."""
        self._get_mower = provider

    def set_planner_provider(self, provider: Callable) -> None:
        """Register a callable that returns the PlannerAgent (or None).

        Lets the watchdog coordinate with the session executor: respect a user
        park (``_user_inhibited``) and resume in a schedule-aware way instead of
        blindly clearing the override.
        """
        self._get_planner = provider

    def is_running(self) -> bool:
        return bool(self._bg_task and not self._bg_task.done())

    def start(self) -> None:
        if not self.is_running():
            self._stop_event = asyncio.Event()
            self._bg_task = asyncio.create_task(self._loop())
            logger.info("WeatherWatchdog started")

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
        logger.info("WeatherWatchdog stopped")

    def _set_status(self, msg: str) -> str:
        self.last_status = msg
        self.last_check = dt.datetime.now().isoformat(timespec="seconds")
        logger.info("WeatherWatchdog: %s", msg)
        return msg

    async def check_once(self) -> str:
        """Fetch current weather and park/resume the mower if needed."""
        config = load_config()
        lat = float(config["location_lat"])
        lon = float(config["location_lon"])
        max_rain = float(config.get("max_rain_mm_h", 0.5))
        max_wind = float(config.get("max_wind_speed_ms", 10.0))

        try:
            data = await fetch_current_weather(lat, lon)
        except Exception as e:
            return self._set_status(f"Weather fetch failed: {e}")

        current = data.get("current", {})
        rain = float(current.get("precipitation", 0.0))
        wind = float(current.get("windspeed_10m", 0.0))
        wmo  = int(current.get("weathercode", 0))
        desc = _WMO_DESCRIPTIONS.get(wmo, f"code {wmo}")

        self.last_weather = {
            "rain_mm": rain,
            "wind_ms": wind,
            "wmo": wmo,
            "description": desc,
            "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        }

        thunderstorm  = wmo >= 95
        bad_conditions = thunderstorm or rain > max_rain or wind > max_wind

        mower = self._get_mower() if self._get_mower else None
        if mower is None or not mower.is_connected():
            if self._parked_by_watchdog:
                self._parked_by_watchdog = False
            self._weather_cleared_at = None
            return self._set_status(
                f"Mower not connected — current weather: {desc}, "
                f"{rain:.1f} mm/h, {wind:.1f} m/s"
            )

        try:
            activity = await mower.mower_activity()
        except Exception as e:
            return self._set_status(f"Could not read mower activity: {e}")

        if bad_conditions and activity in (MowerActivity.MOWING, MowerActivity.GOING_OUT):
            reason_parts = []
            if thunderstorm:
                reason_parts.append(f"thunderstorm ({desc})")
            if rain > max_rain:
                reason_parts.append(f"rain {rain:.1f} > {max_rain} mm/h")
            if wind > max_wind:
                reason_parts.append(f"wind {wind:.1f} > {max_wind} m/s")
            reason = ", ".join(reason_parts)
            try:
                # Use park_home (HOME mode) so the week schedule cannot restart
                # the mower while conditions are still bad.  mower_resume() below
                # will exit HOME mode when the weather clears.
                await mower.mower_park_home()
                self._parked_by_watchdog = True
                self._weather_cleared_at = None  # (re)start drying clock on next clear
                return self._set_status(f"PARKED (HOME): {reason}")
            except Exception as e:
                return self._set_status(f"Park command failed: {e}")

        elif not bad_conditions and self._parked_by_watchdog:
            smart = bool(config.get("enabled"))
            planner = self._get_planner() if self._get_planner else None

            # Fix 3: respect a user park-for-the-day — never auto-resume against
            # the user's explicit intent.  Just drop our flag and stay parked.
            if smart and planner is not None and getattr(planner, "_user_inhibited", False):
                self._parked_by_watchdog = False
                self._weather_cleared_at = None
                return self._set_status(
                    f"Weather cleared but mower parked by user — not resuming ({desc})"
                )

            # ── Drying delay ────────────────────────────────────────────────
            # Give the lawn time to dry after the weather clears before
            # resuming.  The clock starts the first time we see good weather
            # while parked; it is reset whenever conditions turn bad again
            # (handled in the bad-weather branch / status branch below).
            dry_delay_sec = max(0, int(config.get("watchdog_dry_delay_minutes", 0))) * 60
            if dry_delay_sec > 0:
                now = dt.datetime.now()
                if self._weather_cleared_at is None:
                    self._weather_cleared_at = now
                    return self._set_status(
                        f"Weather cleared — drying {dry_delay_sec // 60} min "
                        f"before resume ({desc})"
                    )
                elapsed = (now - self._weather_cleared_at).total_seconds()
                if elapsed < dry_delay_sec:
                    return self._set_status(
                        f"Drying: {int(elapsed) // 60}/{dry_delay_sec // 60} min "
                        f"elapsed before resume ({desc})"
                    )
                # Drying delay satisfied — fall through to resume / replan.
            self._weather_cleared_at = None

            if smart:
                # Fix 1 + 2: schedule-aware resume.  Only restart mowing if a
                # planned session is still within its window; re-issue the
                # override (mirrors the web resume handler) instead of
                # mower_resume(), which would ClearOverride and stop the mower.
                active = getattr(planner, "_active_session", None) if planner else None
                if active is not None:
                    end_dt = (
                        active["start_dt"]
                        + dt.timedelta(seconds=active["duration_sec"])
                    )
                    remaining = int((end_dt - dt.datetime.now()).total_seconds())
                    if remaining > 0:
                        try:
                            await mower.mower_override_seconds(remaining)
                            self._parked_by_watchdog = False
                            return self._set_status(
                                f"RESUMED active session — mowing {remaining // 60} min "
                                f"until {end_dt.strftime('%H:%M')} ({desc})"
                            )
                        except Exception as e:
                            return self._set_status(f"Resume (override) failed: {e}")
                # No active session, or the window already ended (e.g. the drying
                # delay outlasted it): the interrupted session is lost.  Trigger
                # a replan so the planner can schedule a make-up session — today
                # if a window still allows it, otherwise on the next valid day.
                self._parked_by_watchdog = False
                if planner is not None and hasattr(planner, "run_once"):
                    try:
                        await planner.run_once()
                        return self._set_status(
                            f"Weather cleared after drying — session window passed, "
                            f"replanned ({desc})"
                        )
                    except Exception as e:
                        return self._set_status(f"Replan after drying failed: {e}")
                return self._set_status(
                    f"Weather cleared — staying parked until next planned session ({desc})"
                )

            # Classic mode: return to the firmware weekly schedule.
            try:
                await mower.mower_resume()
                self._parked_by_watchdog = False
                return self._set_status(
                    f"RESUMED: conditions cleared — {desc}, "
                    f"{rain:.1f} mm/h, {wind:.1f} m/s"
                )
            except Exception as e:
                return self._set_status(f"Resume command failed: {e}")

        else:
            cond = "BAD" if bad_conditions else "OK"
            parked_note = " (parked by watchdog)" if self._parked_by_watchdog else ""
            activity_name = activity.name if activity else "unknown"
            if bad_conditions:
                # Conditions deteriorated again before the drying delay elapsed
                # (or mower already parked) — reset the drying clock.
                self._weather_cleared_at = None
            return self._set_status(
                f"Weather {cond}: {desc}, {rain:.1f} mm/h, {wind:.1f} m/s"
                f"{parked_note} — mower: {activity_name}"
            )

    async def _loop(self) -> None:
        """Background loop: check current weather at the configured interval."""
        while self._stop_event and not self._stop_event.is_set():
            config = load_config()
            if config.get("watchdog_enabled"):
                try:
                    await self.check_once()
                except Exception as e:
                    logger.error("WeatherWatchdog loop error: %s", e, exc_info=True)

            interval_min = max(1, int(config.get("watchdog_interval_minutes", 5)))
            total = interval_min * 60.0
            slept = 0.0
            while slept < total:
                if self._stop_event.is_set():
                    return
                chunk = min(30.0, total - slept)
                await asyncio.sleep(chunk)
                slept += chunk
