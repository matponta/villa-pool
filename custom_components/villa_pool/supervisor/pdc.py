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


def grid_conditions(state: PoolState) -> tuple[bool, str | None]:
    """Would GRID be justified right now? Returns (ok, refusal reason).

    The refusal reason is what surfaces on `sensor.pool_supervisor_reason`, so
    §7.4's "`reason` says `band F2`" is satisfied by the band branch below.
    Reason on BANDS, never on prices — the PUN index changes monthly (§3).
    """
    w = state.config.windows
    if not state.grid_heating:
        return False, "grid heating off"
    if state.water_temp is None:
        return False, "water temp unknown"
    if not in_window(state.now, w.pdc_grid_start, w.pdc_grid_end):
        return False, "outside grid window"
    if state.band in GRID_FORBIDDEN_BANDS:
        return False, f"band {state.band}"
    if state.water_temp >= state.config.min_temp:
        return False, "water at min temp"
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
    now = state.now
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
        if state.water_temp is not None and state.water_temp >= (
            cfg.min_temp + MIN_TEMP_HYSTERESIS
        ):
            return _stop(mem, now, "min temp reached", cfg)
        if not grid_ok_now:
            return _stop(mem, now, grid_refusal or "grid no longer allowed", cfg)
        return _hold(mem, PDC_GRID, "heating on grid", cfg)

    # --- transitions out of OFF ----------------------------------------------
    min_off_done = _elapsed(mem.pdc_last_stop, now, PDC_MIN_OFF_S)
    if solar_ok_now:
        if not min_off_done:
            return _hold(mem, PDC_OFF, "solar ok but MIN_OFF not elapsed", cfg)
        return _enter(mem, now, PDC_SOLAR, "solar headroom sustained", cfg)
    if grid_ok_now:
        if not min_off_done:
            return _hold(mem, PDC_OFF, "grid due but MIN_OFF not elapsed", cfg)
        return _enter(mem, now, PDC_GRID, "water below minimum in grid window", cfg)

    return _hold(mem, PDC_OFF, grid_refusal or "no heating demand", cfg)


# --- small helpers, kept boring so the machine above reads as a table --------

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
