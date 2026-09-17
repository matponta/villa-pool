"""Diagnostic sensors (STORY §4).

`sensor.pool_supervisor_reason` is the one the owner reads first when something
looks wrong: one line saying why each actuator is where it is. In v0.1.0 — a
dry run — it is the primary output of the whole integration.
"""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VillaPoolConfigEntry
from .const import (
    DEFAULT_PRICE_F1,
    DEFAULT_PRICE_F2,
    DEFAULT_PRICE_F3,
    POOL_VOLUME_M3,
)
from .coordinator import VillaPoolCoordinator
from .entity import pool_device
from .supervisor import cop_estimate, hours_missing, thermal_cost

_LOGGER = logging.getLogger(__name__)

# HA caps a state string at 255 characters; the reason line is deliberately
# short but a long requester list could push it over, so it is truncated for
# the state and carried in full in an attribute.
MAX_STATE = 255


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaPoolConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities([
        SupervisorReasonSensor(coordinator, entry),
        PdcStateSensor(coordinator, entry),
        CopSensor(coordinator, entry),
        ThermalCostSensor(coordinator, entry),
        ChlorineHoursMissingSensor(coordinator, entry),
        VolumeTodaySensor(coordinator, entry),
        CoverClosedForSensor(coordinator, entry),
    ])


class PoolSensorBase(CoordinatorEntity[VillaPoolCoordinator], SensorEntity):
    """Common wiring. Reads the engine's last decision."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: VillaPoolConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = pool_device(entry)
        self._unsub_engine = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        engine = self._engine
        if engine is not None:
            self._unsub_engine = engine.add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_engine is not None:
            self._unsub_engine()
            self._unsub_engine = None
        await super().async_will_remove_from_hass()

    @property
    def _engine(self):
        return getattr(self._entry.runtime_data, "engine", None)

    @property
    def _decision(self):
        return getattr(self._engine, "decision", None)

    @property
    def _state(self):
        return getattr(self._engine, "last_state", None)


class SupervisorReasonSensor(PoolSensorBase):
    """Why each actuator is where it is — one line (STORY §4)."""

    _attr_name = "Supervisor reason"
    _attr_icon = "mdi:comment-question-outline"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "supervisor_reason")

    @property
    def native_value(self) -> str:
        decision = self._decision
        if decision is None:
            return "starting up"
        return decision.reason[:MAX_STATE]

    @property
    def extra_state_attributes(self) -> dict:
        decision = self._decision
        if decision is None:
            return {"dry_run": True}
        return {
            "reason": decision.reason,
            "dry_run": bool(self._entry.runtime_data.settings.get("dry_run", True)),
            "would_pump_on": decision.pump_on,
            "would_pump_speed": decision.pump_speed,
            "would_pdc_state": decision.pdc_state,
            "would_pdc_setpoint": decision.pdc_setpoint,
            "would_chlorine_on": decision.chlorine_on,
            "requesters": list(decision.requesters),
            "blocked_reason": decision.blocked_reason,
            "pdc_write_allowed": decision.pdc_write,
            # What the supervisor has actually SENT, as opposed to what it
            # wants. `latched` is the one an owner needs when the pool is not
            # following: it names the levers the supervisor has stopped
            # driving because something else kept moving them back.
            **self._actuation(),
            **decision.detail,
        }

    def _actuation(self) -> dict:
        actuator = getattr(self._engine, "actuator", None)
        if actuator is None:
            return {"writes": 0, "last_write": None, "latched": []}
        return actuator.diagnostics()


class PdcStateSensor(PoolSensorBase):
    """off | solar | grid | blocked, with the attributes §4 asks for."""

    _attr_name = "PdC state"
    _attr_icon = "mdi:heat-pump-outline"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["off", "solar", "grid", "blocked"]

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "pdc_state")

    @property
    def native_value(self) -> str | None:
        decision = self._decision
        return decision.pdc_state if decision else None

    @property
    def extra_state_attributes(self) -> dict:
        decision = self._decision
        engine = self._engine
        if decision is None or engine is None:
            return {}
        memory = engine.memory
        return {
            "reason": decision.detail.get("pdc_reason"),
            "since": memory.pdc_since.isoformat() if memory.pdc_since else None,
            "setpoint": decision.pdc_setpoint,
            "last_stop": (
                memory.pdc_last_stop.isoformat() if memory.pdc_last_stop else None
            ),
            "write_allowed": decision.pdc_write,
        }


class CopSensor(PoolSensorBase):
    """Estimated COP from the outdoor-air model (DIAGNOSTIC — §5.4).

    LOW confidence: one measured point (2.82 at ~18 °C) and a Carnot-scaled
    slope. Nothing in the control law reads this.
    """

    _attr_name = "COP stimato"
    _attr_icon = "mdi:gauge"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "cop_stimato")

    @property
    def native_value(self) -> float | None:
        state = self._state
        return cop_estimate(state.air_temp) if state else None

    @property
    def extra_state_attributes(self) -> dict:
        state = self._state
        data = self.coordinator.data or {}
        return {
            "air_temp": state.air_temp if state else None,
            "air_source": (
                "pdc_ambient" if data.get("pdc_air_temp") is not None else "outdoor"
            ),
            "confidence": "low — 1 measured point (2.82 @ ~18 °C), modelled slope",
            "unit_of_measurement": None,
        }


class ThermalCostSensor(PoolSensorBase):
    """EUR per thermal kWh (DIAGNOSTIC — §5.4)."""

    _attr_name = "Costo termico stimato"
    _attr_icon = "mdi:currency-eur"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "costo_termico_stimato")

    def _prices(self) -> dict[str, float]:
        settings = self._entry.runtime_data.settings
        return {
            "F1": float(settings.get("price_f1", DEFAULT_PRICE_F1)),
            "F2": float(settings.get("price_f2", DEFAULT_PRICE_F2)),
            "F3": float(settings.get("price_f3", DEFAULT_PRICE_F3)),
        }

    @property
    def native_value(self) -> float | None:
        state = self._state
        if state is None:
            return None
        data = self.coordinator.data or {}
        return thermal_cost(
            state.band, self._prices(), state.air_temp,
            headroom_w=state.headroom_w, pdc_power_w=data.get("pdc_power"),
        )

    @property
    def extra_state_attributes(self) -> dict:
        state = self._state
        return {
            "band": state.band if state else None,
            "prices": self._prices(),
            "note": "diagnostic only; not a decision input until calibrated",
        }


class ChlorineHoursMissingSensor(PoolSensorBase):
    """Hours still owed against today's chlorine target."""

    _attr_name = "Chlorine hours missing"
    _attr_icon = "mdi:test-tube-empty"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "chlorine_hours_missing")

    @property
    def native_value(self) -> float | None:
        state = self._state
        return round(hours_missing(state), 2) if state else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {"hours_today": data.get("chlorine_hours_today")}


