"""Chlorinator: enable to a daily target (STORY §5.3).

HA only *enables* the UNIKO via its Shelly relay — the UNIKO decides when the
cell actually produces. So the only honest measure of progress is
`sensor.salt_chlorinator_runtime_today`; the relay's own state is not evidence
of production and is never counted.

One hard rule outranks the owner's own `pool_in_use`: the hydraulic interlock
— no confirmed flow, no cell.

Until v0.9.0 there was a second: a cover closed for more than 24 h cut the cell
outright. It ran the pool at ZERO hours from 27/9 to 29/9 under a closed cover
and water at 27-28 °C, which is where free chlorine goes fastest. A cover cuts
UV loss, not all loss. The owner's amendment (2026-09-29) replaced it with a
ramp to a floor — `cover_target_hours` — and the floor is never zero unless
the owner sets it so.

The target is a proxy: hours, not grams. The real target is FAC 1.5-2 ppm
(STORY §9). From v0.8.0 an ORP probe can *trim* that proxy — `orp_trim_h`,
computed in `water.py` and clamped there — but it never replaces it and never
switches the cell. `orp_trim_h` defaults to 0.0 through every function here, so
with no probe, a stale reading or the switch off, this module behaves exactly
as it did in v0.6.0.
"""
from __future__ import annotations

from ..const import (
    CHLORINE_MIN_PUMP_SPEED,
    MODE_WINTER,
    PDC_SOLAR,
)
from .model import PoolState
from .windows import in_slot, in_window


def cover_day(state: PoolState) -> int | None:
    """Which day of the cover ramp today is, or None when the ramp is off.

    0 is the day the cover closed. None whenever the cover does not read a
    POSITIVE closed — open, unknown or no sensor all leave the full target.
    """
    if state.cover_closed is not True:
        return None
    return max(0, state.cover_closed_days or 0)


def cover_target_hours(state: PoolState) -> float | None:
    """The cover's chlorine target for today, before any ORP extension.

    A straight line from `target_chlorine_hours` on day 0 to
    `cover_min_chlorine_hours` on day `cover_ramp_days`, flat after it. Both
    ends are the owner's settings and the steps are computed from them, so
    moving either keeps the ramp proportional: 8 -> 2 over 3 days is 8, 6, 4,
    2; 6 -> 2 is 6, 4.7, 3.3, 2.

    A floor set ABOVE the full target is read as the full target: a cover can
    lower the demand, never raise it. A ramp of 0 days drops to the floor on
    the day the cover closes.
    """
    day = cover_day(state)
    if day is None:
        return None
    cfg = state.config
    start = cfg.target_chlorine_hours
    floor = min(max(0.0, cfg.cover_min_chlorine_hours), start)
    ramp = max(0, int(cfg.cover_ramp_days))
    fraction = 1.0 if ramp == 0 else min(day, ramp) / ramp
    return start - (start - floor) * fraction


def target_hours(state: PoolState, orp_trim_h: float = 0.0) -> float:
    """Today's effective chlorine-hours target.

    The full target with the cover open; the cover ramp's value while it is
    closed (`cover_target_hours`).

    `orp_trim_h` is the §9 chemistry correction, already clamped by
    `water.orp_trim`. It is added AFTER the ramp, not scaled by it: the ramp
    estimates how much the pool *loses* under the cover, while the trim answers
    what the water actually measured. That is also what makes the floor a
    floor rather than a ceiling — with the probe on, a shut pool that is
    losing more than the ramp assumed gets the hours back. Winter ignores it
    entirely: that is a fixed maintenance dose in a closed pool, not a target
    to chase.
    """
    cfg = state.config
    if state.mode == MODE_WINTER:
        return cfg.winter_chlorine_hours
    covered = cover_target_hours(state)
    target = cfg.target_chlorine_hours if covered is None else covered
    return max(0.0, target + orp_trim_h)


def hours_missing(state: PoolState, orp_trim_h: float = 0.0) -> float:
    """Hours still owed against today's target (never negative)."""
    return max(0.0, target_hours(state, orp_trim_h) - state.chlorine_hours_today)


def catchup_active(state: PoolState, orp_trim_h: float = 0.0) -> bool:
    """Past the deadline with hours still owed (STORY §5.3).

    Runs "until target or midnight": after midnight the daily counter has reset
    and the deadline is in the future again, so this naturally goes quiet.
    """
    if not state.chlorine_target_control:
        return False
    return (
        state.now.time() >= state.config.windows.deadline
        and hours_missing(state, orp_trim_h) > 0
    )


def chlorine_decision(
    state: PoolState,
    *,
    pump_confirmed: bool,
    pump_speed: int | None,
    pdc_state: str,
    antifreeze: bool,
    orp_trim_h: float = 0.0,
) -> tuple[bool, str]:
    """Should the chlorinator be enabled? Returns (enabled, reason)."""
    cfg = state.config
    w = cfg.windows

    # --- interlocks, in priority order (STORY §5.5) --------------------------
    if state.maintenance:
        return False, "maintenance"
    if state.mode in ("manual", "closed"):
        return False, f"mode {state.mode}"
    # Rung 2 of the ladder: a faulted pump cuts the cell as hard as no flow
    # does. The owner's independent watchdog automation does this too — the
    # integration does not lean on it (STORY §5.5, §7.5).
    if state.pump_problem:
        return False, "pump problem"
    if not pump_confirmed:
        return False, "pump not in marcia"
    if antifreeze:
        return False, "antifreeze"
    # Guardrail §6: the cell's flow-switch minimum is UNKNOWN, so the
    # chlorinator is never enabled below the calibrated 80 % until the
    # step-down test has been done.
    if pump_speed is not None and pump_speed < CHLORINE_MIN_PUMP_SPEED:
        return False, f"pump {pump_speed}% below chlorine minimum"

    # In winter the summer 09-21 window does not apply: §3 says the pump runs
    # "from 12:00 for winter_hours at 80 % with chlorine enabled", so the winter
    # slot IS the chlorine window.
    if state.mode == MODE_WINTER:
        in_chlorine_window = in_slot(state.now, w.winter_start, cfg.winter_hours)
    else:
        in_chlorine_window = in_window(state.now, w.chlorine_start, w.chlorine_end)

    # --- the escape hatch: follow the window, ignore the target -------------
    if not state.chlorine_target_control:
        if in_chlorine_window:
            return True, "chlorine window (target control off)"
        return False, "outside chlorine window (target control off)"

    missing = hours_missing(state, orp_trim_h)

    # The owner is in the water: produce, unless an interlock above said no.
    if state.pool_in_use:
        return True, "pool in use"

    if missing <= 0:
        return False, f"chlorine target met ({state.chlorine_hours_today:.1f} h)"

    if in_chlorine_window:
        return True, f"chlorine window, {missing:.1f} h to target"

    # "Free hours": the PdC is already running the pump on sun, so take the
    # production now rather than paying for the pump later (STORY §5.3).
    if pdc_state == PDC_SOLAR:
        return True, "free hours while PdC on solar"

    if catchup_active(state, orp_trim_h):
        return True, f"catch-up, {missing:.1f} h to target"

    return False, "outside chlorine window"
