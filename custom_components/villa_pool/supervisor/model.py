"""Pure per-cycle data carriers for the pool supervisor.

Import-pure: no Home Assistant imports, no I/O, no clock reads. Building a
`PoolState` from HA lives in `engine.py`; everything in this package is a
function of the snapshot it is handed, so the whole control law is unit-testable
without a running HA.

`now` is carried ON the state rather than read from a clock, so tests pin time by
passing it (and `freeze_time` only has to drive the HA-facing layer).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time


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
    chlorine_target_control: bool = True
    volume_today_m3: float = 0.0

    def with_now(self, now: datetime) -> "PoolState":
        """A copy advanced to `now` (test convenience)."""
        return replace(self, now=now)


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
    maintenance_since: datetime | None = None


@dataclass(frozen=True)
class Decision:
    """What the supervisor intends this tick.

    In v0.1.0 nothing here is written: the engine logs each intent at INFO with
    its reason and publishes `reason` on `sensor.pool_supervisor_reason`.
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
    # False = the engine must not touch the PdC this tick at all. Set while the
    # cloud-polled climate is unavailable/unknown: "no new information" is not a
    # state change, and a write into a polling gap is how you get a double start
    # (STORY §5.2, §6, §7.9).
    pdc_write: bool = True
    # Free-form detail for the diagnostic sensors' attributes.
    detail: dict = field(default_factory=dict)
