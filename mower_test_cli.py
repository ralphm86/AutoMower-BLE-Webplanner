#!/usr/bin/env python3
"""
Interactive mower test CLI.

Usage:
    python mower_test_cli.py --address AA:BB:CC:DD:EE:FF [--pin 1234] [--channel 1197489078]

After connecting, type 'help' for a list of available commands.
"""

import argparse
import asyncio
import calendar
import datetime as dt
import logging
import sys

from bleak import BleakScanner

from automower_ble.error_codes import ErrorCodes
from automower_ble.mower import Mower
from automower_ble.protocol import ModeOfOperation, MowerActivity, MowerState, OverrideAction, TaskInformation

logging.basicConfig(
    level=logging.WARNING,         # keep bleak noise down; raise to INFO/DEBUG if needed
    format="%(asctime)s  %(name)-20s  %(levelname)s  %(message)s",
)
logger = logging.getLogger("mower_test_cli")

# ─── pretty helpers ───────────────────────────────────────────────────────────

def _hms(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _dur(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m:02d}m"
    if h:
        return f"{h}h"
    return f"{m}m"


DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

RESTRICTION_REASONS = {
    0: "None",
    1: "Week schedule",
    2: "Park override",
    3: "Sensor",
    4: "Daily limit",
}

# ─── command implementations ──────────────────────────────────────────────────

async def cmd_status(mower: Mower, _args):
    """Print full mower status."""
    state      = await mower.mower_state()
    activity   = await mower.mower_activity()
    battery    = await mower.battery_level()
    charging   = await mower.is_charging()
    next_start = await mower.mower_next_start_time()
    mode       = await mower.mower_mode()
    restr_raw  = await mower.command("GetRestrictionReason")
    error_code = await mower.command("GetError")
    rem_charge = await mower.command("GetRemainingChargingTime") if charging else None

    mode_name = mode.name if mode is not None else "?"

    restr_str = RESTRICTION_REASONS.get(restr_raw, f"Code {restr_raw}") if restr_raw is not None else "?"

    try:
        error_name = ErrorCodes(error_code).name if error_code else "NO_ERROR"
    except ValueError:
        error_name = f"UNKNOWN({error_code})"

    charge_str = f"  Remaining charge: {_dur(rem_charge)}" if rem_charge else ""

    print(f"""
  State      : {state.name if state else '?'}
  Activity   : {activity.name if activity else '?'}
  Mode       : {mode_name}{'  ⚠ NOT AUTO — schedule will not run' if mode_name not in ('AUTO', '?') else ''}
  Restriction: {restr_str}
  Battery    : {battery}%  {'(charging)' if charging else ''}{charge_str}
  Next start : {next_start.strftime('%Y-%m-%d %H:%M:%S') if next_start else '—'}
  Error      : {error_name}{f'  (code {error_code})' if error_code else ''}
""")


async def cmd_mode(mower: Mower, _args):
    """Print current mode and restriction reason."""
    mode      = await mower.mower_mode()
    restr_raw = await mower.command("GetRestrictionReason")
    mode_name = mode.name if mode is not None else "?"
    restr_str = RESTRICTION_REASONS.get(restr_raw, f"Code {restr_raw}") if restr_raw is not None else "?"
    print(f"  Mode: {mode_name}   Restriction: {restr_str}")


async def cmd_set_mode(mower: Mower, args):
    """set_mode <auto|home|manual|demo>"""
    mapping = {
        "auto": ModeOfOperation.AUTO,
        "home": ModeOfOperation.HOME,
        "manual": ModeOfOperation.MANUAL,
        "demo": ModeOfOperation.DEMO,
    }
    if not args or args[0].lower() not in mapping:
        print(f"  Usage: set_mode <{'|'.join(mapping)}>")
        return
    m = mapping[args[0].lower()]
    await mower.command("SetMode", mode=m)
    print(f"  Mode set to {m.name}")


async def cmd_mow(mower: Mower, args):
    """mow [hours=3.0]  — force mow for N hours."""
    hours = float(args[0]) if args else 3.0
    await mower.mower_override(hours)
    print(f"  Mow override sent: {hours}h")


async def cmd_pause(mower: Mower, _args):
    """Pause the mower."""
    await mower.mower_pause()
    print("  Pause sent")


async def cmd_resume(mower: Mower, _args):
    """Resume (StartTrigger)."""
    await mower.mower_resume()
    print("  Resume (StartTrigger) sent")


async def cmd_park(mower: Mower, _args):
    """Park until next scheduled start."""
    await mower.mower_park()
    print("  Park until next start sent")


async def cmd_park_home(mower: Mower, _args):
    """Set mode HOME (parks indefinitely, ignores schedule)."""
    await mower.mower_park_home()
    print("  HOME mode set")


async def cmd_park_duration(mower: Mower, args):
    """park_duration <hours>  — park for N hours then resume."""
    if not args:
        print("  Usage: park_duration <hours>")
        return
    hours = float(args[0])
    await mower.mower_park_duration(hours)
    print(f"  Park for {hours}h sent")


async def cmd_clear_override(mower: Mower, _args):
    """Send ClearOverride."""
    await mower.command("ClearOverride")
    print("  ClearOverride sent")


async def cmd_override_info(mower: Mower, _args):
    """Show current override status."""
    ov = await mower.command("GetOverride")
    if ov is None:
        print("  Override: (no response)")
        return
    try:
        action_name = OverrideAction(ov.get("action", 0)).name
    except ValueError:
        action_name = f"UNKNOWN({ov.get('action')})"
    remaining = ov.get("duration")
    print(f"  Override action : {action_name}")
    print(f"  Remaining       : {_dur(remaining) if remaining else '—'}")


async def cmd_schedule_show(mower: Mower, _args):
    """Print all tasks currently stored in the mower."""
    num = await mower.command("GetNumberOfTasks")
    if num is None:
        print("  Could not read task count")
        return
    print(f"  {num} task(s):")
    for i in range(num):
        task = await mower.get_task(i)
        if task is None:
            print(f"  [{i}] (no data)")
            continue
        days = "".join(
            n for n, f in zip(DAY_NAMES, [
                task.on_monday, task.on_tuesday, task.on_wednesday,
                task.on_thursday, task.on_friday, task.on_saturday, task.on_sunday
            ]) if f
        )
        print(
            f"  [{i}]  start={_hms(task.next_start_time)}  "
            f"dur={_dur(task.duration_in_seconds)}  days={days or '(none)'}"
        )


async def cmd_schedule_set(mower: Mower, args):
    """
    schedule_set <start_HH:MM> <dur_minutes> <days>
    days: comma-separated list of 0-6 (0=Mon) or day abbreviations (Mon,Tue,...)
    Example:  schedule_set 08:00 90 Mon,Wed,Fri
    """
    if len(args) < 3:
        print("  Usage: schedule_set <HH:MM> <duration_minutes> <days>")
        print("  days: comma-separated 0-6 or Mon,Tue,Wed,Thu,Fri,Sat,Sun")
        return

    try:
        h, m = map(int, args[0].split(":"))
        start_sec = h * 3600 + m * 60
    except ValueError:
        print("  Bad time format — use HH:MM")
        return

    dur_sec = int(args[1]) * 60
    day_flags = _parse_days(args[2])
    if day_flags is None:
        return

    task = TaskInformation(
        next_start_time=start_sec,
        duration_in_seconds=dur_sec,
        on_monday=day_flags[0], on_tuesday=day_flags[1], on_wednesday=day_flags[2],
        on_thursday=day_flags[3], on_friday=day_flags[4],
        on_saturday=day_flags[5], on_sunday=day_flags[6],
    )
    await mower.set_schedule([task])
    print(f"  Schedule pushed: {_hms(start_sec)} for {_dur(dur_sec)} on {args[2]}")


async def cmd_schedule_now(mower: Mower, args):
    """
    schedule_now [offset_minutes=5] [dur_minutes=30]
    Creates a single task scheduled to start <offset_minutes> from now on today's
    day of week. Useful to test if the mower picks up a fresh schedule.
    """
    offset_min = int(args[0]) if len(args) > 0 else 5
    dur_min    = int(args[1]) if len(args) > 1 else 30

    now = dt.datetime.now()
    start_sec = (now.hour * 3600 + now.minute * 60 + now.second) + offset_min * 60
    if start_sec >= 86400:
        start_sec = 86400 - 60  # clamp to end of day
    dow = now.weekday()  # 0=Mon … 6=Sun
    day_flags = [i == dow for i in range(7)]

    task = TaskInformation(
        next_start_time=start_sec,
        duration_in_seconds=dur_min * 60,
        on_monday=day_flags[0], on_tuesday=day_flags[1], on_wednesday=day_flags[2],
        on_thursday=day_flags[3], on_friday=day_flags[4],
        on_saturday=day_flags[5], on_sunday=day_flags[6],
    )
    fire_at = now + dt.timedelta(minutes=offset_min)
    print(f"  Pushing schedule: start={_hms(start_sec)} ({fire_at.strftime('%H:%M:%S')})  "
          f"dur={_dur(dur_min * 60)}  day={DAY_NAMES[dow]}")
    await mower.set_schedule([task])
    print(f"  Done — mower should start mowing at {fire_at.strftime('%H:%M:%S')}")
    print(f"  Watch: state should change from IN_OPERATION/PARKED_IN_CS → GOING_OUT → MOWING")


async def cmd_schedule_clear(mower: Mower, _args):
    """Delete all tasks from the mower schedule."""
    await mower.set_schedule([])
    print("  All tasks deleted")


async def cmd_sync_time(mower: Mower, _args):
    """Synchronise the mower clock to current local time."""
    await mower.set_time()
    print(f"  Clock synced to {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    mower_ts = await mower.command("GetTime")
    if mower_ts:
        mower_now = dt.datetime.utcfromtimestamp(mower_ts)
        print(f"  Mower reports time: {mower_now.strftime('%Y-%m-%d %H:%M:%S')}")


async def cmd_messages(mower: Mower, args):
    """messages [count=5]  — show recent mower log messages."""
    count = int(args[0]) if args else 5
    num = await mower.command("GetNumberOfMessages")
    if num is None:
        print("  Could not read message count")
        return
    print(f"  {min(count, num)} of {num} message(s):")
    for i in range(min(count, num)):
        msg = await mower.command("GetMessage", messageId=i)
        if not msg:
            continue
        ts = dt.datetime.utcfromtimestamp(msg["time"]).strftime("%Y-%m-%d %H:%M:%S")
        try:
            code_name = ErrorCodes(msg["code"]).name
        except ValueError:
            code_name = f"UNKNOWN({msg['code']})"
        print(f"  [{i}]  {ts}  {code_name}  sev={msg['severity']}")


async def cmd_statistics(mower: Mower, _args):
    """Print lifetime statistics."""
    stats = await mower.command("GetAllStatistics")
    if not stats:
        print("  No statistics available")
        return
    for k, v in stats.items():
        unit = " s" if "Time" in k else ""
        display = f"{v / 3600:.1f} h" if "Time" in k else str(v)
        print(f"  {k:<35} {display}")


async def cmd_next_start(mower: Mower, _args):
    """Show the next scheduled start time as reported by the mower."""
    ns = await mower.mower_next_start_time()
    print(f"  Next start: {ns.strftime('%Y-%m-%d %H:%M:%S') if ns else '—'}")


async def cmd_sequence_debug(mower: Mower, _args):
    """
    Debug sequence for the schedule-then-park-then-stuck bug.
    Prints state+activity+mode+restriction+override+next_start all at once.
    """
    results = await asyncio.gather(
        mower.mower_state(),
        mower.mower_activity(),
        mower.mower_mode(),
        mower.command("GetRestrictionReason"),
        mower.command("GetOverride"),
        mower.mower_next_start_time(),
        mower.battery_level(),
        mower.is_charging(),
        mower.command("GetRemainingChargingTime"),
        return_exceptions=True,
    )
    state, activity, mode, restr_raw, override, next_start, battery, charging, rem_charge = results

    mode_name = mode.name if isinstance(mode, ModeOfOperation) else f"ERR({mode})"

    restr_str = RESTRICTION_REASONS.get(restr_raw, f"Code {restr_raw}") if isinstance(restr_raw, int) else f"ERR({restr_raw})"

    ov_str = "none"
    if isinstance(override, dict):
        try:
            act_name = OverrideAction(override.get("action", 0)).name
        except ValueError:
            act_name = f"UNKNOWN({override.get('action')})"
        ov_str = f"{act_name}  remaining={_dur(override['duration']) if override.get('duration') else '?'}"

    print()
    print(f"  State      : {state.name if hasattr(state, 'name') else state}")
    print(f"  Activity   : {activity.name if hasattr(activity, 'name') else activity}")
    print(f"  Mode       : {mode_name}")
    print(f"  Restriction: {restr_str}")
    print(f"  Override   : {ov_str}")
    print(f"  Next start : {next_start.strftime('%Y-%m-%d %H:%M:%S') if next_start else '—'}")
    charging_ok = isinstance(charging, bool) and charging
    rem_str = (f"  {_dur(rem_charge)} remaining"
               if isinstance(rem_charge, int) and rem_charge and charging_ok else "")
    print(f"  Battery    : {battery}%{'  (charging)' + rem_str if charging_ok else ''}")
    print()

    # Diagnose common stuck states
    if hasattr(state, 'name') and state.name == "IN_OPERATION":
        if hasattr(activity, 'name') and "PARK" in activity.name:
            print("  ⚠  IN_OPERATION + PARKED: mower is waiting to start.")
            if mode_name != "AUTO":
                print(f"  ✗  Mode is {mode_name} — schedule WILL NOT run. Fix: set_mode auto")
            if restr_str != "None":
                print(f"  ✗  Restriction active: {restr_str} — try: clear_override")
            if ov_str != "none" and "FORCEDPARK" in ov_str:
                print("  ✗  Active FORCEDPARK override — try: clear_override")
            if mode_name == "AUTO" and restr_str == "None" and "FORCED" not in ov_str:
                print("  ✓  Mode=AUTO, no restriction, no forced-park override.")
                print("     If next_start time is in the past → schedule was pushed too late.")
                print("     Fix: use 'schedule_now 5' to create a fresh near-future task.")


# ─── helpers ──────────────────────────────────────────────────────────────────

def _parse_days(days_str: str) -> list[bool] | None:
    """Parse '0,2,4' or 'Mon,Wed,Fri' into a list of 7 booleans."""
    abbrev = {n.lower(): i for i, n in enumerate(["mon","tue","wed","thu","fri","sat","sun"])}
    flags = [False] * 7
    for token in days_str.replace(" ", "").split(","):
        token = token.lower()
        if token.isdigit():
            idx = int(token)
        elif token in abbrev:
            idx = abbrev[token]
        else:
            print(f"  Unknown day: '{token}' — use 0-6 or Mon/Tue/Wed/Thu/Fri/Sat/Sun")
            return None
        if 0 <= idx <= 6:
            flags[idx] = True
    return flags


COMMANDS = {
    "status":         (cmd_status,         "Full status snapshot"),
    "mode":           (cmd_mode,           "Show current mode and restriction reason"),
    "set_mode":       (cmd_set_mode,       "set_mode <auto|home|manual|demo>"),
    "mow":            (cmd_mow,            "mow [hours=3.0]  — force mow override"),
    "pause":          (cmd_pause,          "Pause the mower"),
    "resume":         (cmd_resume,         "Resume (StartTrigger)"),
    "park":           (cmd_park,           "Park until next scheduled start"),
    "park_home":      (cmd_park_home,      "Set mode HOME (indefinite park, ignores schedule)"),
    "park_duration":  (cmd_park_duration,  "park_duration <hours>"),
    "clear_override": (cmd_clear_override, "Send ClearOverride"),
    "override":       (cmd_override_info,  "Show current override status"),
    "schedule":       (cmd_schedule_show,  "Show tasks stored in mower"),
    "schedule_set":   (cmd_schedule_set,   "schedule_set <HH:MM> <dur_min> <days>"),
    "schedule_now":   (cmd_schedule_now,   "schedule_now [offset_min=5] [dur_min=30]  — push task starting soon"),
    "schedule_clear": (cmd_schedule_clear, "Delete all tasks from mower"),
    "sync_time":      (cmd_sync_time,      "Sync mower clock to local time"),
    "next_start":     (cmd_next_start,     "Show next scheduled start time from mower"),
    "messages":       (cmd_messages,       "messages [count=5]  — recent log messages"),
    "statistics":     (cmd_statistics,     "Lifetime statistics"),
    "debug":          (cmd_sequence_debug, "Full diagnostic snapshot + stuck-state analysis"),
}


def print_help():
    print()
    print("  Available commands:")
    for name, (_, desc) in COMMANDS.items():
        print(f"    {name:<18}  {desc}")
    print("    help              This help")
    print("    quit / exit / q   Disconnect and exit")
    print()


# ─── REPL ─────────────────────────────────────────────────────────────────────

async def repl(mower: Mower):
    print_help()
    loop = asyncio.get_running_loop()

    while True:
        # Read input without blocking the event loop
        try:
            line = await loop.run_in_executor(None, lambda: input("mower> ").strip())
        except (EOFError, KeyboardInterrupt):
            print("\n  Interrupted — disconnecting…")
            break

        if not line:
            continue

        parts = line.split()
        cmd_name = parts[0].lower()
        cmd_args = parts[1:]

        if cmd_name in ("quit", "exit", "q"):
            break
        if cmd_name in ("help", "?"):
            print_help()
            continue
        if cmd_name not in COMMANDS:
            print(f"  Unknown command '{cmd_name}' — type 'help' for a list.")
            continue

        fn, _ = COMMANDS[cmd_name]
        try:
            await fn(mower, cmd_args)
        except Exception as exc:
            print(f"  ERROR: {exc}")


# ─── entry point ──────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Interactive Automower BLE test CLI")
    parser.add_argument("--address", required=True, metavar="AA:BB:CC:DD:EE:FF",
                        help="BLE address of the mower")
    parser.add_argument("--pin", type=int, default=None,
                        help="Operator PIN (if required)")
    parser.add_argument("--channel", type=int, default=1197489078,
                        help="Channel ID (default: 1197489078)")
    parser.add_argument("--log-level", default="warning",
                        choices=["debug", "info", "warning", "error"],
                        help="Log level for bleak/protocol output (default: warning)")
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level.upper()))

    print(f"Scanning for {args.address} …")
    device = await BleakScanner.find_device_by_address(args.address, timeout=10.0)
    if device is None:
        print(f"Device not found: {args.address}")
        sys.exit(1)

    mower = Mower(args.channel, args.address, args.pin)
    from automower_ble.protocol import ResponseResult
    result = await mower.connect(device)
    if result != ResponseResult.OK:
        print(f"Connection failed: {result.name}")
        sys.exit(1)

    print(f"Connected to {args.address}")
    try:
        await mower.set_time()
        print(f"Clock synced to {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        print(f"Clock sync failed (continuing): {e}")

    try:
        await repl(mower)
    finally:
        print("Disconnecting…")
        await mower.disconnect()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
