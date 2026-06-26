# AutoMower-BLE

An unofficial, reverse-engineered Husqvarna Automower BLE library with a full-featured local web interface. Control your mower, manage its schedule, and let a weather-aware planning agent keep the lawn cut — all without any cloud account or internet dependency (except optional weather data).

<noscript><a href="https://liberapay.com/alistair23/donate"><img alt="Donate using Liberapay" src="https://liberapay.com/assets/widgets/donate.svg"></a></noscript>

Developed and tested against an **Automower 305**. Should work on all Automowers; reports for other models are welcome.

Details on the reverse-engineering process: https://www.alistair23.me/2024/01/06/reverse-engineering-automower-ble

---

## System Overview

```
┌─────────────────────────────────────────────────────────┐
│                  AutoMower-BLE Stack                    │
│                                                         │
│  Browser  ──HTTP──►  web_app.py  (FastAPI / uvicorn)   │
│                           │                             │
│        ┌──────────────────┼──────────────────┐          │
│        │                  │                  │          │
│   planner.py         automower_ble/    background loops │
│  PlannerAgent +       mower.py         (reconnect /     │
│  WeatherWatchdog      protocol.py       sampler /       │
│        │              protocol.json     idle-sleep)     │
│        │                  │                  │          │
│  Open-Meteo API       BLE (bleak) ◄──────────┘          │
│  (forecast +              │                             │
│   current wx)             │                             │
│                    Husqvarna Automower                  │
└─────────────────────────────────────────────────────────┘
```

### Components

