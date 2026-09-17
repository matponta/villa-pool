"""`binary_sensor.pool_solar_ok` — the hysteresis + dwell verdict (STORY §4)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VillaPoolConfigEntry
from .const import SOLAR_OFF_DWELL_S, SOLAR_ON_DWELL_S
from .coordinator import VillaPoolCoordinator
from .entity import pool_device


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaPoolConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([SolarOkBinarySensor(entry.runtime_data.coordinator, entry)])


class SolarOkBinarySensor(
    CoordinatorEntity[VillaPoolCoordinator], BinarySensorEntity
):
    """Is there sustained PV headroom for the PdC?

    ON after the headroom has held above `solar_on_w` for the 10 min dwell; OFF
    the moment it falls below `solar_off_w`. The patience on the way DOWN lives
    in the PdC state machine instead ("not solar_ok for 15 min"), which is what
    lets a 12 min cloud pass without costing a compressor cycle.
    """

    _attr_has_entity_name = True
    _attr_name = "Solar ok"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator, entry: VillaPoolConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_solar_ok"
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
