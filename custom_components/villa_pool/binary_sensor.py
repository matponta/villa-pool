"""The integration's own binary sensors.

`binary_sensor.pool_solar_ok` is STORY §4's hysteresis + dwell verdict.
`binary_sensor.pool_antifreeze` is the freeze latch, added in v0.4.0: in winter
it is the one thing the owner wants to be able to see at a glance, and reading
it out of an attribute on the reason sensor is not glancing.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VillaPoolConfigEntry
from .const import MODE_AUTO, SOLAR_OFF_DWELL_S, SOLAR_ON_DWELL_S
from .coordinator import VillaPoolCoordinator
from .entity import pool_device


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaPoolConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities([
        SolarOkBinarySensor(coordinator, entry),
        AntifreezeBinarySensor(coordinator, entry),
    ])


class PoolBinarySensorBase(
    CoordinatorEntity[VillaPoolCoordinator], BinarySensorEntity
):
    """Common wiring: subscribe to the engine so the state is this tick's.

    A sensor driven by the coordinator alone publishes the PREVIOUS tick's
    decision, i.e. a full minute stale.
    """

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


class SolarOkBinarySensor(PoolBinarySensorBase):
    """Is there sustained PV headroom for the PdC?

    ON after the headroom has held above `solar_on_w` for the 10 min dwell; OFF
    the moment it falls below `solar_off_w`. The patience on the way DOWN lives
    in the PdC state machine instead ("not solar_ok for 15 min"), which is what
    lets a 12 min cloud pass without costing a compressor cycle.
    """

    _attr_name = "Solar ok"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator, entry: VillaPoolConfigEntry) -> None:
        super().__init__(coordinator, entry, "solar_ok")

    @property
    def is_on(self) -> bool | None:
        engine = self._engine
        return engine.memory.solar_ok if engine else None

    @property
    def extra_state_attributes(self) -> dict:
        engine = self._engine
        data = self.coordinator.data or {}
        settings = self._entry.runtime_data.settings
        memory = engine.memory if engine else None
        return {
            "headroom_w": data.get("headroom_w"),
            "on_threshold_w": settings.get("solar_on_w"),
            "off_threshold_w": settings.get("solar_off_w"),
            "on_dwell_minutes": SOLAR_ON_DWELL_S // 60,
            "off_dwell_minutes_pdc": SOLAR_OFF_DWELL_S // 60,
            "raw_ok_since": (
                memory.solar_raw_since.isoformat()
                if memory and memory.solar_raw_since else None
            ),
        }


class AntifreezeBinarySensor(PoolBinarySensorBase):
    """Is freeze protection engaged? (STORY §3, wired live in v0.4.0.)

    Engages below `antifreeze_on_c` (0 °C) and releases at `antifreeze_off_c`
    (+2 °C) — a latch, not a threshold, so the pump does not chatter around
    zero. An unknown outdoor temperature HOLDS the latch rather than releasing
    it: when the question is whether the pipes are freezing, the safe direction
    is to keep the water moving.

    From v0.4.0 this outranks `manual` and `closed` (owner amendment
    2026-09-17), which is why `overrides_mode` is worth showing: it says the
    supervisor is driving the pump in a mode that otherwise freezes it.
    `maintenance` still wins over antifreeze.
    """

    _attr_name = "Antifreeze"
    _attr_icon = "mdi:snowflake-alert"
    _attr_device_class = BinarySensorDeviceClass.COLD

    def __init__(self, coordinator, entry: VillaPoolConfigEntry) -> None:
        super().__init__(coordinator, entry, "antifreeze")

    @property
    def is_on(self) -> bool | None:
        engine = self._engine
        return engine.memory.antifreeze_active if engine else None

    @property
    def extra_state_attributes(self) -> dict:
        engine = self._engine
        decision = getattr(engine, "decision", None)
        settings = self._entry.runtime_data.settings
        data = self.coordinator.data or {}
        detail = decision.detail if decision else {}
        return {
            "outdoor_temp": data.get("outdoor_temp"),
            "on_below_c": settings.get("antifreeze_on_c"),
            "release_at_c": settings.get("antifreeze_off_c"),
            "speed": settings.get("antifreeze_speed"),
            "overrides_mode": detail.get("antifreeze_override"),
            "frozen_by_maintenance": bool(
                detail.get("frozen") and not detail.get("antifreeze_override")
                and settings.get("maintenance")
            ),
            "mode": settings.get("mode", MODE_AUTO),
        }
