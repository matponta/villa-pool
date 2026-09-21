"""The supervisor engine: one tick, one decision, and now the writes.

Every 60 s the engine:
  * builds a `PoolState` from the coordinator's reads plus the setting entities,
  * runs the pure `decide()`,
  * logs at INFO — on CHANGE, not every tick — what it intends and why,
  * publishes the reason and the decision for the diagnostic sensors,
  * and hands the decision to the `Actuator`.

**`switch.pool_dry_run` is the gate, and it is ON by default.** While it is on
the actuator compares intent against the real devices and logs the difference
without calling anything, so the 24 h dry run's claim — "`villa_pool` appears
nowhere in the pool's logbook" — stays literally true. Turning it off is the
owner's deliberate act and is logged loudly on the transition.

`ACTUATION_IMPLEMENTED` records that a write path exists at all. It was False
through v0.1.0, where the capability was *absent* rather than disabled; v0.2.0
wired the pump and the chlorinator and v0.3.0 the PdC, and which levers are
live is `actuator.PDC_ACTUATION_IMPLEMENTED` rather than anything here.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .actuator import Actuator
from .const import (
    DEFAULT_ANTIFREEZE_OFF_C,
    DEFAULT_GRID_DAY_TOPUP,
    DEFAULT_ANTIFREEZE_ON_C,
    DEFAULT_ANTIFREEZE_SPEED,
    DEFAULT_COVER_CHLORINE_FACTOR,
    DEFAULT_DEADLINE,
    DEFAULT_FILTRATION_SPEED,
    DEFAULT_MIN_TEMP,
    DEFAULT_PDC_GRID_END,
    DEFAULT_PDC_GRID_START,
    DEFAULT_PDC_SOLAR_END,
    DEFAULT_PDC_SOLAR_START,
    DEFAULT_PDC_SPEED,
    DEFAULT_PUMP_END,
    DEFAULT_PUMP_START,
    DEFAULT_CHLORINE_END,
    DEFAULT_CHLORINE_START,
    DEFAULT_SOLAR_OFF_W,
    DEFAULT_SOLAR_ON_W,
    DEFAULT_SOLAR_TARGET_TEMP,
    DEFAULT_TARGET_CHLORINE_HOURS,
    DEFAULT_ORP_MAX_EXTRA_HOURS,
    DEFAULT_ORP_TARGET_MV,
    DEFAULT_PH_CEILING,
    DEFAULT_TARGET_TURNOVERS,
    DEFAULT_WINTER_CHLORINE_HOURS,
    DEFAULT_WINTER_HOURS,
    DEFAULT_WINTER_START,
    GRID_FORBIDDEN_BANDS,
    MODE_AUTO,
    PDC_GRID,
    PDC_SOLAR,
    PDC_STATES,
)
from .supervisor import (
    Decision,
    Memory,
    PoolConfig,
    PoolState,
    Session,
    SessionResult,
    Windows,
    decide,
    grid_conditions,
    in_window,
    restore_memory,
    session_step,
)

_LOGGER = logging.getLogger(__name__)

# There is a write path in this component. False through v0.1.0.
ACTUATION_IMPLEMENTED = True


def _parse_time(value: str) -> time:
    hh, _, mm = value.partition(":")
    return time(int(hh), int(mm))


class SupervisorEngine:
    """Ticks off the coordinator, decides, and (for now) only reports."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.memory = Memory()
        self.decision: Decision | None = None
        self.last_state: PoolState | None = None
        self._last_logged: tuple | None = None
        self._restored = False
        self._unsub = None
        self._stopped = False
        self._was_live: bool | None = None
        self.actuator = Actuator(hass, entry, coordinator)
        # The §5.4 heating-session log. `last_session` is what the sensors
        # publish; it stays None until the first run COMPLETES, because half a
        # session has a start reading nobody took.
        self.session: Session | None = None
        self.last_session: SessionResult | None = None
        self._pdc_was_running = False
        # Entities that display the decision subscribe here. The coordinator's
        # own listeners fire BEFORE this engine's background tick completes, so
        # a sensor driven by the coordinator alone would always publish the
        # PREVIOUS tick's decision — a full minute stale.
        self._listeners: list = []
        # One lock serialises the scheduled tick and any awaited request_run, so
        # two passes can never interleave over the shared Memory.
        self._lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._unsub = self.coordinator.async_add_listener(self._on_update)

    def stop(self) -> None:
        self._stopped = True
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._listeners.clear()

    def add_listener(self, callback_fn) -> callable:
        """Subscribe to 'a new decision is available'. Returns an unsubscribe."""
        self._listeners.append(callback_fn)

        def _remove() -> None:
            if callback_fn in self._listeners:
                self._listeners.remove(callback_fn)

        return _remove

    def _notify(self) -> None:
        for callback_fn in list(self._listeners):
            callback_fn()

    @callback
    def _on_update(self) -> None:
        if self._stopped or self._lock.locked():
            return
        self.entry.async_create_background_task(
            self.hass, self._tick(), "villa_pool_supervisor_tick"
        )

    async def request_run(self) -> None:
        """Run a pass now (a setting changed). Never skipped, never overlapping."""
        if not self._stopped:
            await self._tick()

    # --- settings ------------------------------------------------------------

    def _entity_value(self, key: str, default):
        """Read one of OUR OWN setting entities by its unique-id suffix.

        The settings are the integration's own `number`/`time`/`select`/`switch`
        entities (STORY §4), so they restore across restarts and are editable
        from the dashboard without touching the config entry.
        """
        store = self.entry.runtime_data.settings
        value = store.get(key)
        return default if value is None else value

    def _build_config(self) -> PoolConfig:
        v = self._entity_value
        return PoolConfig(
            min_temp=float(v("min_temp", DEFAULT_MIN_TEMP)),
            solar_target_temp=float(v("solar_target_temp", DEFAULT_SOLAR_TARGET_TEMP)),
            solar_on_w=float(v("solar_on_w", DEFAULT_SOLAR_ON_W)),
            solar_off_w=float(v("solar_off_w", DEFAULT_SOLAR_OFF_W)),
            filtration_speed=int(v("filtration_speed", DEFAULT_FILTRATION_SPEED)),
            pdc_speed=int(v("pdc_speed", DEFAULT_PDC_SPEED)),
            antifreeze_speed=int(v("antifreeze_speed", DEFAULT_ANTIFREEZE_SPEED)),
            antifreeze_on_c=float(v("antifreeze_on_c", DEFAULT_ANTIFREEZE_ON_C)),
            antifreeze_off_c=float(v("antifreeze_off_c", DEFAULT_ANTIFREEZE_OFF_C)),
            target_turnovers=float(v("target_turnovers", DEFAULT_TARGET_TURNOVERS)),
            target_chlorine_hours=float(
                v("target_chlorine_hours", DEFAULT_TARGET_CHLORINE_HOURS)
            ),
            winter_chlorine_hours=float(
                v("winter_chlorine_hours", DEFAULT_WINTER_CHLORINE_HOURS)
            ),
            cover_chlorine_factor=float(
                v("cover_chlorine_factor", DEFAULT_COVER_CHLORINE_FACTOR)
            ),
            winter_hours=float(v("winter_hours", DEFAULT_WINTER_HOURS)),
            orp_target=float(v("orp_target", DEFAULT_ORP_TARGET_MV)),
            orp_max_extra_hours=float(
                v("orp_max_extra_hours", DEFAULT_ORP_MAX_EXTRA_HOURS)
            ),
            ph_ceiling=float(v("ph_ceiling", DEFAULT_PH_CEILING)),
            windows=Windows(
                pump_start=v("pump_start", _parse_time(DEFAULT_PUMP_START)),
                pump_end=v("pump_end", _parse_time(DEFAULT_PUMP_END)),
                pdc_solar_start=v(
                    "pdc_solar_start", _parse_time(DEFAULT_PDC_SOLAR_START)
                ),
                pdc_solar_end=v("pdc_solar_end", _parse_time(DEFAULT_PDC_SOLAR_END)),
                pdc_grid_start=v(
                    "pdc_grid_start", _parse_time(DEFAULT_PDC_GRID_START)
                ),
                pdc_grid_end=v("pdc_grid_end", _parse_time(DEFAULT_PDC_GRID_END)),
                chlorine_start=v(
                    "chlorine_start", _parse_time(DEFAULT_CHLORINE_START)
                ),
                chlorine_end=v("chlorine_end", _parse_time(DEFAULT_CHLORINE_END)),
                deadline=v("deadline", _parse_time(DEFAULT_DEADLINE)),
                winter_start=v("winter_start", _parse_time(DEFAULT_WINTER_START)),
            ),
        )

    def _build_state(self, now: datetime, utc_now: datetime) -> PoolState:
        data = self.coordinator.data or {}
        v = self._entity_value
        air = data.get("pdc_air_temp")
        return PoolState(
            now=now,
            utc_now=utc_now,
            config=self._build_config(),
            mode=str(v("mode", MODE_AUTO)),
            water_temp=data.get("water_temp"),
            outdoor_temp=data.get("outdoor_temp"),
            # The air the evaporator sees, falling back to the Ecowitt probe.
            air_temp=air if air is not None else data.get("outdoor_temp"),
            headroom_w=data.get("headroom_w"),
            band=data.get("band"),
            pump_running=data.get("pump_running"),
            pump_problem=bool(data.get("pump_problem")),
            pump_flow_warning=bool(data.get("pump_flow_warning")),
            pdc_running=data.get("pdc_running"),
            pdc_fault=bool(data.get("pdc_fault")),
            pdc_available=bool(data.get("pdc_available", True)),
            chlorine_running=data.get("chlorine_running"),
            chlorine_hours_today=float(data.get("chlorine_hours_today") or 0.0),
            cover_closed=data.get("cover_closed"),
            cover_closed_for_h=self._cover_closed_for_h(now),
            pool_in_use=bool(data.get("pool_in_use")),
            maintenance=bool(v("maintenance", False)),
            grid_heating=bool(v("grid_heating", True)),
            grid_day_topup=bool(v("grid_day_topup", DEFAULT_GRID_DAY_TOPUP)),
            chlorine_target_control=bool(v("chlorine_target_control", True)),
            orp_control=bool(v("orp_control", False)),
            volume_today_m3=float(data.get("volume_today_m3") or 0.0),
            water_ph=data.get("water_ph"),
            water_orp=data.get("water_orp"),
            water_ec=data.get("water_ec"),
        )

    def _cover_closed_for_h(self, now: datetime) -> float | None:
        """How long the cover has read `closed`, from the entity's own history.

        Returns None while no cover entity is configured — which is the case
        today (the sensor is installed 20-21/9, id TBD). Every cover rule is
        therefore inert until the owner supplies the entity id.
        """
        entity_id = self.coordinator.eid("cover_closed")
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state != "on":
            return None
        return (dt_util.utcnow() - state.last_changed).total_seconds() / 3600.0

    # --- the tick ------------------------------------------------------------

    async def _tick(self) -> None:
        if self._stopped:
            return
        async with self._lock:
            if self._stopped:
                return
            # Two clocks. Local-naive for the windows (every STORY §3 window
            # is wall-clock: "23:00" means 23:00 in Italy, in July and in
            # January alike), UTC for every timer. See `model.PoolState`.
            now = dt_util.now().replace(tzinfo=None)
            state = self._build_state(now, dt_util.utcnow())
            if not self._restored:
                self.memory = self._restore(state)
                # A run we ADOPTED was already going before we booted, so the
                # first tick is not a false->true edge. `session.py` refuses to
                # bracket a run whose start reading nobody took (§5.4) — and
                # this is what tells it, since `_pdc_was_running` starts False.
                self._pdc_was_running = self.memory.pdc_state in (
                    PDC_SOLAR, PDC_GRID
                )
                self._restored = True
            decision, self.memory = decide(state, self.memory)
            self.decision = decision
            self.last_state = state
            self._log_session(state, decision)
            self._log_intent(decision)
            self._notify()
            live = self._announce_mode()
            await self.actuator.async_apply(decision, state.mono, live=live)

    def _log_session(self, state: PoolState, decision: Decision) -> None:
        """Bracket each heating run and record what it cost (STORY §5.4).

        Bracketed by the SUPERVISOR's state rather than `pool_pdc_acceso`: the
        machine's own flag is cloud-polled and flickers, and a flicker would
        chop one run into several. The decision state holds through a polling
        gap, which is exactly the bracket an energy measurement wants.
        """
        data = self.coordinator.data or {}
        w = state.config.windows
        running = decision.pdc_state in (PDC_SOLAR, PDC_GRID)
        self.session, finished = session_step(
            self.session,
            now=state.mono,
            running=running,
            was_running=self._pdc_was_running,
            mode=decision.pdc_state,
            water=state.water_temp,
            energy=data.get("pdc_energy"),
            air=state.air_temp,
            cover_closed=state.cover_closed,
            in_night_window=in_window(
                state.now, w.pdc_grid_start, w.pdc_grid_end
            ),
        )
        self._pdc_was_running = running
        if finished is None:
            return
        self.last_session = finished
        _LOGGER.info(
            "SESSION %s %.0f min · water %s -> %s (%s K) · %s kWh · air %s °C "
            "· COP %s%s",
            finished.mode, finished.minutes, finished.water_start,
            finished.water_end, finished.delta_t, finished.energy_kwh,
            finished.air_mean, finished.cop or "—",
            f" · {finished.note}" if finished.note else "",
        )

    def _dry_run(self) -> bool:
        return bool(self._entity_value("dry_run", True))

    def _announce_mode(self) -> bool:
        """Say it out loud the first time, and on every change after that.

        Going live is the single most consequential thing the owner can do to
        this integration, and "when did it start writing?" must be answerable
        from the log alone rather than from the switch's current position.
        """
        live = not self._dry_run()
        if live == self._was_live:
            return live
        self._was_live = live
        self.actuator.reset()
        if live:
            _LOGGER.warning(
                "switch.pool_dry_run is OFF — the supervisor is now WRITING to "
                "the pool. Every command is logged at INFO with its reason."
            )
        else:
            _LOGGER.info(
                "switch.pool_dry_run is ON — the supervisor decides and reports "
                "but writes nothing."
            )
        return live

    def _restore(self, state: PoolState) -> Memory:
        """Re-derive the latches on the first tick after a (re)start (§7.10)."""
        data = self.coordinator.data or {}
        return restore_memory(
            mono=state.mono,
            pdc_running=data.get("pdc_running"),
            pump_running=data.get("pump_running"),
            water_temp=state.water_temp,
            min_temp=state.config.min_temp,
            # The law's own answer, not a second opinion: a machine found
            # running is adopted exactly when `grid_conditions` would keep it
            # running this tick. Asking separately is what let the restore path
            # miss §5.4's daytime window and then the 2026-09-18 band amendment.
            grid_ok=grid_conditions(state, running=True)[0],
            solar_ok=False,
            outdoor_temp=state.outdoor_temp,
            antifreeze_off_c=state.config.antifreeze_off_c,
        )

    # --- reporting -----------------------------------------------------------

    def _log_intent(self, decision: Decision) -> None:
        """Log at INFO when the intent CHANGES (STORY §8 step 1).

        Per-tick logging would be 1440 lines a day and unreadable; the owner is
        comparing intent against what the pool actually did, so the interesting
        events are the transitions. The reason is always carried with them.

        This is the *decision*, which is not the same thing as a command: the
        actuator logs `WRITE` (or `DRY-RUN would set`) separately, and the two
        differ in both directions — an intent can change with nothing to send
        because the pool is already there, and a command can be sent with no
        change of intent because the device drifted away from it.
        """
        fingerprint = (
            decision.pump_on,
            decision.pump_speed,
            decision.pdc_state,
            decision.pdc_setpoint,
            decision.chlorine_on,
        )
        if fingerprint == self._last_logged:
            return
        previous, self._last_logged = self._last_logged, fingerprint
        if previous is None:
            _LOGGER.info(
                "INTENT baseline: pump=%s@%s pdc=%s@%s chlorine=%s — %s",
                "on" if decision.pump_on else "off", decision.pump_speed,
                decision.pdc_state, decision.pdc_setpoint,
                "on" if decision.chlorine_on else "off", decision.reason,
            )
            return
        for label, before, after in (
            ("pump", (previous[0], previous[1]), (fingerprint[0], fingerprint[1])),
            ("pdc", (previous[2], previous[3]), (fingerprint[2], fingerprint[3])),
            ("chlorine", previous[4], fingerprint[4]),
        ):
            if before != after:
                _LOGGER.info(
                    "INTENT %s: %s -> %s — %s",
                    label, before, after, decision.reason,
                )


def valid_pdc_state(value: str | None) -> str | None:
    """Guard for anything reading a persisted PdC state back."""
    return value if value in PDC_STATES else None


def band_forbidden(band: str | None) -> bool:
    return band in GRID_FORBIDDEN_BANDS
