"""Chlorinator: enable to a daily target (STORY §5.3).

HA only *enables* the UNIKO via its Shelly relay — the UNIKO decides when the
cell actually produces. So the only honest measure of progress is
`sensor.salt_chlorinator_runtime_today`; the relay's own state is not evidence
of production and is never counted.

Two hard rules that outrank the owner's own `pool_in_use`:

* the hydraulic interlock — no confirmed flow, no cell; and
* the 24 h cover rule — a pool that has been shut for a day does not need more
  chlorine, and making it anyway is how you end up over-chlorinated under a
  closed cover.

The target is a proxy: hours, not grams. The real target is FAC 1.5-2 ppm, and
when the ORP probe / Modbus bridge to the UNIKO lands this switches to estimated
grams (STORY §9).
"""
from __future__ import annotations

from ..const import (
    CHLORINE_MIN_PUMP_SPEED,
    COVER_CLOSED_CHLORINE_CUTOFF_H,
    MODE_WINTER,
    PDC_SOLAR,
)
from .model import PoolState
from .windows import in_slot, in_window


def cover_cutoff(state: PoolState) -> bool:
    """Has the cover been closed longer than the 24 h cut-off?

    Only a POSITIVE closed reading can trigger this. The sensor is not
    installed yet (id TBD, STORY §1) and an absent or unknown one must never
    silently stop chlorination.
    """
    if not state.cover_closed:
        return False
    hours = state.cover_closed_for_h
    return hours is not None and hours > COVER_CLOSED_CHLORINE_CUTOFF_H


def target_hours(state: PoolState) -> float:
    """Today's effective chlorine-hours target.

    Halved (by `cover_chlorine_factor`) while the cover is closed: less UV
    burn-off under the cover means less production is needed.
    """
    cfg = state.config
    if state.mode == MODE_WINTER:
        return cfg.winter_chlorine_hours
    target = cfg.target_chlorine_hours
    if state.cover_closed:
        target *= cfg.cover_chlorine_factor
    return target


def hours_missing(state: PoolState) -> float:
    """Hours still owed against today's target (never negative)."""
    return max(0.0, target_hours(state) - state.chlorine_hours_today)


def catchup_active(state: PoolState) -> bool:
    """Past the deadline with hours still owed (STORY §5.3).

    Runs "until target or midnight": after midnight the daily counter has reset
    and the deadline is in the future again, so this naturally goes quiet.
    """
    if not state.chlorine_target_control:
        return False
    return state.now.time() >= state.config.windows.deadline and hours_missing(state) > 0


def chlorine_decision(
    state: PoolState,
    *,
    pump_confirmed: bool,
    pump_speed: int | None,
    pdc_state: str,
    antifreeze: bool,
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
    if cover_cutoff(state):
        return False, "cover closed > 24 h"
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

    missing = hours_missing(state)

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

    if catchup_active(state):
        return True, f"catch-up, {missing:.1f} h to target"

    return False, "outside chlorine window"
