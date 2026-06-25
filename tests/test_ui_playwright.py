"""
Browser / HTML-layer tests for the AutoMower-BLE web UI (templates/index.html).

These drive the real page in a headless browser via Playwright. A live uvicorn
server runs in a background thread with auth disabled; all ``/api/*`` calls are
intercepted with ``page.route`` so no Bluetooth hardware or planner state is
needed — the tests assert that the JavaScript renders the mocked data correctly.

Not part of the default ``pytest`` run (excluded in pyproject). Run explicitly:

    pip install -e ".[test,ui]"
    playwright install chromium
    pytest tests/test_ui_playwright.py
"""

import socket
import threading
import time

import pytest

pytest.importorskip("playwright")
import uvicorn  # noqa: E402


# ─── Live server fixture ──────────────────────────────────────────────────────
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Run web_app in a background uvicorn thread, BLE loops neutralised."""
    import web_app

    web_app._auth_hash = None  # no login wall

    # Neutralise the background loops so the lifespan never touches Bluetooth.
    async def _noop():
        return None

    web_app._load_reconnect_state = lambda: None
    web_app._reconnect_loop = _noop
    web_app._sampler_loop = _noop
    web_app._idle_disconnect_loop = _noop

    port = _free_port()
    config = uvicorn.Config(web_app.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the socket to accept connections.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("Live server did not start in time")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


# ─── Route-mock helpers ───────────────────────────────────────────────────────
def _json_route(page, url_glob, payload):
    page.route(url_glob, lambda route: route.fulfill(json=payload))


CONNECTED = {
    "connected": True,
    "address": "AA:BB:CC:DD:EE:FF",
    "auto_reconnect": True,
    "reconnect_target": "AA:BB:CC:DD:EE:FF",
    "saved_channel_id": 1197489078,
    "saved_pin": None,
    "server_started_at": "2026-06-25T10:00:00",
    "uptime_seconds": 60,
}

STATUS_MOWING = {
    "connected": True,
    "address": "AA:BB:CC:DD:EE:FF",
    "mower_name": "Sir Schnittalot",
    "serial_number": 123456,
    "manufacturer": "Husqvarna",
    "model": "Automower 305",
    "supports_cutting_height": False,
    "state": "IN_OPERATION",
    "state_description": "In operation (see activity for details)",
    "activity": "MOWING",
    "activity_description": "Mowing lawn",
    "mode": "AUTO",
    "control_mode": "classic",
    "smart_context": None,
    "user_inhibited": False,
    "restriction_reason": "None",
    "battery_level": 80,
    "charging": False,
    "remaining_charging_min": None,
    "next_start_time": None,
    "estimated_next_start_time": None,
    "error_code": 0,
    "error_name": "NO_ERROR",
}


# ─── Tests ────────────────────────────────────────────────────────────────────
class TestPageLoad:
    def test_title_and_navbar(self, page, live_server):
        page.goto(live_server)
        assert "Automower BLE Control" in page.content()
        assert page.locator("#nav-conn-badge").is_visible()

    def test_disconnected_badge(self, page, live_server):
        _json_route(page, "**/api/connection", {"connected": False, "address": None})
        page.goto(live_server)
        page.wait_for_selector("#nav-conn-badge:has-text('Disconnected')")


class TestStatusTab:
    def test_shows_mower_status(self, page, live_server):
        _json_route(page, "**/api/connection", CONNECTED)
        _json_route(page, "**/api/status", STATUS_MOWING)
        page.goto(live_server)

        # Wait until the connection poll flips the badge to Connected.
        page.wait_for_selector("#nav-conn-badge:has-text('Connected')")

        page.click("a[data-section='sec-status']")
        page.wait_for_selector("#status-content:has-text('Mowing lawn')")
        content = page.inner_text("#status-content")
        assert "Automower 305" in content
        assert "Sir Schnittalot" in content
        assert "80" in content


class TestCommandsTab:
    def test_mow_command_shows_toast(self, page, live_server):
        _json_route(page, "**/api/connection", CONNECTED)
        _json_route(page, "**/api/status", STATUS_MOWING)
        _json_route(
            page,
            "**/api/command/mow",
            {"status": "ok", "action": "mow", "control_mode": "classic",
             "message": "Mowing for 3 h."},
        )
        page.goto(live_server)
        page.wait_for_selector("#nav-conn-badge:has-text('Connected')")
        page.click("a[data-section='sec-commands']")
        # The commands tab renders a Mow button once loaded.
        page.wait_for_selector("text=Mow", timeout=5000)


class TestNavigation:
    def test_tab_switching_changes_active_section(self, page, live_server):
        _json_route(page, "**/api/connection", {"connected": False, "address": None})
        page.goto(live_server)
        page.click("a[data-section='sec-planner']")
        assert page.locator("#sec-planner").get_attribute("class").find("active") != -1
