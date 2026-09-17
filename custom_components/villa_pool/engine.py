"""The supervisor engine: one tick, one decision, zero writes (v0.1.0).

**v0.1.0 is DRY-RUN ONLY.** There is no write path in this file — not one
gated by a flag, not one behind a branch: the code that would call
`switch.turn_on` or `climate.set_hvac_mode` does not exist yet. That is
deliberate. The owner's §8 step 1 is "no actuation, log intended writes only,
run 24 h dry, compare logs with reality", and the cheapest way to guarantee
that is for the capability to be absent rather than merely disabled.

`switch.pool_dry_run` (ON by default) is therefore a *declaration*, not a
gate: while it is on the engine says what it would do; turning it off in this
version logs a loud warning that actuation lands in v0.2.0 and still writes
nothing. `ACTUATION_IMPLEMENTED` below is the single flag a future release
flips, and the tests pin that it is False here.

What the engine does do every 60 s:
  * build a `PoolState` from the coordinator's reads plus the setting entities,
  * run the pure `decide()`,
  * log at INFO — on CHANGE, not every tick — what it would have done and why,
  * publish the reason and the decision for the diagnostic sensors.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_ANTIFREEZE_OFF_C,
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
    DEFAULT_TARGET_TURNOVERS,
    DEFAULT_WINTER_CHLORINE_HOURS,
    DEFAULT_WINTER_HOURS,
    DEFAULT_WINTER_START,
    GRID_FORBIDDEN_BANDS,
    MODE_AUTO,
    PDC_STATES,
)
from .supervisor import (
    Decision,
    Memory,
    PoolConfig,
    PoolState,
    Windows,
    decide,
    in_window,
    restore_memory,
)

_LOGGER = logging.getLogger(__name__)

# The single switch a future release flips. v0.1.0: False, and pinned by a test.
ACTUATION_IMPLEMENTED = False


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

    def _build_state(self, now: datetime) -> PoolState:
        data = self.coordinator.data or {}
        v = self._entity_value
        air = data.get("pdc_air_temp")
        return PoolState(
            now=now,
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
            chlorine_target_control=bool(v("chlorine_target_control", True)),
            volume_today_m3=float(data.get("volume_today_m3") or 0.0),
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
            now = dt_util.now().replace(tzinfo=None)
            state = self._build_state(now)
            if not self._restored:
                self.memory = self._restore(state)
                self._restored = True
            decision, self.memory = decide(state, self.memory)
            self.decision = decision
            self.last_state = state
            self._log_intent(decision)
            self._notify()
            if not self._dry_run() and not ACTUATION_IMPLEMENTED:
                _LOGGER.warning(
                    "switch.pool_dry_run is OFF, but v0.1.0 has no actuation path "
                    "at all — still writing nothing. Actuation lands in v0.2.0 "
                    "(pump + chlorine) and v0.3.0 (PdC)."
                )

    def _dry_run(self) -> bool:
        return bool(self._entity_value("dry_run", True))

    def _restore(self, state: PoolState) -> Memory:
        """Re-derive the latches on the first tick after a (re)start (§7.10)."""
        data = self.coordinator.data or {}
        w = state.config.windows
        return restore_memory(
            now=state.now,
            pdc_running=data.get("pdc_running"),
            pump_running=data.get("pump_running"),
            water_temp=state.water_temp,
            min_temp=state.config.min_temp,
            band=state.band,
            grid_heating=state.grid_heating,
            in_grid_window=in_window(state.now, w.pdc_grid_start, w.pdc_grid_end),
            solar_ok=False,
        )

    # --- reporting -----------------------------------------------------------

    def _log_intent(self, decision: Decision) -> None:
        """Log at INFO when the intent CHANGES (STORY §8 step 1).

        Per-tick logging would be 1440 lines a day and unreadable; the owner is
        comparing intent against what the pool actually did, so the interesting
        events are the transitions. The reason is always carried with them.
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
                "DRY-RUN baseline: pump=%s@%s pdc=%s@%s chlorine=%s — %s",
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
                    "DRY-RUN would set %s: %s -> %s — %s",
                    label, before, after, decision.reason,
                )


def valid_pdc_state(value: str | None) -> str | None:
    """Guard for anything reading a persisted PdC state back."""
    return value if value in PDC_STATES else None


def band_forbidden(band: str | None) -> bool:
    return band in GRID_FORBIDDEN_BANDS
