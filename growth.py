"""
Lawn growth model for the adaptive mowing planner.

Daily weather is turned into a growth index (0 = dormant, 1 = peak growth)
from mean temperature and a simple soil-water bucket.  Growth accumulates as
a mowing "backlog" (seconds of mowing owed) which actual mowing pays off.
"""

# Mow once the backlog reaches this share of a full peak-growth session.
TRIGGER_FRACTION = 0.75
# Backlog ceiling, as a multiple of a full session, so long gaps don't snowball.
CAP_FRACTION = 2.0

_KC = 0.8            # crop coefficient applied to reference evapotranspiration
_START_FILL = 0.6    # assumed soil fill at the start of the simulated history


def temp_factor(t_c: float) -> float:
    """Cool-season grass: dormant below 5 °C and above 35 °C, optimum 15–24 °C."""
    if t_c <= 5.0 or t_c >= 35.0:
        return 0.0
    if t_c < 15.0:
        return (t_c - 5.0) / 10.0
    if t_c <= 24.0:
        return 1.0
    return (35.0 - t_c) / 11.0


def water_factor(soil_mm: float, capacity_mm: float) -> float:
    """Growth is unrestricted while the bucket is at least half full."""
    return max(0.05, min(1.0, soil_mm / (0.5 * capacity_mm)))


def simulate_growth(daily: dict[str, dict], capacity_mm: float = 40.0) -> dict[str, dict]:
    """
    Run the soil-water bucket over *daily* (``parse_daily`` output, past days
    first) and return {date: {growth, temp_factor, water_factor, soil_mm}}.
    Days without a mean temperature are skipped.
    """
    capacity_mm = max(1.0, capacity_mm)
    soil = _START_FILL * capacity_mm
    result: dict[str, dict] = {}
    for date in sorted(daily):
        d = daily[date]
        soil += (d.get("precip_mm") or 0.0) - _KC * (d.get("et0_mm") or 0.0)
        soil = max(0.0, min(capacity_mm, soil))
        t = d.get("temp_mean_c")
        if t is None:
            continue
        tf = temp_factor(t)
        wf = water_factor(soil, capacity_mm)
        result[date] = {
            "growth": round(tf * wf, 3),
            "temp_factor": round(tf, 3),
            "water_factor": round(wf, 3),
            "soil_mm": round(soil, 1),
        }
    return result


def demand_params(session_hours: float, peak_interval_days: int) -> tuple[float, float, float]:
    """Return (backlog_sec_per_day_at_peak, trigger_sec, cap_sec)."""
    session_sec = max(0.0, session_hours) * 3600.0
    per_day = session_sec / max(1, peak_interval_days)
    return per_day, TRIGGER_FRACTION * session_sec, CAP_FRACTION * session_sec


def carried_backlog_sec(
    growth: dict[str, dict],
    mowed_sec_by_date: dict[str, float],
    today_iso: str,
    per_day_sec: float,
    cap_sec: float,
) -> float:
    """Backlog at the start of today: past growth minus past mowing, never below 0."""
    backlog = 0.0
    for date in sorted(growth):
        if date >= today_iso:
            break
        backlog += growth[date]["growth"] * per_day_sec - mowed_sec_by_date.get(date, 0.0)
        backlog = max(0.0, min(cap_sec, backlog))
    return backlog