| File / Package | Role |
|---|---|
| `automower_ble/protocol.json` | Reverse-engineered BLE command definitions (major/minor IDs, request & response field types) |
| `automower_ble/protocol.py` | Low-level BLE client: packet encoding/decoding, CRC, connection handshake, and resilient frame reassembly (handles fragmented, duplicated, and out-of-order notifications) |
| `automower_ble/mower.py` | High-level mower API: `connect`, `set_schedule`, `mower_park`, `set_time`, etc. |
| `automower_ble/models.py` | Mapping of `(deviceType, deviceVariant)` codes to human-readable model names |
| `automower_ble/error_codes.py` | Enumeration of mower error / message codes |
| `automower_ble/helpers.py` | Shared encoding/CRC helpers |
| `web_app.py` | FastAPI web server — REST API + HTML page serving. Manages connection state, auto-reconnect, BLE idle sleep, time sync, runtime sampling |
| `planner.py` | Weather-based planning agent (`PlannerAgent`) and real-time watchdog (`WeatherWatchdog`) |
| `templates/index.html` | Single-page Bootstrap 5 UI (7 tabs: Connect, Status, Commands, Schedule, Statistics, Messages, Planner) |
| `templates/login.html` | Password login page (shown when authentication is enabled) |
| `ble_scanner.py` | Standalone CLI scanner that lists nearby Husqvarna BLE devices |
| `mower_test_cli.py` | Interactive CLI for exercising mower commands without the web app |
| `tests/` | `pytest` suite — BLE protocol, web API, planner logic, weather-watchdog coordination, and Playwright UI tests |
| `planner_config.json` | Persisted planner settings (location, windows, thresholds, protections, watchdog) |
| `reconnect_config.json` | Persisted BLE address, channel ID, PIN, and auto-reconnect flag |
| `runtime_samples.json` | Rolling battery/activity samples used by the runtime estimator |
| `mow_history.json` | Actual recorded mowing time per day (seeds the planner's interval constraint) |
| `planned_sessions.json` | Upcoming planned mowing sessions dispatched by the planner's executor |
| `plan_log.json` | Per-day decision log from the last planning run (shown in the Planner tab) |

### Key Features

- **BLE pairing & control** — scan, connect, pair (with optional PIN), send all standard commands
- **Smart vs Classic control mode** — when the planner is enabled the app runs in **Smart** mode (commands cooperate with the planner's executor and session tracking); otherwise **Classic** mode issues raw firmware commands. See [Control Modes](#control-modes) below.
- **Schedule editor** — read the current 7-day schedule, edit it visually, push it back to the mower (locked while the planner owns the schedule)
- **Auto-reconnect** — background loop re-connects automatically after a dropout, recovering from BlueZ "zombie" links. The protocol layer reassembles fragmented BLE notifications, drops the firmware's duplicate-notification floods, and skips unsolicited frames so a reconnect mid-mow recovers cleanly
- **BLE idle sleep** — disconnects automatically when the web UI is idle and the mower is parked, then reconnects on the next request or upcoming planned session, to save energy
- **Clock sync** — mower clock synced to local time on every connect
- **Weather planner** — fetches hourly forecasts from [Open-Meteo](https://open-meteo.com) (free, no API key) and computes optimal mowing slots respecting:
  - per-day-of-week available time windows and target mowing hours
  - minimum interval between mowing days
  - per-session min/max duration
  - rain, wind speed, temperature, and post-rain delay thresholds
  - **hedgehog protection** (daylight-only mowing, sunrise→sunset)
  - **heat-stress protection** (midday no-mow window + reduced frequency on hot days)
  - **dew avoidance** (delay start until dew has evaporated, auto-estimated or fixed)
  - **cycle-aware sizing** (inflates the window so actual cutting time hits the target despite charging breaks)
- **Planner executor** — dispatches the computed sessions to the mower at the right time via direct override commands, deducting already-completed mowing from the daily target
- **City geocoding** — resolve a city name to coordinates via Open-Meteo for the planner location
- **Weather watchdog** — polls *current* conditions every N minutes; parks the mower (HOME mode) if a thunderstorm or threshold breach is detected mid-session, resumes automatically when conditions clear. An optional **drying delay** holds the resume for a configurable time after the weather clears so the lawn can dry; if that delay outlasts the interrupted session's window, the planner replans a make-up session (today if a window still allows it, otherwise the next valid day)
- **Runtime estimator** — samples battery level and activity every minute; derives discharge/charge rates and estimates mowing vs charging breakdown for a planned session
- **Mow history** — records actual mowing time per day so the planner's interval logic reflects reality even after missed or weather-cancelled sessions
- **Password authentication** — optional session-cookie login page (bcrypt + signed cookie); enabled with `--password` or the `AUTH_PASSWORD` env var
- **Rotating file logging** — optional `--log-file` with size-based rotation for multi-day diagnostics
- **Flash wear protection** — schedule is only written to the mower when it actually changes
- **Auto-load on tab switch** — each tab automatically fetches fresh data when opened (while connected)

---

## Installation

### Requirements

- Python **3.12** or newer
- Bluetooth adapter (built-in on Raspberry Pi)
- Linux (BlueZ stack); macOS works for development

### Quick Start (local machine)

```bash
git clone https://github.com/your-repo/AutoMower-BLE.git
cd AutoMower-BLE

python3.12 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install bleak bleak_retry_connector fastapi uvicorn jinja2 python-multipart \
            itsdangerous bcrypt
pip install -e .

python web_app.py --host 127.0.0.1 --port 8080
```

Open **http://127.0.0.1:8080** in your browser.

> **Optional:** protect the UI with a password:
> ```bash
> python web_app.py --host 127.0.0.1 --port 8080 --password "yourpassword"
> # or via environment variable:
> AUTH_PASSWORD=yourpassword python web_app.py
> ```
> When a password is set, an `automower_session` cookie (8 h, HttpOnly, signed) is issued on successful login.

### Raspberry Pi Zero 2W (headless server)

```bash
# 1. System packages
sudo apt update
sudo apt install -y python3.12 python3.12-venv bluetooth bluez libglib2.0-dev

# 2. Enable Bluetooth
sudo systemctl enable bluetooth
sudo systemctl start bluetooth

# 3. Deploy project
cd /home/pi
git clone https://github.com/your-repo/AutoMower-BLE.git   # or scp from your dev machine
cd AutoMower-BLE

# 4. Virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# 5. Dependencies
pip install --upgrade pip
pip install bleak bleak_retry_connector fastapi uvicorn jinja2 python-multipart \
            itsdangerous bcrypt
pip install -e .

# 6. First run (test)
python web_app.py --host 0.0.0.0 --port 8080
```

The web UI is reachable at **http://\<pi-ip\>:8080** from any device on your network.

#### Run on boot (systemd)

```bash
sudo nano /etc/systemd/system/automower.service
```

```ini
[Unit]
Description=AutoMower BLE Web Interface
After=bluetooth.target network.target

[Service]
User=pi
WorkingDirectory=/home/pi/AutoMower-BLE
ExecStart=/home/pi/AutoMower-BLE/.venv/bin/python web_app.py --host 0.0.0.0 --port 8080
# Optional: protect with a password
# Environment=AUTH_PASSWORD=yourpassword
# Optional: stable session tokens across restarts (otherwise sessions expire on restart)
# Environment=SECRET_KEY=a-long-random-string
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable automower
sudo systemctl start automower

# Check status / logs
sudo systemctl status automower
sudo journalctl -u automower -f
```

---

## Authentication

The web UI can be protected with a single password. When enabled, all routes (except `/login` and `/static/*`) require a valid session cookie.

| Option | Description |
|---|---|
| `--password <pw>` | Set via CLI argument |
| `AUTH_PASSWORD=<pw>` | Set via environment variable (useful for systemd) |
| `--secret-key <s>` | Override the signing secret for session cookies (sessions survive restarts) |
| `SECRET_KEY=<s>` | Same, via environment variable |

**Security properties:**
- Passwords hashed with **bcrypt** (no plaintext stored in memory)
- Session cookie is `HttpOnly`, `SameSite=Lax`, 8 hours max-age, signed with `itsdangerous`
- Per-IP rate-limiting: 5 failed attempts per 5 minutes
- If `--password` is **not** set, auth is disabled and a warning is logged (suitable for trusted LAN use)

---

## Control Modes

The app behaves differently depending on whether the **planner** is enabled (toggle in the Planner tab).

| | **Classic mode** (planner disabled) | **Smart mode** (planner enabled) |
|---|---|---|
| Schedule | Firmware weekly schedule, editable in the Schedule tab | Owned by the planner; the firmware holds a harmless placeholder task and the Schedule editor is locked |
| Mowing | Mower follows its own weekly calendar | The planner's executor dispatches computed sessions via direct override commands |
| Manual **Mow** | Raw `SetOverrideMow` for the chosen duration | Clears any user-park, installs a synthetic session so the executor parks at the right time, then resumes planned sessions |
| **Pause / Park&nbsp;Home** | Raw firmware command | Also sets a *user inhibit* so the executor skips upcoming sessions until you **Resume** |
| **Resume** | `ClearOverride` + AUTO + start | Clears the inhibit and restores the active session window without cancelling the executor's override |
| **Park** (skip session) | Park until next firmware start | Cancels the current session only; the next planned session runs normally |

In Smart mode the **Status** tab's **Session** field shows the live session context (e.g. "Mowing until 18:30 (45 min left)", "Charging until ~17:05 — mowing until 18:30", "Next: Fri 27 Jun at 09:00 (90 min)", or "Parked by user — next session will be skipped"). While a session is running it always reflects the *current* session — derived from the planner's tracked session, or, after a restart/reconnect, from the firmware override — rather than the next planned one.

---

## Runtime Estimator

The **Statistics** tab includes a runtime estimator that learns from observed battery samples:

- The sampler background task records battery level, activity, and charging state every 60 seconds while connected (`runtime_samples.json`, max 2,000 entries ≈ 33 h)
- From completed mowing and charging segments it derives **discharge rate** (%/h), **charge rate** (%/h), and the **return-to-station battery threshold**
- If insufficient sample data exists it falls back to lifetime averages from the mower's own statistics
- The **Estimate** button projects mowing hours, charging hours, and number of charge stops for any planned session duration
- Samples survive restarts; use the **🗑️** button to reset and start fresh

---

## Configuration Files

Both files are created automatically on first use and are reloaded at runtime without restart.

**`reconnect_config.json`** — BLE connection target:
```json
{
  "target": { "address": "D8:B6:73:40:07:37", "channel_id": 1197489078, "pin": null },
  "enabled": true
}
```

**`planner_config.json`** — Planner & watchdog settings (edit via the Planner tab in the UI).

### Runtime state files

These are written automatically while the server runs and need no manual editing:

| File | Contents |
|---|---|
| `runtime_samples.json` | Rolling battery/activity samples for the runtime estimator |
| `mow_history.json` | Actual mowing seconds per day (pruned to 90 days) |
| `planned_sessions.json` | Upcoming planned sessions awaiting dispatch |
| `plan_log.json` | Per-day decision log from the last planning run |


---

## CLI / Library Usage

Scan for nearby Husqvarna devices:
```bash
python ble_scanner.py
```

Direct mower control (without the web app):
```bash
python -m automower_ble.mower --address D8:B6:73:40:07:37
# with a command:
python -m automower_ble.mower --address D8:B6:73:40:07:37 --command park
# with a PIN (experimental):
python -m automower_ble.mower --address D8:B6:73:40:07:37 --pin 1234
```

Available commands: `park`, `pause`, `override`, `resume`

An interactive testing CLI is also provided:
```bash
python mower_test_cli.py
```

### Web server options

```bash
python web_app.py [options]
```

| Option | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Interface to bind |
| `--port` | `8080` | Port to listen on |
| `--log-level` | `info` | `debug` / `info` / `warning` / `error` |
| `--password` | _(none)_ | Web UI password (or `AUTH_PASSWORD` env var); auth disabled if omitted |
| `--secret-key` | random | Session-cookie signing secret (or `SECRET_KEY` env var); set for stable sessions across restarts |
| `--log-file` | _(none)_ | Also write logs to this rotating file |
| `--log-max-mb` | `10` | Max size (MB) before the log file rotates |
| `--log-backups` | `5` | Number of rotated log files to keep |

---

## Developer Notes

### Running Tests

The BLE protocol tests run with no extra dependencies:

```bash
python -m unittest discover -s tests -v
```

The full suite (protocol + web API + planner + watchdog) uses `pytest`. Install
the test extras and run:

```bash
pip install -e ".[test]"
pytest                      # protocol, web API, planner and watchdog tests
```

The web-API, planner and watchdog tests need no Bluetooth hardware — the BLE
`Mower` is replaced with a mock and the FastAPI app is driven in-process.

The watchdog tests (`tests/test_watchdog.py`) specifically cover how the
`WeatherWatchdog` coordinates with the planner's session executor: parking on
bad weather, schedule-aware resume (re-issuing the override for an active
session instead of clearing it), not starting unplanned mowing once a session
window has ended, respecting a user park, and the drying delay (holding the
resume until the lawn has dried, then either resuming or replanning a make-up
session if the session window has passed).

#### Browser / UI tests (optional)

The HTML layer is covered by Playwright tests that run the real page in a
headless browser. They are excluded from the default run; enable them with:

```bash
pip install -e ".[test,ui]"
playwright install chromium
pytest tests/test_ui_playwright.py
```

> **Run the Playwright tests separately.** `pytest-playwright` keeps an event
> loop running in the main thread after its first test, which breaks any
> `async` unit test that runs afterwards in the same process
> (`RuntimeError: Runner.run() cannot be called from a running event loop`).
> For this reason the UI file is excluded from the default `pytest` run via
> `addopts` in `pyproject.toml`. Use `pytest` for everything else and
> `pytest tests/test_ui_playwright.py` for the UI layer.

### Command Protocol

Commands are defined in `automower_ble/protocol.json`. Each entry specifies:
- `major` / `minor` — BLE command identifiers
- `requestType` — ordered dict of field name → type (`uint8`, `uint16`, `uint32`, `bool`, `ascii`)
- `responseType` — same structure for the response payload

Fields are serialised **in the order they appear in the JSON** (little-endian). Adding new commands only requires a JSON entry; no Python changes needed unless the command has special logic.

---

## PIN Codes (Flymo / OEM models)

Some models (Easilife Go and other Husqvarna OEM boards) require a PIN. The PIN is entered by pressing physical buttons on the mower during pairing:

| Button | Digit |
|---|---|
| On/Off | 1 |
| Go/Schedule | 2 |
| Go | 3 |
| Park | 4 |

Default PIN is typically `1234`. See your operator's manual for the button layout.

---

## Capturing Bluetooth Traffic (Android)

Useful for reverse-engineering new commands or debugging unknown responses.

1. Enable **Developer Options** on your Android device (tap *Build number* 7 times in Settings → About)
2. In Developer Options, enable **Bluetooth HCI snoop log**
3. Toggle Bluetooth off and back on
4. Use the manufacturer app to send commands to the mower
5. Retrieve the log:
   - File path: `/data/misc/bluetooth/logs/btsnoop_hci.log` (varies by manufacturer)
   - Via ADB: `adb bugreport MyFilename` → extract zip → `FS/data/log/bt/btsnoop_hci.log`
6. Open in Wireshark with the bundled `husqvarna_automower_protocol.lua` plugin:

```bash
mkdir -p ~/.config/wireshark/plugins
cp husqvarna_automower_protocol.lua ~/.config/wireshark/plugins/
```

Filter for outgoing requests: `btatt.opcode == 0x52`
