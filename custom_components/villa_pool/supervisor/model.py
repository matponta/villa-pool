"""Pure per-cycle data carriers for the pool supervisor.

Import-pure: no Home Assistant imports, no I/O, no clock reads. Building a
`PoolState` from HA lives in `engine.py`; everything in this package is a
function of the snapshot it is handed, so the whole control law is unit-testable
without a running HA.

`now` is carried ON the state rather than read from a clock, so tests pin time by
passing it (and `freeze_time` only has to drive the HA-facing layer).

**Two clocks, deliberately.** `now` is naive LOCAL time and answers only
"where are we in the day" — windows, the deadline, the winter slot. `utc_now`
is the same instant in UTC and is what every *duration* is measured against
(MIN_ON, MIN_OFF, the 60 s pump confirmation, the solar dwells, the post-run).
Mixing the two is not academic here: across the October DST transition the
local hour 02:00-03:00 happens twice, so a local-time `now - since` goes
negative for that hour once a year and a timer silently re-arms. Harmless while
the integration was dry; not harmless once it writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time

from ..const import (
    DEFAULT_ORP_MAX_EXTRA_HOURS,
    DEFAULT_ORP_TARGET_MV,
    DEFAULT_PH_CEILING,
)


@dataclass(frozen=True)
class Windows:
    """The daily windows (STORY §3: same every day -> times, not schedules).

    Any window may cross midnight; `windows.in_window` is the only place that
    wrap-around is interpreted.
    """

    pump_start: time
    pump_end: time
    pdc_solar_start: time
    pdc_solar_end: time
    pdc_grid_start: time      # crosses midnight by default (23:00 -> 07:00)
    pdc_grid_end: time
    chlorine_start: time
    chlorine_end: time
    deadline: time            # targets must be met by here; then catch-up
    winter_start: time        # winter: run `winter_hours` from here


@dataclass(frozen=True)
class PoolConfig:
    """The settings snapshot, coerced once per cycle from the entities.

    Mirrors villa_hvac's `SupervisorConfig`: the control law reads only this, so
    a missing/garbage entity value can never reach the law unclamped.
    """

    min_temp: float
    solar_target_temp: float
    solar_on_w: float
    solar_off_w: float
    filtration_speed: int
    pdc_speed: int
    antifreeze_speed: int
    antifreeze_on_c: float
    antifreeze_off_c: float
    target_turnovers: float
    target_chlorine_hours: float
    winter_chlorine_hours: float
    cover_chlorine_factor: float
    winter_hours: float
    windows: Windows
    # --- water chemistry (§5.3, §9) ------------------------------------------
    # Defaulted, so every PoolConfig built before v0.8.0 -- and every test that
    # builds one -- keeps working and keeps the v0.6.0 law unchanged.
    orp_target: float = DEFAULT_ORP_TARGET_MV
    orp_max_extra_hours: float = DEFAULT_ORP_MAX_EXTRA_HOURS
    ph_ceiling: float = DEFAULT_PH_CEILING


@dataclass(frozen=True)
class PoolState:
    """Everything the law reasons over for one tick.

    A `None` on a live read means "unknown this cycle" and is NEVER silently
    read as `False` — see `pdc_available` for the cloud-polling case that rule
    exists for (STORY §5.2).
    """

    now: datetime
    config: PoolConfig
    mode: str

    # The same instant as `now`, in UTC. ALL duration arithmetic uses this via
    # `.mono`; `now` is for time-of-day only. None means "no separate UTC
    # anchor was supplied" — `.mono` then falls back to `now`, which is what
    # the pure tests want (one frame, no DST in sight) and never what the
    # engine does.
    utc_now: datetime | None = None

    # --- temperatures ---------------------------------------------------------
    water_temp: float | None = None
    outdoor_temp: float | None = None
    air_temp: float | None = None        # PdC ambient, falls back to outdoor

    # --- energy ---------------------------------------------------------------
    headroom_w: float | None = None
    band: str | None = None              # F1 | F2 | F3

    # --- pump -----------------------------------------------------------------
    # `pump_running` is the ONE truth (binary_sensor.pool_pompa_in_marcia).
    # Never inferred from watts: P proportional to speed^3, so 427 W at 80 %
    # becomes ~22 W at 30 % and a power threshold silently lies (STORY §6).
    pump_running: bool | None = None
    pump_problem: bool = False
    pump_flow_warning: bool = False

    # --- PdC ------------------------------------------------------------------
    pdc_running: bool | None = None
    pdc_fault: bool = False
    # False while the cloud-polled climate entity is unavailable/unknown. The
    # law then makes NO state change and issues NO write (STORY §5.2 / §7.9).
    pdc_available: bool = True

    # --- chlorinator ----------------------------------------------------------
    chlorine_running: bool | None = None
    chlorine_hours_today: float = 0.0

    # --- cover (sensor not installed yet; id TBD — STORY §1) -----------------
    # None = no sensor configured or unknown. The 24 h cut-off can only fire on
    # a POSITIVE closed reading, so an absent sensor never disables chlorine.
    cover_closed: bool | None = None
    cover_closed_for_h: float | None = None

    # --- owner switches / inputs ---------------------------------------------
    pool_in_use: bool = False
    maintenance: bool = False
    grid_heating: bool = True            # §3: allowed from day one
    # §5.4, confirmed by the owner 2026-09-17: also allow GRID inside the SOLAR
    # window when the sun is not there. The air is 8-10 K warmer by day, so the
    # same thermal kWh costs 30-45 % less than at night — and F1 ~ F3 in price.
    grid_day_topup: bool = True
    chlorine_target_control: bool = True
    # §5.3 / §9: let a fresh ORP reading trim the chlorine-hours target.
    # Default False -- this ships inert, like `dry_run` did, and is turned on
    # deliberately once the probe has earned it.
    orp_control: bool = False
    volume_today_m3: float = 0.0

    # --- water chemistry (YINMIK WF-3188, in the skimmer) --------------------
    # None = not configured, or unreadable this tick. These are RAW probe
    # values and mean nothing on their own: `water.read_quality` is the only
    # place that decides whether the pump has flushed the skimmer long enough
    # for them to be the pool's chemistry rather than the pocket's.
    water_ph: float | None = None
    water_orp: float | None = None
    water_ec: float | None = None

    @property
    def mono(self) -> datetime:
        """The anchor every duration in this package is measured against.

        UTC when the caller supplied one, otherwise `now`. Never used for
        windows or any other time-of-day question — see the module docstring.
        """
        return self.utc_now if self.utc_now is not None else self.now

    def with_now(self, now: datetime) -> "PoolState":
        """A copy advanced to `now` (test convenience).

        The UTC anchor moves by the same delta, so a replayed sequence of ticks
        keeps its two clocks consistent.
        """
        utc_now = (
            self.utc_now + (now - self.now) if self.utc_now is not None else None
        )
        return replace(self, now=now, utc_now=utc_now)


@dataclass(frozen=True)
class Memory:
    """The supervisor's latches and timers, carried between ticks.

    Pure: `decide()` returns the next Memory rather than mutating this one, so a
    test can replay a sequence of ticks deterministically. Restored across a HA
    restart from the entity states (STORY §6 / §7.10) — never assumed empty.
    """

    pdc_state: str = "off"
    pdc_since: datetime | None = None          # when pdc_state was entered
    pdc_last_stop: datetime | None = None      # for MIN_OFF between two starts
    solar_ok: bool = False
    solar_raw_since: datetime | None = None    # raw headroom ok since (10 min dwell)
    solar_lost_since: datetime | None = None   # solar_ok False since (15 min dwell)
    pump_running_since: datetime | None = None  # for the 60 s confirmation
    postrun_until: datetime | None = None      # pump post-run after a PdC stop
    antifreeze_active: bool = False
    # Set while antifreeze is driving the pump in a mode that would otherwise
    # freeze the supervisor out (`manual` / `closed`). Having STARTED the pump,
    # the supervisor is responsible for stopping it: without this the release
    # simply reverts to "not driving anything" and leaves the pump running for
    # the rest of the winter.
    antifreeze_owns_pump: bool = False
    maintenance_since: datetime | None = None


@dataclass(frozen=True)
class Decision:
    """What the supervisor intends this tick.

    The engine turns this into service calls (`actuator.py`) when
    `switch.pool_dry_run` is off, and into log lines when it is on. `reason` is
    published on `sensor.pool_supervisor_reason` either way.
    """

    pump_on: bool = False
    pump_speed: int | None = None
    pdc_state: str = "off"
    pdc_setpoint: float | None = None
    chlorine_on: bool = False
    # One line, owner-facing: why each actuator is where it is (STORY §4).
    reason: str = ""
    # Which demands asked for the pump, highest-speed first (STORY §5.1).
    requesters: tuple[str, ...] = ()
    # Set when the priority ladder stopped at an interlock (STORY §5.5).
    blocked_reason: str | None = None
    # False = do not touch ANY lever this tick. Set only by rung 1 of the
    # ladder (maintenance / mode manual / mode closed), where the STORY word is
    # "freezes all actuation" — which is not the same instruction as "turn
    # everything off". `pump_on=False` in a frozen decision means "we are not
    # asking for the pump", not "switch the pump off"; an actuator that failed
    # to tell those apart would stop the pool the moment the owner flipped
    # maintenance to work on it.
    actuate: bool = True
    # False = the engine must not touch the PdC this tick at all. Set while the
    # cloud-polled climate is unavailable/unknown: "no new information" is not a
    # state change, and a write into a polling gap is how you get a double start
    # (STORY §5.2, §6, §7.9).
    pdc_write: bool = True
    # Free-form detail for the diagnostic sensors' attributes.
    detail: dict = field(default_factory=dict)
