"""The PdC 4-state machine (STORY §5.2).

States: OFF | SOLAR | GRID | BLOCKED.

Home Assistant writes only `climate.set_hvac_mode` (heat/off) and
`climate.set_temperature`; the machine's own thermostat does the rest. This
module decides which state the PdC should be in and at what setpoint — it never
touches HA.

Two rules here are load-bearing and were learned from this specific hardware:

* **`unavailable`/`unknown` is not a state change.** The PdC is cloud-polled at
  ~5 min and its entities flicker between polls. Reading a polling gap as "off"
  and "restarting" a machine that never stopped is the failure this guards
  (§5.2, §6, §7.9) — so an unavailable PdC freezes the machine AND suppresses
  every write.
* **MIN_OFF (15 min) between two starts**, and MIN_ON (30 min) before a
  solar-loss stop. Compressor protection; a 12 min cloud must not cost a cycle.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from ..const import (
    GRID_FORBIDDEN_BANDS,
    MIN_TEMP_HYSTERESIS,
    PDC_BLOCKED,
    PDC_GRID,
    PDC_MIN_OFF_S,
    PDC_MIN_ON_S,
    PDC_OFF,
    PDC_SOLAR,
    SOLAR_OFF_DWELL_S,
)
from .model import Memory, PoolState
from .solar import solar_lost_for
from .windows import in_window

RUNNING_STATES = (PDC_SOLAR, PDC_GRID)

# Which window is authorising a grid run. The night one is §3's 23:00-07:00;
# the daytime one is §5.4's top-up inside the SOLAR window, and exists because
# the air is 8-10 K warmer by day — the same thermal kWh costs 30-45 % less,
# and F1 and F3 are within a cent of each other.
GRID_NIGHT = "night"
GRID_DAY = "day"


def in_day_topup_window(state: PoolState) -> bool:
    """Is §5.4's daytime top-up authorising a grid run right now?

    Asked separately from `grid_window` because this is ALSO the band
    exemption (owner amendment 2026-09-18), and the two windows may overlap
    once the owner edits them: what the band rule turns on is whether the DAY
    window authorises, not which of the two `grid_window` chose to name.
    """
    w = state.config.windows
    return state.grid_day_topup and in_window(
        state.now, w.pdc_solar_start, w.pdc_solar_end
    )


def grid_window(state: PoolState) -> str | None:
    """Which grid window `now` falls in, or None.

    The night window is named first when both authorise: it is the one §3
    specified, and the daytime label is what carries the extra cost warning,
    so under-claiming it is the safe direction.
    """
    w = state.config.windows
    if in_window(state.now, w.pdc_grid_start, w.pdc_grid_end):
        return GRID_NIGHT
    if in_day_topup_window(state):
        return GRID_DAY
    return None


def blocking_reason(state: PoolState, *, pump_confirmed: bool,
                    pdc_blocked_modes: tuple[str, ...]) -> str | None:
    """Why the PdC may not run at all this tick, or None.

    Order matters only for the message the owner reads first; any one of these
    is sufficient. The hydraulic interlock (no confirmed flow) is listed first
    because it is the one that protects the hardware.
    """
    if state.maintenance:
        return "maintenance"
    if state.mode in pdc_blocked_modes:
        return f"mode {state.mode}"
    if state.pdc_fault:
        return "PdC fault"
    if state.pump_problem:
        return "pump problem"
    if not pump_confirmed:
        return "pump not in marcia"
    return None


def solar_conditions(state: PoolState, mem: Memory) -> bool:
    """Would SOLAR be justified right now (ignoring timers and interlocks)?

    The solar window OR the grid window qualifies: free sun is worth taking
    whenever it appears inside either heating window (§5.2).
    """
    w = state.config.windows
    if state.water_temp is None:
        return False
    in_either = in_window(state.now, w.pdc_solar_start, w.pdc_solar_end) or in_window(
        state.now, w.pdc_grid_start, w.pdc_grid_end
    )
    return (
        mem.solar_ok
        and state.water_temp < state.config.solar_target_temp
        and in_either
    )


def grid_conditions(
    state: PoolState, *, running: bool = False
) -> tuple[bool, str | None]:
    """Would GRID be justified right now? Returns (ok, refusal reason).

    `running` selects which side of the hysteresis band the water test uses,
    and IT IS LOAD-BEARING (STORY §5.2: start below `min_temp`, stop at
    `min_temp + 0.5`). Applying the start threshold to a running machine kills
    the band: the run ends the moment the water touches 27.0, drifts back below
    within MIN_OFF, and restarts — an overnight short-cycle of roughly one start
    every 25 min, which is exactly what MIN_ON/MIN_OFF exist to prevent. Caught
    by simulating a full September day before the v0.1.0 tag.

    The refusal reason is what surfaces on `sensor.pool_supervisor_reason`, so
    §7.4's "`reason` says `band F2`" is satisfied by the band branch below.
    Reason on BANDS, never on prices — the PUN index changes monthly (§3).

    **The band veto is NIGHT-ONLY since the owner amendment of 2026-09-18.**
    §5.4's daytime top-up runs whatever the band is. §3 forbade F2 because F2
    is ~17 % dearer per electrical kWh than F1/F3 — but the top-up exists
    because daytime air buys ~24 % more heat per kWh, and the two are the same
    size. Vetoing F2 by day did not make the heat cheaper, it moved the whole
    run to 23:00 where the kWh is cheap and the COP is worse; with the default
    windows it only ever bit on Saturday (F2 07:00-23:00 against a 10-18 solar
    window), i.e. it bought the pool one cold day a week. The night window
    keeps the veto: there the band is the only thing that varies.
    """
    if not state.grid_heating:
        return False, "grid heating off"
    if state.water_temp is None:
        return False, "water temp unknown"
    if grid_window(state) is None:
        return False, "outside grid window"
    if state.band in GRID_FORBIDDEN_BANDS and not in_day_topup_window(state):
        return False, f"band {state.band}"
    limit = state.config.min_temp + (MIN_TEMP_HYSTERESIS if running else 0.0)
    if state.water_temp >= limit:
        return False, "min temp reached" if running else "water at min temp"
    return True, None


def _elapsed(since: datetime | None, now: datetime, seconds: int) -> bool:
    """Has `seconds` passed since `since`? An unknown anchor counts as yes —
    a missing timer must not wedge the machine permanently."""
    if since is None:
        return True
    return (now - since) >= timedelta(seconds=seconds)


def pdc_step(
    state: PoolState,
    mem: Memory,
    *,
    pump_confirmed: bool,
    pdc_blocked_modes: tuple[str, ...],
) -> tuple[str, float | None, str, bool, Memory]:
    """Advance the machine one tick.

    Returns `(pdc_state, setpoint, reason, may_write, memory)`.
    """
    cfg = state.config
    # Timers only. Every time-of-day question below goes through `state.now`
    # inside `solar_conditions` / `grid_conditions` (see `model.PoolState`).
    now = state.mono
    current = mem.pdc_state

    # --- the cloud-polling gap: freeze everything ----------------------------
    # Not a state change, not a write, not an observation. The machine keeps
    # whatever state it had and we look again next tick.
    if not state.pdc_available:
        return (
            current,
            _setpoint_for(current, cfg),
            "PdC unavailable — holding, no write",
            False,
            mem,
        )

    # --- hard blocks win over everything ------------------------------------
    blocked = blocking_reason(
        state, pump_confirmed=pump_confirmed, pdc_blocked_modes=pdc_blocked_modes
    )
    if blocked is not None:
        mem2 = mem
        if current != PDC_BLOCKED:
            # Leaving a running state: stamp the stop so MIN_OFF applies to the
            # next start, and remember when we entered BLOCKED.
            mem2 = replace(
                mem,
                pdc_state=PDC_BLOCKED,
                pdc_since=now,
                pdc_last_stop=now if current in RUNNING_STATES else mem.pdc_last_stop,
            )
        return PDC_BLOCKED, None, f"PdC blocked: {blocked}", True, mem2

    # A block that has cleared drops to OFF and is then free to start again in
    # this same tick — MIN_OFF is what protects the compressor, not inertia.
    if current == PDC_BLOCKED:
        current = PDC_OFF
        mem = replace(mem, pdc_state=PDC_OFF, pdc_since=now)

    solar_ok_now = solar_conditions(state, mem)
    grid_ok_now, grid_refusal = grid_conditions(state)

    # --- transitions out of a running state ----------------------------------
    if current == PDC_SOLAR:
        if state.water_temp is not None and state.water_temp >= cfg.solar_target_temp:
            return _stop(mem, now, "solar target reached", cfg)
        lost = solar_lost_for(mem, now)
        if lost >= timedelta(seconds=SOLAR_OFF_DWELL_S) and _elapsed(
            mem.pdc_since, now, PDC_MIN_ON_S
        ):
            # SOLAR -> GRID rather than off, when the grid path would take over.
            if grid_ok_now:
                return _enter(mem, now, PDC_GRID, "sun gone — grid top-up", cfg)
            return _stop(mem, now, "sun gone", cfg)
        return _hold(mem, PDC_SOLAR, "heating on solar", cfg)

    if current == PDC_GRID:
        if solar_ok_now:
            return _enter(mem, now, PDC_SOLAR, "solar took over from grid", cfg)
        keep_ok, keep_refusal = grid_conditions(state, running=True)
        if not keep_ok:
            return _stop(mem, now, keep_refusal or "grid no longer allowed", cfg)
        # Named while it RUNS, not only when it starts: the entry reason scrolls
        # past in one tick, and these two cost very different amounts.
        return _hold(mem, PDC_GRID, f"heating on grid{_grid_label(state)}", cfg)

    # --- transitions out of OFF ----------------------------------------------
    min_off_done = _elapsed(mem.pdc_last_stop, now, PDC_MIN_OFF_S)
    if solar_ok_now:
        if not min_off_done:
            return _hold(mem, PDC_OFF, "solar ok but MIN_OFF not elapsed", cfg)
        return _enter(mem, now, PDC_SOLAR, "solar headroom sustained", cfg)
    if grid_ok_now:
        if not min_off_done:
            return _hold(mem, PDC_OFF, "grid due but MIN_OFF not elapsed", cfg)
        if grid_window(state) == GRID_DAY:
            return _enter(
                mem, now, PDC_GRID,
                f"water below minimum{_grid_label(state)}", cfg,
            )
        return _enter(
            mem, now, PDC_GRID, "water below minimum in grid window", cfg,
        )

    return _hold(mem, PDC_OFF, grid_refusal or "no heating demand", cfg)


# --- small helpers, kept boring so the machine above reads as a table --------

def _grid_label(state: PoolState) -> str:
    """The cost-relevant tail of a GRID reason line.

    Empty for a night run. The daytime one is named because the owner has to
    be able to tell the two apart at a glance, and since 2026-09-18 it also
    names the band when it is one §3 would have refused — same argument one
    level down: that is the dearest electrical kWh the pool buys, and the line
    is the only place it shows.
    """
    if grid_window(state) != GRID_DAY:
        return ""
    if state.band in GRID_FORBIDDEN_BANDS:
        return f" — daytime top-up (band {state.band})"
    return " — daytime top-up"


def _setpoint_for(pdc_state: str, cfg) -> float | None:
    if pdc_state == PDC_SOLAR:
        return cfg.solar_target_temp
    if pdc_state == PDC_GRID:
        return cfg.min_temp
    return None


def _enter(mem, now, new_state, reason, cfg):
    mem2 = replace(mem, pdc_state=new_state, pdc_since=now)
    return new_state, _setpoint_for(new_state, cfg), reason, True, mem2


def _hold(mem, pdc_state, reason, cfg):
    mem2 = mem if mem.pdc_state == pdc_state else replace(mem, pdc_state=pdc_state)
    return pdc_state, _setpoint_for(pdc_state, cfg), reason, True, mem2


def _stop(mem, now, reason, cfg):
    mem2 = replace(mem, pdc_state=PDC_OFF, pdc_since=now, pdc_last_stop=now)
    return PDC_OFF, None, reason, True, mem2