class VolumeTodaySensor(RestoreSensor, PoolSensorBase):
    """m3 filtered today, integrated from the flow sensor; resets at midnight."""

    _attr_name = "Volume today"
    _attr_icon = "mdi:water-sync"
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "volume_today")
        self._unsub = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self.coordinator.seed_volume(float(last.native_value))
            except (TypeError, ValueError):
                pass
        self._unsub = async_track_time_change(
            self.hass, self._midnight, hour=0, minute=0, second=0
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        await super().async_will_remove_from_hass()

    @callback
    def _midnight(self, _now) -> None:
        self.coordinator.reset_volume()
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self.coordinator.volume_today_m3, 2)

    @property
    def extra_state_attributes(self) -> dict:
        turnovers = self.coordinator.volume_today_m3 / POOL_VOLUME_M3
        return {
            "turnovers": round(turnovers, 2),
            "pool_volume_m3": POOL_VOLUME_M3,
            "target_turnovers": self._entry.runtime_data.settings.get(
                "target_turnovers"
            ),
        }


class CoverClosedForSensor(PoolSensorBase):
    """Hours the cover has read closed.

    `unknown` until the cover sensor exists and is picked in the options flow
    (it is installed 20-21/9; its entity id is not guessed — STORY §1).
    """

    _attr_name = "Cover closed for"
    _attr_icon = "mdi:sun-snowflake-variant"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "cover_closed_for")

    @property
    def native_value(self) -> float | None:
        state = self._state
        if state is None or state.cover_closed_for_h is None:
            return None
        return round(state.cover_closed_for_h, 2)

    @property
    def available(self) -> bool:
        return bool(self.coordinator.eid("cover_closed"))

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "cover_entity": self.coordinator.eid("cover_closed"),
            "note": (
                "no cover sensor configured yet — cover rules are inert"
                if not self.coordinator.eid("cover_closed") else None
            ),
        }
