"""
Web interface for Husqvarna Automower BLE control.

Usage:
    python web_app.py [--host 127.0.0.1] [--port 8080] [--log-level info]

Then open http://127.0.0.1:8080 in your browser.
"""

# Copyright: AutoMower-BLE contributors

import argparse
import asyncio
import json
import logging
import datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from bleak import BleakScanner

from automower_ble.mower import Mower
from automower_ble.protocol import (
    MowerState,
    MowerActivity,
    ResponseResult,
    TaskInformation,
)
from automower_ble.error_codes import ErrorCodes
from automower_ble.models import MowerModels
from planner import (
    PlannerAgent,
    WeatherWatchdog,
    load_config as planner_load_config,
    save_config as planner_save_config,
    DEFAULT_CONFIG as PLANNER_DEFAULT_CONFIG,
    geocode_city,
)

logger = logging.getLogger(__name__)

# ─── Global connection state ──────────────────────────────────────────────────
_mower: Optional[Mower] = None
_connected: bool = False

# ─── Auto-reconnect ───────────────────────────────────────────────────────────
_RECONNECT_CONFIG_PATH = Path("reconnect_config.json")
# Saved target: {"address": str, "channel_id": int, "pin": int|None}
_reconnect_cfg: Optional[dict] = None
_reconnect_enabled: bool = False   # user-controlled toggle
_reconnect_task: Optional[asyncio.Task] = None


def _load_reconnect_state() -> None:
    global _reconnect_cfg, _reconnect_enabled
    if _RECONNECT_CONFIG_PATH.exists():
        try:
            data = json.loads(_RECONNECT_CONFIG_PATH.read_text())
            _reconnect_cfg = data.get("target")
            _reconnect_enabled = bool(data.get("enabled", False))
        except Exception:
            pass


def _save_reconnect_state() -> None:
    _RECONNECT_CONFIG_PATH.write_text(json.dumps(
        {"target": _reconnect_cfg, "enabled": _reconnect_enabled}, indent=2
    ))


async def _reconnect_loop() -> None:
    """Background loop: scan for saved mower and reconnect automatically."""
    global _mower, _connected
    _SCAN_TIMEOUT = 8.0
    _RETRY_INTERVAL = 30.0

    while True:
        await asyncio.sleep(_RETRY_INTERVAL)

        if not _reconnect_enabled or not _reconnect_cfg:
            continue

        # Sync stale flag: BLE dropped without going through /api/disconnect
        if _connected and (_mower is None or not _mower.is_connected()):
            logger.info("Auto-reconnect: detected unexpected disconnect")
            _connected = False
            _mower = None

        if _connected:
            continue  # already connected

        try:
            addr = _reconnect_cfg["address"]
            logger.info("Auto-reconnect: scanning for %s ...", addr)
            device = await BleakScanner.find_device_by_address(addr, timeout=_SCAN_TIMEOUT)
            if device is None:
                logger.debug("Auto-reconnect: %s not in range", addr)
                continue

            logger.info("Auto-reconnect: found %s, connecting ...", addr)
            candidate = Mower(
                _reconnect_cfg["channel_id"],
                addr,
                _reconnect_cfg.get("pin"),
            )
            result = await candidate.connect(device)
            if result == ResponseResult.OK:
                _mower = candidate
                _connected = True
                logger.info("Auto-reconnect: connected to %s ✓", addr)
                # Sync clock, then push pending schedule
                try:
                    await _mower.set_time()
                except Exception as te:
                    logger.warning("Auto-reconnect: time sync failed: %s", te)
                asyncio.create_task(planner.run_once())
            else:
                logger.warning("Auto-reconnect: failed (%s)", result.name)
        except Exception as exc:
            logger.warning("Auto-reconnect error: %s", exc)

# ─── Planner agent ────────────────────────────────────────────────────────────
planner = PlannerAgent()
planner.set_mower_provider(lambda: _mower)

watchdog = WeatherWatchdog()
watchdog.set_mower_provider(lambda: _mower)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _reconnect_task
    # Startup
    _load_reconnect_state()
    _reconnect_task = asyncio.create_task(_reconnect_loop())
    cfg = planner_load_config()
    if cfg.get("enabled"):
        planner.start()
    watchdog.start()  # always running; gated internally by watchdog_enabled flag
    yield
    # Shutdown
    if _reconnect_task:
        _reconnect_task.cancel()
    await planner.stop()
    await watchdog.stop()


