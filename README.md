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
│                    ┌──────┴──────┐                      │
│                    │             │                      │
│               planner.py    automower_ble/              │
│           (Weather Agent)    mower.py                   │
│                    │         protocol.py                │
│                    │         protocol.json              │
│             Open-Meteo API       │                      │
│             (forecast +          │                      │
│              current wx)    BLE (bleak)                 │
│                                  │                      │
│                           Husqvarna Automower           │
└─────────────────────────────────────────────────────────┘
```

### Components

| File / Package | Role |
|---|---|
| `automower_ble/protocol.json` | Reverse-engineered BLE command definitions (major/minor IDs, request & response field types) |
| `automower_ble/protocol.py` | Low-level BLE client: packet encoding/decoding, CRC, connection handshake |
| `automower_ble/mower.py` | High-level mower API: `connect`, `set_schedule`, `mower_park`, `set_time`, etc. |
| `web_app.py` | FastAPI web server — REST API + HTML page serving. Manages connection state, auto-reconnect, time sync |
| `planner.py` | Weather-based planning agent (`PlannerAgent`) and real-time watchdog (`WeatherWatchdog`) |
| `templates/index.html` | Single-page Bootstrap 5 UI (7 tabs: Connect, Status, Commands, Schedule, Statistics, Messages, Planner) |
| `planner_config.json` | Persisted planner settings (location, windows, thresholds, watchdog) |
| `reconnect_config.json` | Persisted BLE address, channel ID, PIN, and auto-reconnect flag |

### Key Features

- **BLE pairing & control** — scan, connect, pair (with optional PIN), send all standard commands
- **Schedule editor** — read the current 7-day schedule, edit it visually, push it back to the mower
- **Auto-reconnect** — background loop re-connects automatically after a dropout
- **Clock sync** — mower clock synced to local time on every connect
- **Weather planner** — fetches hourly forecasts from [Open-Meteo](https://open-meteo.com) (free, no API key), computes optimal mowing slots respecting rain, wind, temperature, and rain-delay constraints, pushes the schedule to the mower
- **Weather watchdog** — polls *current* conditions every N minutes; parks the mower (HOME mode) if a thunderstorm or threshold breach is detected mid-session, resumes automatically when conditions clear
- **Runtime estimator** — samples battery level and activity every minute; derives discharge/charge rates and estimates mowing vs charging breakdown for a planned session
- **Password authentication** — optional session-cookie login page (bcrypt + signed cookie); enabled with `--password` or the `AUTH_PASSWORD` env var
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

---

## CLI / Library Usage

Scan for nearby Husqvarna devices:
```bash
python ble_scanner.py
```

Direct mower control (without the web app):
```bash
python automower_ble/mower.py --address D8:B6:73:40:07:37
# with a command:
python automower_ble/mower.py --address D8:B6:73:40:07:37 --command park
```

Available commands: `park`, `pause`, `override`, `resume`

---

## Developer Notes

### Running Tests

```bash
python -m unittest discover -s tests -v
```

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
