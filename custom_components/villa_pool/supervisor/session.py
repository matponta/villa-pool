"""Heating-session logging — the pure half (STORY §5.4).

§5.4 asks for every heating session to be recorded "so the slope can be fitted
from data". That is the whole point: the COP model has exactly ONE measured
point (2.82 at ~18 °C air) and a Carnot-scaled guess for its slope, and it stays
diagnostic-only until there are real sessions to fit against.

The arithmetic is the owner's own, from the night of 16->17/9: thermal energy is
`POOL_VOLUME_M3 x KWH_PER_M3_K x delta_T_water`, and the measured COP is that
divided by the electrical kWh on phase A. 90 m3 x 1.163 x 0.5 K = 52.3 kWh_th
against ~18.6 kWh_el gives 2.82 — which is exactly the figure §1 records, so a
session logged here is directly comparable with the measurement the model was
built on.

**Only night sessions calibrate.** §5.4 is explicit: "Daytime sessions are
contaminated by solar gain on the pool; a clean daytime COP needs the return-line
probe after the bypass mix". A session is therefore marked `clean` only if it ran
wholly inside the night grid window — which, now that `grid_day_topup` exists,
is a question about the clock and not about the mode. A daytime session is still
logged, with `clean` false and its COP flagged as an upper bound: the sun did
part of the work and the compressor gets the credit.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from ..const import (
    KWH_PER_M3_K,
    POOL_VOLUME_M3,
    SESSION_MIN_DELTA_T,
    SESSION_MIN_MINUTES,
)


@dataclass(frozen=True)
class Session:
    """A heating run in progress."""

    started: datetime | None = None
    mode: str = ""
    water_start: float | None = None
    energy_start: float | None = None
    air_sum: float = 0.0
    air_n: int = 0
    cover_closed: bool | None = None
    # ANDed every tick: false the moment any part of the run falls outside the
    # night window, which is what disqualifies it from calibrating the model.
    all_night: bool = True


@dataclass(frozen=True)
class SessionResult:
    """A finished heating run, and what it says about the machine."""

    started: datetime | None = None
    ended: datetime | None = None
    mode: str = ""
    minutes: float = 0.0
    water_start: float | None = None
    water_end: float | None = None
    delta_t: float | None = None
    energy_kwh: float | None = None
    thermal_kwh: float | None = None
    cop: float | None = None
    air_mean: float | None = None
    cover_closed: bool | None = None
    clean: bool = False
    # Why there is no COP, when there is none.
    note: str = ""


def thermal_kwh(delta_t: float, volume_m3: float = POOL_VOLUME_M3) -> float:
    """Energy it takes to lift `volume_m3` of water by `delta_t` kelvin."""
    return volume_m3 * KWH_PER_M3_K * delta_t


def session_step(
    current: Session | None,
    *,
    now: datetime,
    running: bool,
    was_running: bool,
    mode: str,
    water: float | None,
    energy: float | None,
    air: float | None,
    cover_closed: bool | None,
    in_night_window: bool,
) -> tuple[Session | None, SessionResult | None]:
    """Advance the session log. Returns (session in progress, finished result).

    `running` is the SUPERVISOR's state, not `pool_pdc_acceso`: the machine's own
    flag is cloud-polled and flickers, and a flicker would chop one run into
    several. The decision state holds through a polling gap, which is exactly the
    bracket we want around an energy measurement.

    A run already in progress at startup is NOT adopted — a session is opened
    only on a false->true edge. Half a session has a start reading we never took,
    and a COP computed from it would be fiction.
    """
    if running and not was_running:
        return (
            Session(
                started=now,
                mode=mode,
                water_start=water,
                energy_start=energy,
                air_sum=air or 0.0,
                air_n=1 if air is not None else 0,
                cover_closed=cover_closed,
                all_night=in_night_window,
            ),
            None,
        )

    if running and current is not None:
        return (
            replace(
                current,
                air_sum=current.air_sum + (air or 0.0),
                air_n=current.air_n + (1 if air is not None else 0),
                all_night=current.all_night and in_night_window,
                # A run that changes mode mid-way (solar taking over from grid)
                # is one run; record where it ended up.
                mode=mode or current.mode,
            ),
            None,
        )

    if not running and current is not None:
        return None, _finish(current, now=now, water=water, energy=energy)

    return current, None


def _finish(
    s: Session, *, now: datetime, water: float | None, energy: float | None
) -> SessionResult:
    minutes = (
        (now - s.started).total_seconds() / 60.0 if s.started is not None else 0.0
    )
    delta_t = (
        round(water - s.water_start, 2)
        if water is not None and s.water_start is not None
        else None
    )
    energy_kwh = (
        round(energy - s.energy_start, 3)
        if energy is not None and s.energy_start is not None
        else None
    )
    air_mean = round(s.air_sum / s.air_n, 1) if s.air_n else None
    result = SessionResult(
        started=s.started,
        ended=now,
        mode=s.mode,
        minutes=round(minutes, 1),
        water_start=s.water_start,
        water_end=water,
        delta_t=delta_t,
        energy_kwh=energy_kwh,
        air_mean=air_mean,
        cover_closed=s.cover_closed,
        clean=s.all_night,
    )

    # Everything below refuses to invent a COP. A number that is wrong is worse
    # than no number, because this one exists to be fitted against.
    if minutes < SESSION_MIN_MINUTES:
        return replace(result, note=f"too short ({minutes:.0f} min)")
    if delta_t is None or energy_kwh is None:
        return replace(result, note="water or energy reading missing")
    if delta_t < SESSION_MIN_DELTA_T:
        # The probe reads to 0.1 °C, so a rise this small is mostly quantisation.
        return replace(result, note=f"delta T {delta_t:.1f} K below resolution")
    if energy_kwh <= 0:
        return replace(result, note="no energy recorded")

    heat = thermal_kwh(delta_t)
    return replace(
        result,
        thermal_kwh=round(heat, 1),
        cop=round(heat / energy_kwh, 2),
        note="" if s.all_night else "daytime — solar gain inflates this, upper bound",
    )
