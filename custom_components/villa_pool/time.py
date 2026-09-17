"""The daily windows as `time.*` entities (STORY §3/§4).

The windows are the same every day, so they are plain times rather than
schedules — and the owner can move any of them from the dashboard without a
release. Any window may cross midnight (the PdC grid window does by default,
23:00 -> 07:00); `supervisor.windows.in_window` is the only place that is
interpreted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import VillaPoolConfigEntry
from .const import (
    DEFAULT_CHLORINE_END,
    DEFAULT_CHLORINE_START,
    DEFAULT_DEADLINE,
    DEFAULT_PDC_GRID_END,
    DEFAULT_PDC_GRID_START,
    DEFAULT_PDC_SOLAR_END,
    DEFAULT_PDC_SOLAR_START,
    DEFAULT_PUMP_END,
    DEFAULT_PUMP_START,
    DEFAULT_WINTER_START,
)
from .entity import PoolEntity


def _t(value: str) -> time:
    hh, _, mm = value.partition(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class WindowTime:
    key: str
    name: str
    default: time
    icon: str


WINDOW_TIMES: tuple[WindowTime, ...] = (
    WindowTime("pump_start", "Pump start", _t(DEFAULT_PUMP_START), "mdi:pump"),
    WindowTime("pump_end", "Pump end", _t(DEFAULT_PUMP_END), "mdi:pump-off"),
    WindowTime("pdc_solar_start", "PdC solar start",
               _t(DEFAULT_PDC_SOLAR_START), "mdi:solar-power"),
    WindowTime("pdc_solar_end", "PdC solar end",
               _t(DEFAULT_PDC_SOLAR_END), "mdi:solar-power-variant-outline"),
    WindowTime("pdc_grid_start", "PdC grid start",
               _t(DEFAULT_PDC_GRID_START), "mdi:transmission-tower"),
    WindowTime("pdc_grid_end", "PdC grid end",
               _t(DEFAULT_PDC_GRID_END), "mdi:transmission-tower-off"),
    WindowTime("chlorine_start", "Chlorine start",
               _t(DEFAULT_CHLORINE_START), "mdi:test-tube"),
    WindowTime("chlorine_end", "Chlorine end",
               _t(DEFAULT_CHLORINE_END), "mdi:test-tube-off"),
    WindowTime("deadline", "Deadline", _t(DEFAULT_DEADLINE),
               "mdi:clock-alert-outline"),
    WindowTime("winter_start", "Winter start", _t(DEFAULT_WINTER_START),
               "mdi:snowflake-alert"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaPoolConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([PoolWindowTime(entry, w) for w in WINDOW_TIMES])


class PoolWindowTime(PoolEntity, TimeEntity, RestoreEntity):
    """One window boundary, restored across restarts."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaPoolConfigEntry, window: WindowTime) -> None:
        super().__init__(entry, window.key)
        self._window = window
        self._attr_name = window.name
        self._attr_icon = window.icon
        self._attr_native_value = window.default

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (None, "unknown", "unavailable"):
            parsed = dt_util.parse_time(last.state)
            if parsed is not None:
                self._attr_native_value = parsed
        self._publish()

    def _publish(self) -> None:
        self._entry.runtime_data.settings[self._window.key] = self._attr_native_value

    async def async_set_value(self, value: time) -> None:
        self._attr_native_value = value
        self._publish()
        self.async_write_ha_state()
        await self._request_run()
