"""
Regenerate the web-UI screenshots in docs/images/.

Runs the real web app against *synthetic* demo data (fake weather, fake mow
history, fake mower status) in a temporary directory, so no Bluetooth hardware,
network access or personal configuration is needed and nothing private ends up
in the pictures.

    pip install -e ".[ui]"
    playwright install chromium
    python docs/make_screenshots.py
"""

import datetime as dt
import json
import math
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import planner as planner_mod  # noqa: E402
import web_app  # noqa: E402

TODAY = dt.date.today()
UTC_OFFSET = int(dt.datetime.now().astimezone().utcoffset().total_seconds())


# ─── Synthetic data ───────────────────────────────────────────────────────────
def _daily_mean(off: int) -> float:
    base = 18 + 3 * math.sin(off / 3.0)
    return base + (6 if off == 1 else 0)  # one hot day to show heat protection


def _rain_hours(off: int) -> dict[int, float]:
    if off == -11:
        return {h: 1.0 for h in range(10, 18)}
    if off == 3:
        return {h: 1.2 for h in range(13, 18)}
    if off == 4:
        return {h: 1.0 for h in range(8, 16)}
    return {}


def fake_openmeteo(lat: float, lon: float) -> dict:
    times, temp, rain, wind, code, dew, rh = [], [], [], [], [], [], []
    d_time, d_sr, d_ss, d_max, d_mean, d_prec, d_et0, d_wind = ([] for _ in range(8))
    for off in range(-14, 7):
        day = TODAY + dt.timedelta(days=off)
        mean, rains = _daily_mean(off), _rain_hours(off)
        temps = []
        for h in range(24):
            t = mean + 5 * math.sin(2 * math.pi * (h - 9) / 24)
            temps.append(t)
            times.append(f"{day.isoformat()}T{h:02d}:00")
            temp.append(round(t, 1))
            rain.append(rains.get(h, 0.0))
            wind.append(round(2.5 + 1.5 * math.sin(h / 4 + off), 1))
            code.append(61 if h in rains else [0, 1, 2, 3][(h // 6 + off) % 4])
            dew.append(round(t - (1.0 if 3 <= h <= 8 else 6.0), 1))
            rh.append(95 if 3 <= h <= 8 else 62)
        d_time.append(day.isoformat())
        d_sr.append(f"{day.isoformat()}T06:10")
        d_ss.append(f"{day.isoformat()}T20:50")
        d_max.append(round(max(temps), 1))
        d_mean.append(round(mean, 1))
        d_prec.append(round(sum(rains.values()), 1))
        d_et0.append(round(0.9 + 0.08 * mean, 2))
        d_wind.append(6.0)
    return {
        "utc_offset_seconds": UTC_OFFSET,
        "hourly": {
            "time": times, "temperature_2m": temp, "precipitation": rain,
            "windspeed_10m": wind, "weathercode": code,
            "dew_point_2m": dew, "relative_humidity_2m": rh,
        },
        "daily": {
            "time": d_time, "sunrise": d_sr, "sunset": d_ss,
            "temperature_2m_max": d_max, "temperature_2m_mean": d_mean,
            "wind_speed_10m_max": d_wind, "precipitation_sum": d_prec,
            "et0_fao_evapotranspiration": d_et0,
        },
    }


async def _fake_fetch(lat: float, lon: float) -> dict:
    return fake_openmeteo(lat, lon)


def write_demo_files(tmp: Path) -> None:
    windows = []
    for day in range(6):  # Mon–Sat
        windows.append({"day": day, "start_hour": 8, "end_hour": 18})
    cfg = {
        **planner_mod.DEFAULT_CONFIG,
        "enabled": True,
        "location_lat": 52.52, "location_lon": 13.405,
        "location_name": "Berlin, Germany",
        "available_windows": windows,
        "replan_interval_hours": 6.0, "replan_time": "03:30",
        "watchdog_enabled": True, "watchdog_dry_delay_minutes": 30,
        "hedgehog_protection": True,
        "heat_stress_protection": True,
        "dew_avoidance_enabled": True,
        "adaptive_enabled": True,
        "adaptive_session_hours": 3.0,
        "adaptive_peak_interval_days": 2,
    }
    (tmp / "planner_config.json").write_text(json.dumps(cfg, indent=2))
    history = {}
    for off in range(-14, 0):
        day = TODAY + dt.timedelta(days=off)
        if off in (-8, -7, -6, -5, -4, -3):  # dormant dry spell, no mowing
            continue
        sec = 3 * 3600 if off % 2 == 0 else int(2.5 * 3600)
        history[day.isoformat()] = {"mow_sec": sec, "complete": True,
                                    "last_updated": f"{day.isoformat()}T12:00:00"}
    (tmp / "mow_history.json").write_text(json.dumps(history, indent=2))
    (tmp / "planned_sessions.json").write_text("[]")
    (tmp / "plan_log.json").write_text("[]")


# ─── Mocked mower endpoints ───────────────────────────────────────────────────
CONNECTION = {
    "connected": True, "address": "AA:BB:CC:DD:EE:FF", "auto_reconnect": True,
    "reconnect_target": "AA:BB:CC:DD:EE:FF", "saved_channel_id": 1197489078,
    "saved_pin": None, "server_started_at": TODAY.isoformat() + "T00:00:00",
    "uptime_seconds": 3 * 86400 + 5 * 3600,
}
STATUS = {
    "connected": True, "address": "AA:BB:CC:DD:EE:FF",
    "mower_name": "Sir Schnittalot", "serial_number": 123456789,
    "manufacturer": "Husqvarna", "model": "Automower 305",
    "supports_cutting_height": False,
    "state": "IN_OPERATION", "state_description": "In operation (see activity for details)",
    "activity": "MOWING", "activity_description": "Mowing lawn",
    "mode": "AUTO", "control_mode": "smart", "smart_context": None,
    "user_inhibited": False, "restriction_reason": "None",
    "battery_level": 78, "charging": False, "remaining_charging_min": None,
    "next_start_time": None, "estimated_next_start_time": None,
    "error_code": 0, "error_name": "NO_ERROR",
}
WATCHDOG = {
    "enabled": True, "running": True, "interval_minutes": 5,
    "last_check": dt.datetime.now().isoformat(),
    "last_status": "Weather OK \u2014 mower keeps mowing",
    "parked_by_watchdog": False,
    "last_weather": {"description": "Partly cloudy", "rain_mm": 0.0, "wind_ms": 3.2,
                     "checked_at": dt.datetime.now().isoformat()},
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _rect(page, selector: str) -> dict:
    return page.evaluate(
        """sel => { const r = document.querySelector(sel).getBoundingClientRect();
                    return {x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height}; }""",
        selector,
    )


def _union_shot(page, path: Path, selectors: list[str], max_h: int | None = None) -> None:
    rects = [_rect(page, s) for s in selectors]
    x = min(r["x"] for r in rects) - 12
    y = min(r["y"] for r in rects) + 1
    right = max(r["x"] + r["w"] for r in rects) + 12
    bottom = max(r["y"] + r["h"] for r in rects) + 2
    if max_h:
        bottom = min(bottom, y + max_h)
    page.screenshot(path=str(path), full_page=True,
                    clip={"x": max(x, 0), "y": max(y, 0), "width": right - x, "height": bottom - y})
    print("wrote", path.relative_to(ROOT))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="automower-demo-"))
    write_demo_files(tmp)
    planner_mod.CONFIG_PATH = tmp / "planner_config.json"
    planner_mod.MOW_HISTORY_PATH = tmp / "mow_history.json"
    planner_mod.PLANNED_SESSIONS_PATH = tmp / "planned_sessions.json"
    planner_mod.PLAN_LOG_PATH = tmp / "plan_log.json"
    planner_mod.fetch_forecast = _fake_fetch

    web_app._auth_hash = None

    async def _noop():
        return None

    web_app._load_reconnect_state = lambda: None
    web_app._reconnect_loop = _noop
    web_app._sampler_loop = _noop
    web_app._idle_disconnect_loop = _noop

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(web_app.app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
            break
        except OSError:
            time.sleep(0.1)

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.route("**/api/connection", lambda r: r.fulfill(json=CONNECTION))
        page.route("**/api/status", lambda r: r.fulfill(json=STATUS))
        page.route("**/api/watchdog/status", lambda r: r.fulfill(json=WATCHDOG))
        page.goto(f"http://127.0.0.1:{port}")
        page.wait_for_selector("#nav-conn-badge:has-text('Connected')")

        # Status tab
        page.click("a[data-section='sec-status']")
        page.wait_for_selector("#status-content:has-text('Mowing lawn')")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "status.png"))
        print("wrote docs/images/status.png")

        # Planner tab
        page.click("a[data-section='sec-planner']")
        page.wait_for_selector("#windows-body tr:not(#windows-empty)")
        page.click("button:has-text('Run Now')")
        page.wait_for_selector("#growth-content table, #growth-content .badge", timeout=20000)
        page.wait_for_selector("#planner-log-body tr td:nth-child(3)", timeout=20000)
        page.wait_for_selector("#forecast-content table", timeout=20000)
        page.wait_for_timeout(800)
        page.evaluate("document.getElementById('toast-container').innerHTML = ''")
        page.add_style_tag(content="body::before { display: none !important; }")

        sec = "#sec-planner"
        _union_shot(page, OUT / "planner-general.png", [f"{sec} > .row:nth-of-type(1)"])
        _union_shot(page, OUT / "planner-settings.png", [f"{sec} > .row:nth-of-type(2)"])
        row3 = f"{sec} > div:last-child"
        _union_shot(page, OUT / "planner-overview.png",
                    [f"{row3} > .col-md-5", f"{row3} > .col-md-7"])
        _union_shot(page, OUT / "planner-forecast.png",
                    [f"{row3} > .col-12:last-child"], max_h=620)
        browser.close()

    server.should_exit = True


if __name__ == "__main__":
    main()