app = FastAPI(title="Automower BLE Control", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="."), name="static")
templates = Jinja2Templates(directory="templates")

HUSQVARNA_COMPANY_ID = 0x0426


# ─── Pydantic models ──────────────────────────────────────────────────────────
class ConnectRequest(BaseModel):
    address: str
    channel_id: int = 1197489078
    pin: Optional[int] = None


class TaskModel(BaseModel):
    start_seconds: int      # seconds since midnight (0-86399)
    duration_seconds: int   # duration in seconds
    monday: bool = False
    tuesday: bool = False
    wednesday: bool = False
    thursday: bool = False
    friday: bool = False
    saturday: bool = False
    sunday: bool = False


class ScheduleRequest(BaseModel):
    tasks: list[TaskModel]


class MowRequest(BaseModel):
    duration_hours: float = 3.0


# ─── HTML page ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ─── BLE Scan ────────────────────────────────────────────────────────────────
@app.get("/api/scan")
async def scan_devices(timeout: float = 10.0):
    """Scan for nearby BLE devices. Husqvarna devices are flagged."""
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results = []
    for d, a in devices.values():
        mfr_id = next(iter(a.manufacturer_data.keys()), None)
        results.append(
            {
                "address": d.address,
                "name": d.name or "Unknown",
                "rssi": a.rssi,
                "husqvarna": mfr_id == HUSQVARNA_COMPANY_ID,
            }
        )
    results.sort(key=lambda x: (not x["husqvarna"], -(x["rssi"] or -100)))
    return results


# ─── Connect / Disconnect ─────────────────────────────────────────────────────
@app.post("/api/connect")
async def connect_mower(req: ConnectRequest):
    """Connect and pair with a mower by BLE address."""
    global _mower, _connected, _reconnect_cfg, _reconnect_enabled

    if _connected and _mower:
        raise HTTPException(400, "Already connected — disconnect first.")

    device = await BleakScanner.find_device_by_address(req.address)
    if device is None:
        raise HTTPException(404, f"BLE device not found: {req.address}")

    _mower = Mower(req.channel_id, req.address, req.pin)
    result = await _mower.connect(device)

    if result == ResponseResult.OK:
        _connected = True
        # Sync mower clock to local time immediately after pairing
        try:
            await _mower.set_time()
        except Exception as te:
            logger.warning("Time sync after connect failed: %s", te)
        # Persist target so auto-reconnect can find it after a dropout
        _reconnect_cfg = {"address": req.address, "channel_id": req.channel_id, "pin": req.pin}
        _reconnect_enabled = True
        _save_reconnect_state()
        return {"status": "connected", "address": req.address}
    elif result == ResponseResult.INVALID_PIN:
        _mower = None
        raise HTTPException(401, "Invalid PIN")
    elif result == ResponseResult.NOT_ALLOWED:
        _mower = None
        raise HTTPException(
            403,
            "Connection not allowed — the mower may require a PIN or is locked.",
        )
    else:
        _mower = None
        raise HTTPException(500, f"Connection failed: {result.name}")


@app.post("/api/disconnect")
async def disconnect_mower():
    """Disconnect from the currently connected mower."""
    global _mower, _connected, _reconnect_enabled
    _require_connection()
    await _mower.disconnect()
    _mower = None
    _connected = False
    # Disable auto-reconnect so we don't immediately re-connect after user disconnect
    _reconnect_enabled = False
    _save_reconnect_state()
    return {"status": "disconnected"}


