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
import os
import secrets
import datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path
import time
from typing import Optional

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
import subprocess
from bleak import BleakScanner

from automower_ble.mower import Mower
from automower_ble.protocol import (
    MowerState,
    MowerActivity,
    OverrideAction,
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

logger = logging.getLogger("web_app")

# ─── Authentication ───────────────────────────────────────────────────────────
_SESSION_COOKIE = "automower_session"
_SESSION_MAX_AGE = 8 * 3600  # 8 hours
_auth_hash: Optional[bytes] = None        # bcrypt hash; None = auth disabled
_session_secret: str = secrets.token_hex(32)  # overridden by --secret-key

# Simple in-memory rate-limiter for /login (per remote IP)
_login_attempts: dict[str, list[float]] = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_S = 300  # 5 minutes


def _init_auth(password: Optional[str], secret: Optional[str]) -> None:
    """Hash *password* and store it; optionally override the session secret."""
    global _auth_hash, _session_secret
    if password:
        _auth_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
    if secret:
        _session_secret = secret


def _make_session_token() -> str:
    s = URLSafeTimedSerializer(_session_secret, salt="automower-session")
    return s.dumps("ok")


def _verify_session_token(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        s = URLSafeTimedSerializer(_session_secret, salt="automower-session")
        s.loads(token, max_age=_SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _check_rate_limit(ip: str) -> bool:
    """Return True when the IP is allowed to attempt another login."""
    now = dt.datetime.now().timestamp()
    recent = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW_S]
    _login_attempts[ip] = recent
    return len(recent) < _LOGIN_MAX_ATTEMPTS


def _record_failed_attempt(ip: str) -> None:
    now = dt.datetime.now().timestamp()
    _login_attempts.setdefault(ip, []).append(now)


# Initialise from environment variables so `uvicorn web_app:app` works too.
_init_auth(os.environ.get("AUTH_PASSWORD"), os.environ.get("SECRET_KEY"))

# ─── Global connection state ──────────────────────────────────────────────────
_mower: Optional[Mower] = None
_connected: bool = False
_connection_lock = asyncio.Lock()   # serialises connect / disconnect operations

# ─── Auto-reconnect ───────────────────────────────────────────────────────────
_RECONNECT_CONFIG_PATH = Path("reconnect_config.json")
# Saved target: {"address": str, "channel_id": int, "pin": int|None}
_reconnect_cfg: Optional[dict] = None
_reconnect_enabled: bool = False   # user-controlled toggle
_reconnect_task: Optional[asyncio.Task] = None

# ─── Runtime sampler ──────────────────────────────────────────────────────────
_SAMPLES_PATH = Path("runtime_samples.json")
_MAX_SAMPLES = 2000       # ≈ 33 h at 60 s intervals
_SAMPLE_INTERVAL_S = 60

_runtime_samples: list[dict] = []
_sampler_task: Optional[asyncio.Task] = None


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


def _load_runtime_samples() -> None:
    """Load persisted battery/activity samples from disk into memory."""
    global _runtime_samples
    if _SAMPLES_PATH.exists():
        try:
            data = json.loads(_SAMPLES_PATH.read_text())
            _runtime_samples = data.get("samples", [])[-_MAX_SAMPLES:]
            logger.info("Loaded %d runtime samples from %s", len(_runtime_samples), _SAMPLES_PATH)
        except Exception as exc:
            logger.warning("Could not load runtime samples: %s", exc)


def _save_runtime_samples() -> None:
    """Persist runtime samples to disk atomically (compact JSON, no indent)."""
    try:
        _SAMPLES_PATH.write_text(
            json.dumps({"samples": _runtime_samples[-_MAX_SAMPLES:]}, separators=(",", ":"))
        )
    except Exception as exc:
        logger.warning("Failed to save runtime samples: %s", exc)


async def _sampler_loop() -> None:
    """Background task: record (timestamp, battery, activity, charging) every minute."""
    global _runtime_samples
    while True:
        await asyncio.sleep(_SAMPLE_INTERVAL_S)
        if not _connected or _mower is None:
            continue
        try:
            battery, activity, charging = await asyncio.gather(
                _mower.battery_level(),
                _mower.mower_activity(),
                _mower.is_charging(),
            )
            if battery is None or activity is None:
                continue
            _runtime_samples.append({
                "ts": int(dt.datetime.now().timestamp()),
                "battery": battery,
                "activity": activity.value,
                "charging": bool(charging),
            })
            if len(_runtime_samples) > _MAX_SAMPLES:
                _runtime_samples = _runtime_samples[-_MAX_SAMPLES:]
            _save_runtime_samples()
            logger.debug("Sampler: battery=%d%% activity=%s", battery, activity.name)
        except Exception as exc:
            logger.debug("Sampler error (ignored): %s", exc)


async def _cleanup_connection() -> None:
    """Disconnect from the mower and clear global state.

    Must be called instead of bare ``_mower = None`` assignments so the
    underlying BleakClient (and its bleak-retry-connector auto-reconnect
    machinery) is properly torn down and cannot leave a phantom BLE link at
    the OS level while the web app believes it is disconnected.
    """
    global _mower, _connected
    _connected = False
    if _mower is not None:
        m, _mower = _mower, None
        logger.info("Cleanup: disconnecting mower %s", m.address)
        try:
            await m.disconnect()
        except Exception as exc:
            logger.warning("Mower disconnect error (ignored): %s", exc)
        try:
            result = subprocess.run(
                ["bluetoothctl", "disconnect", m.address],
                capture_output=True, text=True, timeout=5, check=False,
            )
            logger.debug("bluetoothctl disconnect: %s", result.stdout.strip() or result.stderr.strip())
        except Exception as exc:
            logger.debug("bluetoothctl cleanup failed: %s", exc)
        logger.info("Cleanup: mower %s disconnected", m.address)


async def _reconnect_loop() -> None:
    """Background loop: scan for saved mower and reconnect automatically."""
    global _mower, _connected
    _SCAN_TIMEOUT = 8.0
    _RETRY_INTERVAL = 30.0

    while True:
        await asyncio.sleep(_RETRY_INTERVAL)

        if not _reconnect_enabled or not _reconnect_cfg:
            continue

        # Sync stale flag: BLE dropped without going through /api/disconnect.
        # Use _cleanup_connection() so the old BleakClient is properly torn
        # down and bleak-retry-connector stops auto-reconnecting at the OS level.
        if _connected and (_mower is None or not _mower.is_connected()):
            logger.info("Auto-reconnect: detected unexpected disconnect")
            await _cleanup_connection()

        if _connected:
            continue  # already connected

        # Skip if /api/connect is already running a connect attempt.
        if _connection_lock.locked():
            continue

        try:
            addr = _reconnect_cfg["address"]
            logger.info("Auto-reconnect: scanning for %s ...", addr)
            device = await BleakScanner.find_device_by_address(addr, timeout=_SCAN_TIMEOUT)
            if device is None:
                # Device not visible in scan — could be a BlueZ zombie connection
                # where BlueZ still holds the link even though Bleak has given up.
                # In that state the mower is not discoverable until BlueZ releases it.
                try:
                    info = subprocess.run(
                        ["bluetoothctl", "info", addr],
                        capture_output=True, text=True, timeout=5, check=False,
                    )
                    if "Connected: yes" in info.stdout:
                        logger.warning(
                            "Auto-reconnect: %s is zombie-connected in BlueZ — "
                            "forcing bluetoothctl disconnect...",
                            addr,
                        )
                        subprocess.run(
                            ["bluetoothctl", "disconnect", addr],
                            timeout=5, check=False,
                        )
                        # BlueZ needs a moment to process the disconnect before
                        # the device becomes scannable again; the 30 s sleep at
                        # the top of the next loop iteration provides that gap.
                    else:
                        logger.debug("Auto-reconnect: %s not in range", addr)
                except Exception as exc:
                    logger.debug("Auto-reconnect: bluetoothctl check failed: %s", exc)
                continue

            logger.info("Auto-reconnect: found %s, connecting ...", addr)
            candidate = Mower(
                _reconnect_cfg["channel_id"],
                addr,
                _reconnect_cfg.get("pin"),
            )
            async with _connection_lock:
                # Re-check: a concurrent /api/connect may have beaten us here.
                if _connected:
                    logger.debug("Auto-reconnect: concurrent connect won the race, aborting")
                    try:
                        await candidate.disconnect()
                    except Exception:
                        pass
                    continue
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
                    try:
                        await candidate.disconnect()
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("Auto-reconnect error: %s", exc)

# ─── Planner agent ────────────────────────────────────────────────────────────
planner = PlannerAgent()
planner.set_mower_provider(lambda: _mower)

watchdog = WeatherWatchdog()
watchdog.set_mower_provider(lambda: _mower)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _reconnect_task, _sampler_task
    # Re-apply our logging config: uvicorn calls configure_logging() internally
    # during startup which resets the handlers we set before uvicorn.run().
    # Calling here guarantees our format is active for all subsequent output.
    _configure_app_logging(_log_level_str, _log_file, _log_max_mb, _log_backups)
    # Startup
    _load_reconnect_state()
    _load_runtime_samples()
    _reconnect_task = asyncio.create_task(_reconnect_loop())
    _sampler_task = asyncio.create_task(_sampler_loop())
    cfg = planner_load_config()
    logger.info(
        "═══ AutoMower-BLE starting ═══  "
        "auth=%s  planner=%s  watchdog=%s  "
        "reconnect_target=%s  samples=%d",
        "on" if _auth_hash else "off",
        "enabled" if cfg.get("enabled") else "disabled",
        "enabled" if cfg.get("watchdog_enabled") else "disabled",
        _reconnect_cfg.get("address") if _reconnect_cfg else "none",
        len(_runtime_samples),
    )
    if cfg.get("enabled"):
        planner.start()
    watchdog.start()  # always running; gated internally by watchdog_enabled flag
    yield
    # Shutdown
    logger.info("AutoMower-BLE shutdown")
    if _reconnect_task:
        _reconnect_task.cancel()
    if _sampler_task:
        _sampler_task.cancel()
    await planner.stop()
    await watchdog.stop()


app = FastAPI(title="Automower BLE Control", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="."), name="static")
templates = Jinja2Templates(directory="templates")


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """Gate every route behind the session cookie when auth is enabled."""
    if _auth_hash is None:
        # Auth not configured — pass all requests through.
        return await call_next(request)
    path = request.url.path
    # Public paths: login form + static assets
    if path in ("/login", "/logout") or path.startswith("/static/"):
        return await call_next(request)
    if not _verify_session_token(request.cookies.get(_SESSION_COOKIE)):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse("/login", status_code=302)
    return await call_next(request)

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


class ParkDurationRequest(BaseModel):
    duration_hours: float = 1.0


# ─── HTML page ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ─── Login / Logout ───────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # Already authenticated → go straight to the app.
    if _verify_session_token(request.cookies.get(_SESSION_COOKIE)):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
async def login(
    request: Request,
    password: str = Form(...),
):
    client_ip = (request.client.host if request.client else None) or "unknown"
    if not _check_rate_limit(client_ip):
        logger.warning("Login rate-limit hit for %s", client_ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many failed attempts — try again in 5 minutes."},
            status_code=429,
        )
    if _auth_hash and bcrypt.checkpw(password.encode(), _auth_hash):
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(
            _SESSION_COOKIE,
            _make_session_token(),
            max_age=_SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return response
    _record_failed_attempt(client_ip)
    logger.warning("Failed login attempt from %s", client_ip)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Incorrect password."},
        status_code=401,
    )


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(_SESSION_COOKIE)
    return response


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

    async with _connection_lock:
        # Re-check inside lock: auto-reconnect may have connected while we scanned.
        if _connected and _mower:
            raise HTTPException(400, "Already connected — disconnect first.")

        candidate = Mower(req.channel_id, req.address, req.pin)
        result = await candidate.connect(device)

        if result == ResponseResult.OK:
            _mower = candidate
            _connected = True
            logger.info(
                "Connected: address=%s  channel_id=%d  pin=%s",
                req.address, req.channel_id, "yes" if req.pin else "no",
            )
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

        # Connect failed — disconnect the candidate to prevent an orphaned BLE link.
        try:
            await candidate.disconnect()
        except Exception:
            pass

        if result == ResponseResult.INVALID_PIN:
            raise HTTPException(401, "Invalid PIN")
        elif result == ResponseResult.NOT_ALLOWED:
            raise HTTPException(
                403,
                "Connection not allowed — the mower may require a PIN or is locked.",
            )
        else:
            raise HTTPException(500, f"Connection failed: {result.name}")


@app.post("/api/disconnect")
async def disconnect_mower():
    """Disconnect from the currently connected mower."""
    global _mower, _connected, _reconnect_enabled
    _require_connection()
    addr = _mower.address
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
    local_time = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await _mower.set_time()
    logger.info("Time sync: mower=%s  local_time=%s", _mower.address, local_time)
    return {"status": "ok", "local_time": local_time}


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
    # Sync stale flag (BLE dropped without explicit /api/disconnect).
    # Properly tear down the old client so bleak-retry-connector does not keep
    # the BLE link alive at the OS level while the app believes it's gone.
    if _connected and (_mower is None or not _mower.is_connected()):
        await _cleanup_connection()
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


_BLADE_REPLACEMENT_INTERVAL_H = 200  # Husqvarna recommended blade interval (hours)


@app.get("/api/statistics")
async def get_statistics():
    """Return usage statistics from the mower."""
    _require_connection()

    # Fetch core lifetime counters and supplementary live data in parallel.
    stats, remaining_charge, override = await asyncio.gather(
        _mower.command("GetAllStatistics"),
        _mower.command("GetRemainingChargingTime"),
        _mower.command("GetOverride"),
    )

    if stats is None:
        raise HTTPException(500, "Failed to retrieve statistics from mower")

    running_s: int = stats["totalRunningTime"]
    cutting_s: int = stats["totalCuttingTime"]
    charging_s: int = stats["totalChargingTime"]
    searching_s: int = stats["totalSearchingTime"]
    collisions: int = stats["numberOfCollisions"]
    charge_cycles: int = stats["numberOfChargingCycles"]
    blade_s: int = stats["cuttingBladeUsageTime"]

    # ── Derived metrics ───────────────────────────────────────────────────────
    cutting_ratio = round(cutting_s / running_s * 100, 1) if running_s else None
    searching_ratio = round(searching_s / running_s * 100, 1) if running_s else None
    avg_charge_h = round(charging_s / charge_cycles / 3600, 2) if charge_cycles else None
    collision_rate = round(collisions / (running_s / 360000), 1) if running_s else None
    blade_wear_pct = round(blade_s / (_BLADE_REPLACEMENT_INTERVAL_H * 3600) * 100, 1)

    # ── Override status ───────────────────────────────────────────────────────
    override_info = None
    if override is not None:
        action_val = override.get("action", 0)
        try:
            action_name = OverrideAction(action_val).name
        except ValueError:
            action_name = f"UNKNOWN_{action_val}"
        if action_val != OverrideAction.NONE:
            override_info = {
                "action": action_name,
                "remaining_seconds": override.get("duration"),
            }

    return {
        # ── Lifetime counters ─────────────────────────────────────────────
        "total_running_hours": round(running_s / 3600, 1),
        "total_cutting_hours": round(cutting_s / 3600, 1),
        "total_charging_hours": round(charging_s / 3600, 1),
        "total_searching_hours": round(searching_s / 3600, 1),
        "number_of_collisions": collisions,
        "number_of_charging_cycles": charge_cycles,
        "cutting_blade_usage_hours": round(blade_s / 3600, 1),
        # ── Derived efficiency / health metrics ───────────────────────────
        "cutting_ratio_pct": cutting_ratio,
        "searching_ratio_pct": searching_ratio,
        "avg_charge_duration_hours": avg_charge_h,
        "collision_rate_per_100h": collision_rate,
        "blade_wear_pct": blade_wear_pct,
        # ── Live supplementary data ───────────────────────────────────────
        "remaining_charging_time_min": (
            round(remaining_charge / 60, 1) if remaining_charge else None
        ),
        "override": override_info,
    }


# ─── Runtime estimation helpers ───────────────────────────────────────────────
_MIN_SEG_SAMPLES = 3      # minimum samples to qualify a segment
_MIN_SEG_MINUTES = 3.0    # minimum segment duration (minutes)


def _analyse_samples(samples: list[dict]) -> dict:
    """
    Segment samples into completed mowing / charging runs and derive rates.
    The last (possibly ongoing) segment is always skipped.
    """
    discharge_rates: list[float] = []
    charge_rates: list[float] = []
    return_thresholds: list[float] = []

    if len(samples) < 2:
        return {
            "discharge_rate_pct_per_h": None,
            "charge_rate_pct_per_h": None,
            "return_threshold_pct": None,
            "mow_segment_count": 0,
            "charge_segment_count": 0,
        }

    # Build completed segments (skip last ongoing one)
    segments: list[tuple[int, list[dict]]] = []
    seg_start = 0
    for i in range(1, len(samples)):
        if samples[i]["activity"] != samples[seg_start]["activity"]:
            segments.append((samples[seg_start]["activity"], samples[seg_start:i]))
            seg_start = i
    # samples[seg_start:] is the current segment — intentionally skipped

    for idx, (act, seg) in enumerate(segments):
        if len(seg) < _MIN_SEG_SAMPLES:
            continue
        duration_h = (seg[-1]["ts"] - seg[0]["ts"]) / 3600
        if duration_h < _MIN_SEG_MINUTES / 60:
            continue

        if act == MowerActivity.MOWING.value:
            drop = seg[0]["battery"] - seg[-1]["battery"]
            if drop > 0:
                discharge_rates.append(drop / duration_h)
            # Capture the battery % at which the mower leaves for home
            if idx + 1 < len(segments):
                next_act = segments[idx + 1][0]
                if next_act in (MowerActivity.GOING_HOME.value, MowerActivity.CHARGING.value):
                    return_thresholds.append(float(seg[-1]["battery"]))

        elif act == MowerActivity.CHARGING.value:
            gain = seg[-1]["battery"] - seg[0]["battery"]
            if gain > 0:
                charge_rates.append(gain / duration_h)

    return {
        "discharge_rate_pct_per_h": round(sum(discharge_rates) / len(discharge_rates), 1) if discharge_rates else None,
        "charge_rate_pct_per_h": round(sum(charge_rates) / len(charge_rates), 1) if charge_rates else None,
        "return_threshold_pct": round(sum(return_thresholds) / len(return_thresholds), 1) if return_thresholds else None,
        "mow_segment_count": len(discharge_rates),
        "charge_segment_count": len(charge_rates),
    }


def _compute_runtime_estimate(
    samples: list[dict],
    stats: Optional[dict],
    current_battery: Optional[int],
    current_activity: Optional[MowerActivity],
    remaining_charge_s: Optional[int],
    duration_hours: float,
) -> dict:
    """
    Estimate mowing vs charging breakdown for a session of *duration_hours*.

    Priority:
      1. Sample-derived rates (discharge/charge %/h + observed return threshold).
      2. Lifetime statistics averages (totalCuttingTime / numberOfChargingCycles etc.).
      3. Returns source="insufficient_data" with no projection when neither is available.
    """
    rates = _analyse_samples(samples)
    discharge_rate = rates["discharge_rate_pct_per_h"]
    charge_rate = rates["charge_rate_pct_per_h"]
    threshold = rates["return_threshold_pct"]

    mow_h: Optional[float] = None
    charge_h: Optional[float] = None
    other_h: float = 0.0
    source = "insufficient_data"

    if discharge_rate and charge_rate and threshold is not None:
        usable_pct = 100.0 - threshold
        mow_h = usable_pct / discharge_rate
        charge_h = usable_pct / charge_rate
        source = "samples"
    elif stats and stats.get("numberOfChargingCycles", 0) > 0:
        n = stats["numberOfChargingCycles"]
        mow_h = stats["totalCuttingTime"] / n / 3600
        charge_h = stats["totalChargingTime"] / n / 3600
        other_h = stats["totalSearchingTime"] / n / 3600
        source = "statistics"

    # ── Current status ────────────────────────────────────────────────────────
    act_val = current_activity.value if current_activity is not None else None
    current_status: Optional[dict] = None

    if act_val is not None and current_battery is not None:
        if act_val == MowerActivity.MOWING.value:
            if discharge_rate and threshold is not None:
                secs = max(0.0, (current_battery - threshold) / discharge_rate * 3600)
                current_status = {"phase": "mowing", "min_until_charge": round(secs / 60)}
            else:
                current_status = {"phase": "mowing", "min_until_charge": None}
        elif act_val == MowerActivity.CHARGING.value:
            current_status = {
                "phase": "charging",
                "min_until_resume": round(remaining_charge_s / 60) if remaining_charge_s else None,
            }
        elif act_val == MowerActivity.GOING_HOME.value:
            current_status = {"phase": "going_home", "min_until_charge": 0}
        else:
            current_status = {"phase": current_activity.name.lower()}

    if mow_h is None or charge_h is None:
        return {
            "sample_count": len(samples),
            "data_source": source,
            "rates": rates,
            "current_status": current_status,
            "projection": None,
        }

    # ── Projection ────────────────────────────────────────────────────────────
    cycle_h = mow_h + charge_h + other_h
    mow_efficiency_pct = round(mow_h / cycle_h * 100, 1)

    full_cycles = int(duration_hours / cycle_h)
    remainder_h = duration_hours - full_cycles * cycle_h
    rem_mow_h = min(remainder_h, mow_h)
    rem_charge_h = max(0.0, remainder_h - mow_h)

    return {
        "sample_count": len(samples),
        "data_source": source,
        "rates": rates,
        "cycle": {
            "avg_mow_h_per_cycle": round(mow_h, 2),
            "avg_charge_h_per_cycle": round(charge_h, 2),
            "cycle_total_h": round(cycle_h, 2),
            "mow_efficiency_pct": mow_efficiency_pct,
        },
        "current_status": current_status,
        "projection": {
            "duration_hours": duration_hours,
            "estimated_mowing_hours": round(full_cycles * mow_h + rem_mow_h, 2),
            "estimated_charging_hours": round(full_cycles * charge_h + rem_charge_h, 2),
            "estimated_charge_stops": full_cycles + (1 if rem_charge_h > 0.0 else 0),
        },
    }


@app.get("/api/runtime_estimate")
async def get_runtime_estimate(duration_hours: float = 3.0):
    """Estimate mowing vs charging time for a planned mowing session."""
    _require_connection()
    if duration_hours <= 0:
        raise HTTPException(422, "duration_hours must be > 0")

    battery, activity, remaining_charge, stats = await asyncio.gather(
        _mower.battery_level(),
        _mower.mower_activity(),
        _mower.command("GetRemainingChargingTime"),
        _mower.command("GetAllStatistics"),
    )

    return _compute_runtime_estimate(
        samples=_runtime_samples,
        stats=stats,
        current_battery=battery,
        current_activity=activity,
        remaining_charge_s=remaining_charge,
        duration_hours=duration_hours,
    )


@app.delete("/api/runtime_samples")
async def clear_runtime_samples():
    """Clear all collected runtime samples, resetting the estimator history."""
    global _runtime_samples
    _runtime_samples = []
    _save_runtime_samples()
    return {"status": "ok", "sample_count": 0}


# ─── Commands ─────────────────────────────────────────────────────────────────
@app.post("/api/command/mow")
async def command_mow(req: MowRequest):
    """Force the mower to mow for the given duration (hours)."""
    _require_connection()
    if req.duration_hours <= 0:
        raise HTTPException(422, "duration_hours must be > 0")
    logger.info("CMD mow: duration=%.1f h  mower=%s", req.duration_hours, _mower.address)
    await _mower.mower_override(req.duration_hours)
    return {"status": "ok", "action": "mow", "duration_hours": req.duration_hours}


@app.post("/api/command/pause")
async def command_pause():
    """Pause the mower."""
    _require_connection()
    logger.info("CMD pause  mower=%s", _mower.address)
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


@app.post("/api/command/park_duration")
async def command_park_duration(req: ParkDurationRequest):
    """Park the mower for the specified duration (hours), then resume normal schedule."""
    _require_connection()
    if req.duration_hours <= 0:
        raise HTTPException(422, "duration_hours must be > 0")
    await _mower.mower_park_duration(req.duration_hours)
    return {"status": "ok", "action": "park_duration", "duration_hours": req.duration_hours}


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
                    "time": dt.datetime.utcfromtimestamp(
                        msg["time"]
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
    hedgehog_protection: bool = False
    heat_stress_protection: bool = False
    heat_stress_temp_celsius: float = 28.0
    heat_stress_no_mow_start_hour: int = 11
    heat_stress_no_mow_end_hour: int = 17
    heat_stress_interval_days: int = 2
    dew_avoidance_enabled: bool = False
    dew_avoidance_auto: bool = True
    dew_avoidance_hours_after_sunrise: float = 2.0


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
    """Return the last fetched weather forecast and daily sunrise/sunset data."""
    return {"forecast": planner.last_forecast, "daily": planner.last_daily}


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
_APP_LOGGERS = ("web_app", "automower_ble", "planner", "uvicorn", "uvicorn.error", "uvicorn.access")

# Module-level storage so lifespan can re-apply logging after uvicorn's
# internal configure_logging() runs and may reset our handlers.
_log_level_str: str = "info"
_log_file: Optional[str] = None
_log_max_mb: int = 10
_log_backups: int = 5


def _configure_app_logging(
    level_str: str,
    log_file: Optional[str] = None,
    log_max_mb: int = 10,
    log_backups: int = 5,
) -> None:
    """
    Configure logging for every automower logger independently of the root
    logger so that uvicorn's own logging initialisation (which calls
    logging.config.dictConfig and may reset root-logger handlers) does not
    silence our output.

    Each automower logger gets its own StreamHandler plus an optional
    RotatingFileHandler.  propagate=False prevents double-printing via root.
    """
    level = getattr(logging, level_str.upper())
    fmt = logging.Formatter(
        "%(levelname)s: %(asctime)s  %(name)-28s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_file:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(
            log_file,
            maxBytes=log_max_mb * 1024 * 1024,
            backupCount=log_backups,
            encoding="utf-8",
        )
        handlers.append(fh)

    for h in handlers:
        h.setFormatter(fmt)
        h.setLevel(level)

    for name in _APP_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.handlers = []          # clear any handlers added at import time
        for h in handlers:
            lg.addHandler(h)
        lg.propagate = False      # don't double-print via root logger

    if log_file:
        logging.getLogger("web_app").info(
            "File logging active: %s  (max %d MB × %d backup(s))",
            log_file, log_max_mb, log_backups,
        )


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
    parser.add_argument(
        "--password",
        default=None,
        help="Web UI password (or set AUTH_PASSWORD env var). "
             "If omitted, auth is disabled.",
    )
    parser.add_argument(
        "--secret-key",
        default=None,
        help="Secret for signing session cookies (or set SECRET_KEY env var). "
             "Defaults to a random value — sessions expire on restart.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Write logs to this rotating file in addition to stderr "
             "(e.g. --log-file automower.log). Useful for multi-day diagnostics.",
    )
    parser.add_argument(
        "--log-max-mb",
        type=int,
        default=10,
        metavar="MB",
        help="Maximum size in MB before the log file is rotated (default: 10).",
    )
    parser.add_argument(
        "--log-backups",
        type=int,
        default=5,
        metavar="N",
        help="Number of rotated log files to keep (default: 5 → up to 50 MB total).",
    )
    args = parser.parse_args()

    # CLI args take precedence over env vars (already applied at import time).
    _init_auth(
        args.password or os.environ.get("AUTH_PASSWORD"),
        args.secret_key or os.environ.get("SECRET_KEY"),
    )
    if _auth_hash is None:
        logging.warning(
            "No --password set: web UI is unprotected. "
            "Pass --password <pw> or set AUTH_PASSWORD to enable authentication."
        )

    _configure_app_logging(
        args.log_level,
        log_file=args.log_file,
        log_max_mb=args.log_max_mb,
        log_backups=args.log_backups,
    )

    # Store params so lifespan can re-apply after uvicorn's internal setup.
    _log_level_str = args.log_level
    _log_file      = args.log_file
    _log_max_mb    = args.log_max_mb
    _log_backups   = args.log_backups

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        log_config=None,   # prevent uvicorn from resetting our log handlers
    )
