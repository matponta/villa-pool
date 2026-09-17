"""Shared builders for the pure control-law tests.

Everything the law needs is a plain snapshot, so a test reads as "this is the
pool at this instant" rather than as HA plumbing. `at()` builds local datetimes
on the real 2026 calendar, because the ARERA band rules and the acceptance
criteria are weekday-sensitive (16/9/2026 = Wednesday, 19/9/2026 = Saturday).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta

from custom_components.villa_pool.const import (
    DEFAULT_ANTIFREEZE_OFF_C,
    DEFAULT_ANTIFREEZE_ON_C,
    DEFAULT_ANTIFREEZE_SPEED,
    DEFAULT_COVER_CHLORINE_FACTOR,
    DEFAULT_FILTRATION_SPEED,
    DEFAULT_MIN_TEMP,
    DEFAULT_PDC_SPEED,
    DEFAULT_SOLAR_OFF_W,
    DEFAULT_SOLAR_ON_W,
    DEFAULT_SOLAR_TARGET_TEMP,
    DEFAULT_TARGET_CHLORINE_HOURS,
    DEFAULT_TARGET_TURNOVERS,
    DEFAULT_WINTER_CHLORINE_HOURS,
    DEFAULT_WINTER_HOURS,
    MODE_AUTO,
)
from custom_components.villa_pool.supervisor import Memory, PoolConfig, PoolState, Windows

DEFAULT_WINDOWS = Windows(
    pump_start=time(8, 0),
    pump_end=time(20, 0),
    pdc_solar_start=time(10, 0),
    pdc_solar_end=time(18, 0),
    pdc_grid_start=time(23, 0),     # crosses midnight
    pdc_grid_end=time(7, 0),
    chlorine_start=time(9, 0),
    chlorine_end=time(21, 0),
    deadline=time(19, 0),
    winter_start=time(12, 0),
)


def config(**kw) -> PoolConfig:
    base = dict(
        min_temp=DEFAULT_MIN_TEMP,
        solar_target_temp=DEFAULT_SOLAR_TARGET_TEMP,
        solar_on_w=DEFAULT_SOLAR_ON_W,
        solar_off_w=DEFAULT_SOLAR_OFF_W,
        filtration_speed=DEFAULT_FILTRATION_SPEED,
        pdc_speed=DEFAULT_PDC_SPEED,
        antifreeze_speed=DEFAULT_ANTIFREEZE_SPEED,
        antifreeze_on_c=DEFAULT_ANTIFREEZE_ON_C,
        antifreeze_off_c=DEFAULT_ANTIFREEZE_OFF_C,
        target_turnovers=DEFAULT_TARGET_TURNOVERS,
        target_chlorine_hours=DEFAULT_TARGET_CHLORINE_HOURS,
        winter_chlorine_hours=DEFAULT_WINTER_CHLORINE_HOURS,
        cover_chlorine_factor=DEFAULT_COVER_CHLORINE_FACTOR,
        winter_hours=DEFAULT_WINTER_HOURS,
        windows=DEFAULT_WINDOWS,
    )
    windows_kw = {k: kw.pop(k) for k in list(kw) if hasattr(DEFAULT_WINDOWS, k)}
    if windows_kw:
        base["windows"] = replace(base["windows"], **windows_kw)
    base.update(kw)
    return PoolConfig(**base)


def at(y=2026, m=9, d=16, hh=17, mm=25) -> datetime:
    """A naive local datetime on the real calendar."""
    return datetime(y, m, d, hh, mm)


def state(now: datetime | None = None, **kw) -> PoolState:
    """A pool snapshot. Defaults describe a healthy, quiet summer pool."""
    cfg = kw.pop("config", None) or config(**{
        k: kw.pop(k) for k in list(kw)
        if k in PoolConfig.__dataclass_fields__ or hasattr(DEFAULT_WINDOWS, k)
    })
    base = dict(
        now=now or at(),
        config=cfg,
        mode=MODE_AUTO,
        water_temp=26.0,
        outdoor_temp=20.0,
        air_temp=20.0,
        headroom_w=0.0,
        band="F1",
        pump_running=True,
        pdc_available=True,
    )
    base.update(kw)
    return PoolState(**base)


def memory(now: datetime | None = None, **kw) -> Memory:
    """A Memory whose pump confirmation is already satisfied.

    `pool_pompa_in_marcia` has no built-in debounce (STORY §1), so the law only
    trusts it after PUMP_CONFIRM_S; most tests are about something else and want
    that already elapsed.
    """
    base = dict(pump_running_since=(now or at()) - timedelta(minutes=10))
    base.update(kw)
    return Memory(**base)
