"""The pure control core for Villa Pool.

No Home Assistant imports anywhere in this package: everything is a function of
the `PoolState` it is handed, which is what makes the whole control law
unit-testable without a running HA and replayable offline against the dry-run
logs.

Re-exported here so callers write `from .supervisor import decide` and never
have to know which submodule a thing lives in.
"""
from __future__ import annotations

from .actuation import (
    BLIND,
    HOLD,
    LATCHED,
    SETTLING,
    WRITE,
    Lever,
    Plan,
    plan,
    same,
)
from .chlorine import (
    catchup_active,
    chlorine_decision,
    cover_cutoff,
    hours_missing,
    target_hours,
)
from .cop import band_price, cop_estimate, solar_share, thermal_cost
from .law import decide, restore_memory
from .model import Decision, Memory, PoolConfig, PoolState, Windows
from .pdc import (
    blocking_reason,
    grid_conditions,
    grid_window,
    pdc_step,
    solar_conditions,
)
from .pump import (
    antifreeze_step,
    confirm_step,
    is_confirmed,
    postrun_active,
    pump_plan,
)
from .session import Session, SessionResult, session_step, thermal_kwh
from .solar import raw_headroom_ok, solar_lost_for, solar_step
from .windows import in_slot, in_window, since_start, window_length

__all__ = [
    "BLIND",
    "Decision",
    "HOLD",
    "LATCHED",
    "Lever",
    "Memory",
    "Plan",
    "PoolConfig",
    "PoolState",
    "SETTLING",
    "Session",
    "SessionResult",
    "WRITE",
    "Windows",
    "antifreeze_step",
    "band_price",
    "blocking_reason",
    "catchup_active",
    "chlorine_decision",
    "confirm_step",
    "cop_estimate",
    "cover_cutoff",
    "decide",
    "grid_conditions",
    "grid_window",
    "hours_missing",
    "in_slot",
    "in_window",
    "is_confirmed",
    "pdc_step",
    "plan",
    "postrun_active",
    "pump_plan",
    "raw_headroom_ok",
    "restore_memory",
    "same",
    "session_step",
    "since_start",
    "solar_conditions",
    "solar_lost_for",
    "solar_share",
    "solar_step",
    "target_hours",
    "thermal_cost",
    "thermal_kwh",
    "window_length",
]
