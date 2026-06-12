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
    # Runtime state (persisted for display only)
    "last_plan_time": None,
    "last_plan_result": "Never run",
}


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
        "hourly": "temperature_2m,precipitation,windspeed_10m,weathercode",
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
    temps  = hourly.get("temperature_2m", [])
    rains  = hourly.get("precipitation", [])
    winds  = hourly.get("windspeed_10m", [])
    codes  = hourly.get("weathercode", [])

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
        })
    return periods


def forecast_to_serialisable(periods: list[dict]) -> list[dict]:
    return [{**p, "dt": p["dt"].isoformat()} for p in periods]


# ─── Planning logic ────────────────────────────────────────────────────────────

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


def plan_schedule(
    config: dict,
    forecast_periods: list[dict],
) -> tuple[list[TaskInformation], list[dict]]:
    """
    Build a mowing schedule for the next 7 days.

    Returns (tasks, planning_log).
    planning_log is a list of per-day decision dicts for display.
    """
    today = dt.date.today()

    min_dur_sec = int(config.get("min_duration_minutes", 30)) * 60
    max_dur_sec = int(config.get("max_duration_minutes", 180)) * 60
    target_dur_sec = int(float(config.get("target_hours_per_day", 2.0)) * 3600)
    interval_days = max(1, int(config.get("mowing_interval_days", 2)))
    max_rain = float(config.get("max_rain_mm_h",
                                config.get("max_rain_mm_3h", 0.5)))  # fallback for old configs
    max_wind = float(config.get("max_wind_speed_ms", 10.0))
    min_temp = float(config.get("min_temp_celsius", 5.0))
    rain_delay_sec = int(config.get("rain_delay_minutes", 0)) * 60

    windows: list[dict] = config.get("available_windows", [])

    tasks: list[TaskInformation] = []
    log: list[dict] = []
    last_mow_date: Optional[dt.date] = None
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

        # 2. Interval constraint
        if last_mow_date is not None and (date - last_mow_date).days < interval_days:
            _log(
                "interval",
                f"Interval: {interval_days}d min, last was {last_mow_date.strftime('%a %d %b')}",
            )
            continue

        # 3-5. Iterate over ALL configured windows for this day and accumulate
        #      sessions until the daily target is met or windows are exhausted.
        day_sessions: list[tuple[int, int, str]] = []  # (start_sec, dur_sec, reason)
        remaining_sec = target_dur_sec
        fail_status = "weather"
        fail_reason = "No suitable weather window found"

        for win in day_windows:
            if remaining_sec < min_dur_sec or len(day_sessions) >= 2:
                break  # daily target met or mower's 2-slot-per-day limit reached

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

            # Keep finding sub-windows within this window until target is met
            # (mower firmware allows at most 2 tasks per day across all windows)
            search_start_sec = w_start_h * 3600
            while (remaining_sec >= min_dur_sec
                   and search_start_sec < w_end_h * 3600
                   and len(day_sessions) < 2):
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
                day_sessions.append((start_s, sess_s, reason))
                remaining_sec -= sess_s
                search_start_sec = start_s + sess_s  # next search starts after this session

        if not day_sessions:
            _log(fail_status, fail_reason)
            continue

        # ✓ Create a TaskInformation for every planned session
        flags = [False] * 7
        flags[dow] = True
        for (start_s, sess_s, _) in day_sessions:
            tasks.append(TaskInformation(
                next_start_time=start_s,
                duration_in_seconds=sess_s,
                on_monday=flags[0], on_tuesday=flags[1], on_wednesday=flags[2],
                on_thursday=flags[3], on_friday=flags[4],
                on_saturday=flags[5], on_sunday=flags[6],
            ))
        last_mow_date = date

        log_reason = (
            day_sessions[0][2]
            if len(day_sessions) == 1
            else f"{len(day_sessions)} sessions planned"
        )
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

    return tasks, log


def _tasks_fingerprint(tasks: list[TaskInformation]) -> list[tuple]:
    """Canonical sortable representation used to detect unchanged schedules."""
    return sorted(
        (
            t.next_start_time, t.duration_in_seconds,
            t.on_monday, t.on_tuesday, t.on_wednesday,
            t.on_thursday, t.on_friday, t.on_saturday, t.on_sunday,
        )
        for t in tasks
    )


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

        self.last_log: list[dict] = []
        self.last_run: Optional[str] = None
        self.last_result: str = "Never run"
        self.last_forecast: list[dict] = []
        self._last_pushed_fp: Optional[list[tuple]] = None  # None = not yet verified against mower

    def set_mower_provider(self, provider: Callable) -> None:
        """Register a callable that returns the current Mower (or None)."""
        self._get_mower = provider

    def is_running(self) -> bool:
        return bool(self._bg_task and not self._bg_task.done())

    def start(self) -> None:
        if not self.is_running():
            self._stop_event = asyncio.Event()
            self._bg_task = asyncio.create_task(self._loop())
            logger.info("PlannerAgent started")

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            try:
                await self._bg_task
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
        self.last_forecast = forecast_to_serialisable(periods)

        tasks, log = plan_schedule(config, periods)
        self.last_log = log

        msg = f"Planned {len(tasks)} mowing session(s)"
        mower = self._get_mower() if self._get_mower else None
        if mower and mower.is_connected():
            new_fp = _tasks_fingerprint(tasks)

            # Fast path: in-memory fingerprint matches — no BLE traffic needed
            if new_fp == self._last_pushed_fp:
                msg += " — schedule unchanged (cached), push skipped"
            else:
                # Check mower activity before doing anything
                try:
                    activity = await mower.mower_activity()
                except Exception as e:
                    activity = None
                    logger.warning("Planner: could not read mower activity: %s", e)

                _SAFE = {MowerActivity.PARKED, MowerActivity.CHARGING, MowerActivity.NONE}
                if activity not in _SAFE:
                    act_name = activity.name if activity else "unknown"
                    msg += (f" — push skipped: mower is {act_name} "
                            f"(only push when PARKED / CHARGING)")
                else:
                    # Read-before-write: pull the real schedule and compare
                    try:
                        current_tasks = await mower.get_all_tasks()
                        current_fp = _tasks_fingerprint(current_tasks)
                    except Exception as e:
                        current_fp = None
                        logger.warning("Planner: could not read current schedule: %s", e)

                    if current_fp == new_fp:
                        # Mower already has the correct schedule
                        self._last_pushed_fp = new_fp
                        msg += " — mower schedule already up to date, push skipped"
                    else:
                        try:
                            await mower.set_schedule(tasks)
                            self._last_pushed_fp = new_fp
                            msg += " — schedule pushed to mower ✓"
                        except Exception as e:
                            msg += f" — push failed: {e}"
        else:
            msg += " — mower not connected (not pushed)"

        now_str = dt.datetime.now().isoformat(timespec="seconds")
        self.last_run = now_str

        config["last_plan_time"] = now_str
        config["last_plan_result"] = msg
        save_config(config)

        return self._set_result(msg)

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
        self._parked_by_watchdog: bool = False

        self.last_check: Optional[str] = None
        self.last_status: str = "Never checked"
        self.last_weather: Optional[dict] = None

    def set_mower_provider(self, provider: Callable) -> None:
        """Register a callable that returns the current Mower (or None)."""
        self._get_mower = provider

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
                return self._set_status(f"PARKED (HOME): {reason}")
            except Exception as e:
                return self._set_status(f"Park command failed: {e}")

        elif not bad_conditions and self._parked_by_watchdog:
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