@app.post("/api/sync_time")
async def sync_time():
    """Synchronise the mower's internal clock to the current local time."""
    _require_connection()
    await _mower.set_time()
    return {
        "status": "ok",
        "local_time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.post("/api/reconnect/toggle")
async def toggle_auto_reconnect(enabled: bool):
    """Enable or disable the auto-reconnect background loop."""
    global _reconnect_enabled
    if enabled and not _reconnect_cfg:
        raise HTTPException(400, "No saved mower address — connect manually first.")
    _reconnect_enabled = enabled
    _save_reconnect_state()
    return {"auto_reconnect": _reconnect_enabled, "target": _reconnect_cfg.get("address") if _reconnect_cfg else None}


@app.get("/api/connection")
async def connection_status():
    """Return whether a mower is currently connected."""
    global _connected, _mower
    # Sync stale flag (BLE dropped without explicit /api/disconnect)
    if _connected and (_mower is None or not _mower.is_connected()):
        _connected = False
        _mower = None
    return {
        "connected": _connected,
        "address": _mower.address if _connected and _mower else None,
        "auto_reconnect": _reconnect_enabled,
        "reconnect_target": _reconnect_cfg.get("address") if _reconnect_cfg else None,
        "saved_channel_id": _reconnect_cfg.get("channel_id") if _reconnect_cfg else None,
        "saved_pin": _reconnect_cfg.get("pin") if _reconnect_cfg else None,
    }


# ─── Status ───────────────────────────────────────────────────────────────────
@app.get("/api/status")
async def get_status():
    """Query all relevant status information from the mower."""
    _require_connection()

    state = await _mower.mower_state()
    activity = await _mower.mower_activity()
    battery = await _mower.battery_level()
    charging = await _mower.is_charging()
    next_start = await _mower.mower_next_start_time()
    mower_name = await _mower.command("GetUserMowerNameAsAsciiString")
    serial = await _mower.command("GetSerialNumber")
    manufacturer = await _mower.get_manufacturer()
    model = await _mower.get_model()
    error_code = await _mower.command("GetError")

    try:
        error_name = ErrorCodes(error_code).name if error_code else "NO_ERROR"
    except ValueError:
        error_name = f"UNKNOWN_{error_code}"

    return {
        "connected": True,
        "address": _mower.address,
        "mower_name": mower_name,
        "serial_number": serial,
        "manufacturer": manufacturer,
        "model": model,
        "state": state.name if state is not None else None,
        "state_description": _state_description(state),
        "activity": activity.name if activity is not None else None,
        "activity_description": _activity_description(activity),
        "battery_level": battery,
        "charging": charging,
        "next_start_time": next_start.isoformat() if next_start else None,
        "error_code": error_code,
        "error_name": error_name,
    }


@app.get("/api/statistics")
async def get_statistics():
    """Return usage statistics from the mower."""
    _require_connection()
    stats = await _mower.command("GetAllStatistics")
    if stats is None:
        raise HTTPException(500, "Failed to retrieve statistics from mower")
    return {
        "total_running_hours": round(stats["totalRunningTime"] / 3600, 1),
        "total_cutting_hours": round(stats["totalCuttingTime"] / 3600, 1),
        "total_charging_hours": round(stats["totalChargingTime"] / 3600, 1),
        "total_searching_hours": round(stats["totalSearchingTime"] / 3600, 1),
        "number_of_collisions": stats["numberOfCollisions"],
        "number_of_charging_cycles": stats["numberOfChargingCycles"],
        "cutting_blade_usage_hours": round(stats["cuttingBladeUsageTime"] / 3600, 1),
    }


# ─── Commands ─────────────────────────────────────────────────────────────────
@app.post("/api/command/mow")
async def command_mow(req: MowRequest):
    """Force the mower to mow for the given duration (hours)."""
    _require_connection()
    if req.duration_hours <= 0:
        raise HTTPException(422, "duration_hours must be > 0")
    await _mower.mower_override(req.duration_hours)
    return {"status": "ok", "action": "mow", "duration_hours": req.duration_hours}


@app.post("/api/command/pause")
async def command_pause():
    """Pause the mower."""
    _require_connection()
    await _mower.mower_pause()
    return {"status": "ok", "action": "pause"}


@app.post("/api/command/resume")
async def command_resume():
    """Resume the mower (continues according to schedule)."""
    _require_connection()
    await _mower.mower_resume()
    return {"status": "ok", "action": "resume"}


@app.post("/api/command/park")
async def command_park():
    """Park the mower until the next scheduled start."""
    _require_connection()
    await _mower.mower_park()
    return {"status": "ok", "action": "park_until_next_start"}


@app.post("/api/command/park_home")
async def command_park_home():
    """
    Park the mower until further notice (HOME mode).
    The mower ignores the week schedule and cannot be force-started.
    """
    _require_connection()
    await _mower.mower_park_home()
    return {"status": "ok", "action": "park_until_further_notice"}


# ─── Schedule ─────────────────────────────────────────────────────────────────
@app.get("/api/schedule")
async def get_schedule():
    """Read the full mowing schedule (all tasks) from the mower."""
    _require_connection()
    num = await _mower.command("GetNumberOfTasks")
    if num is None:
        raise HTTPException(500, "Failed to get task count from mower")

    tasks = []
    for i in range(num):
        task = await _mower.get_task(i)
        if task is not None:
            tasks.append(
                {
                    "task_id": i,
                    "start_seconds": task.next_start_time,
                    "start_time": _seconds_to_hhmm(task.next_start_time),
                    "duration_seconds": task.duration_in_seconds,
                    "duration_str": _seconds_to_duration_str(task.duration_in_seconds),
                    "monday": bool(task.on_monday),
                    "tuesday": bool(task.on_tuesday),
                    "wednesday": bool(task.on_wednesday),
                    "thursday": bool(task.on_thursday),
                    "friday": bool(task.on_friday),
                    "saturday": bool(task.on_saturday),
                    "sunday": bool(task.on_sunday),
                }
            )
    return {"task_count": num, "tasks": tasks}


@app.post("/api/schedule")
async def set_schedule(schedule: ScheduleRequest):
    """
    Replace the entire mowing schedule.
    Sends: StartTaskTransaction → DeleteAllTask → AddTask×N → CommitTaskTransaction.
    """
    _require_connection()

    from automower_ble.protocol import TaskInformation

    task_objects = [
        TaskInformation(
            next_start_time=t.start_seconds,
            duration_in_seconds=t.duration_seconds,
            on_monday=t.monday,
            on_tuesday=t.tuesday,
            on_wednesday=t.wednesday,
            on_thursday=t.thursday,
            on_friday=t.friday,
            on_saturday=t.saturday,
            on_sunday=t.sunday,
        )
        for t in schedule.tasks
    ]
    await _mower.set_schedule(task_objects)
    return {"status": "ok", "tasks_set": len(schedule.tasks)}


# ─── Messages ─────────────────────────────────────────────────────────────────
@app.get("/api/messages")
async def get_messages(count: int = 10):
    """Return the most recent mower log messages (up to `count`)."""
    _require_connection()
    num = await _mower.command("GetNumberOfMessages")
    if num is None:
        raise HTTPException(500, "Failed to get message count")
    messages = []
    for i in range(min(count, num)):
        msg = await _mower.command("GetMessage", messageId=i)
        if msg:
            try:
                code_name = ErrorCodes(msg["code"]).name
            except ValueError:
                code_name = f"UNKNOWN_{msg['code']}"
            messages.append(
                {
                    "message_id": i,
                    "time": dt.datetime.fromtimestamp(
                        msg["time"], dt.UTC
                    ).isoformat(),
                    "code": msg["code"],
                    "code_name": code_name,
                    "severity": msg["severity"],
                }
            )
    return {"total": num, "messages": messages}


# ─── Internal helpers ─────────────────────────────────────────────────────────
def _require_connection() -> None:
    if not _connected or _mower is None:
        raise HTTPException(400, "Not connected to any mower")


def _seconds_to_hhmm(seconds: int) -> str:
    h = (seconds // 3600) % 24
    m = (seconds % 3600) // 60
    return f"{h:02d}:{m:02d}"


def _seconds_to_duration_str(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m:02d}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _state_description(state: Optional[MowerState]) -> str:
    if state is None:
        return "Unknown"
    _descriptions = {
        MowerState.OFF: "Mower is turned off",
        MowerState.WAIT_FOR_SAFETYPIN: "Waiting for safety pin insertion",
        MowerState.STOPPED: "Stopped — requires manual action",
        MowerState.FATAL_ERROR: "Fatal error",
        MowerState.PENDING_START: "Pending start",
        MowerState.PAUSED: "Paused by user",
        MowerState.IN_OPERATION: "In operation (see activity for details)",
        MowerState.RESTRICTED: "Restricted by week calendar or override park",
        MowerState.ERROR: "Error — check error code",
    }
    return _descriptions.get(state, f"Unknown state ({state})")


def _activity_description(activity: Optional[MowerActivity]) -> str:
    if activity is None:
        return "Unknown"
    _descriptions = {
        MowerActivity.NONE: "No current activity",
        MowerActivity.CHARGING: "Charging in station (low battery)",
        MowerActivity.GOING_OUT: "Leaving charging station",
        MowerActivity.MOWING: "Mowing lawn",
        MowerActivity.GOING_HOME: "Returning to charging station",
        MowerActivity.PARKED: "Parked",
        MowerActivity.STOPPED_IN_GARDEN: "Stopped in garden — needs manual action",
    }
    return _descriptions.get(activity, f"Unknown activity ({activity})")


# ─── Planner API ─────────────────────────────────────────────────────────────

class WindowModel(BaseModel):
    day: int           # 0=Monday … 6=Sunday
    start_hour: int    # 0–23
    end_hour: int      # 1–24


class PlannerConfigRequest(BaseModel):
    enabled: bool = False
    location_lat: float = 52.52
    location_lon: float = 13.40
    location_name: str = ""
    available_windows: list[WindowModel] = []
    target_hours_per_day: float = 2.0
    min_duration_minutes: int = 30
    max_duration_minutes: int = 180
    mowing_interval_days: int = 2
    replan_interval_hours: float = 6.0
    replan_time: str = ""  # HH:MM daily fixed time, empty = interval only
    max_wind_speed_ms: float = 10.0
    max_rain_mm_h: float = 0.5
    min_temp_celsius: float = 5.0
    rain_delay_minutes: int = 0
    watchdog_enabled: bool = False
    watchdog_interval_minutes: int = 5


@app.get("/api/planner/config")
async def get_planner_config():
    """Return the current planner configuration."""
    cfg = planner_load_config()
    # Strip runtime-only keys from the response
    cfg.pop("last_plan_time", None)
    cfg.pop("last_plan_result", None)
    return cfg


@app.post("/api/planner/config")
async def set_planner_config(req: PlannerConfigRequest):
    """Save planner configuration and restart the background agent if needed."""
    existing = planner_load_config()
    cfg = req.model_dump()
    # Convert WindowModel objects to plain dicts
    cfg["available_windows"] = [w.model_dump() for w in req.available_windows]
    # Preserve persisted runtime state
    cfg["last_plan_time"] = existing.get("last_plan_time")
    cfg["last_plan_result"] = existing.get("last_plan_result")
    planner_save_config(cfg)

    # Start or stop background agent based on new enabled state
    if cfg["enabled"]:
        if not planner.is_running():
            planner.start()
    else:
        await planner.stop()

    return {"status": "ok", "enabled": cfg["enabled"]}


@app.post("/api/planner/run")
async def run_planner_now():
    """Trigger an immediate planning run."""
    result = await planner.run_once()
    return {"status": "ok", "result": result}


@app.get("/api/planner/status")
async def get_planner_status():
    """Return the planner's runtime status and last planning log."""
    cfg = planner_load_config()
    return {
        "enabled": cfg.get("enabled", False),
        "running": planner.is_running(),
        "last_run": planner.last_run or cfg.get("last_plan_time"),
        "last_result": planner.last_result or cfg.get("last_plan_result", "Never run"),
        "last_log": planner.last_log,
        "replan_interval_hours": cfg.get("replan_interval_hours", 6.0),
        "replan_time": cfg.get("replan_time", ""),
    }


@app.get("/api/planner/forecast")
async def get_planner_forecast():
    """Return the last fetched weather forecast (as returned by the planner)."""
    return {"forecast": planner.last_forecast}


@app.get("/api/planner/geocode")
async def geocode(city: str):
    """Geocode a city name using Open-Meteo — returns lat/lon candidates."""
    try:
        results = await geocode_city(city)
    except Exception as e:
        raise HTTPException(500, f"Geocoding failed: {e}") from e
    return results


# ─── Weather Watchdog API ─────────────────────────────────────────────────────

@app.get("/api/watchdog/status")
async def get_watchdog_status():
    """Return the weather watchdog's current state and last observed weather."""
    cfg = planner_load_config()
    return {
        "enabled": cfg.get("watchdog_enabled", False),
        "interval_minutes": cfg.get("watchdog_interval_minutes", 5),
        "running": watchdog.is_running(),
        "parked_by_watchdog": watchdog._parked_by_watchdog,
        "last_check": watchdog.last_check,
        "last_status": watchdog.last_status,
        "last_weather": watchdog.last_weather,
    }


@app.post("/api/watchdog/check")
async def watchdog_check_now():
    """Trigger an immediate weather check (ignores watchdog_enabled flag)."""
    result = await watchdog.check_once()
    return {"status": "ok", "result": result}


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automower BLE Web Interface")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port to listen on (default: 8080)"
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Logging level",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)-15s %(name)-8s %(levelname)s: %(message)s",
    )

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
