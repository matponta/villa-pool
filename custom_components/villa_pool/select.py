"""Pool mode select (STORY §4)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import VillaPoolConfigEntry
from .const import MODE_AUTO, POOL_MODES
from .entity import PoolEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaPoolConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([PoolModeSelect(entry)])


class PoolModeSelect(PoolEntity, SelectEntity, RestoreEntity):
    """auto | filtration_only | winter | manual | closed.

    `manual` and `closed` freeze the supervisor entirely (rung 1 of the §5.5
    ladder): it stops deciding for the actuators and says so in the reason.
    `winter` swaps the summer pump window for the noon slot and blocks the PdC.
    """

    _attr_name = "Pool mode"
    _attr_icon = "mdi:pool"
    _attr_options = POOL_MODES

    def __init__(self, entry: VillaPoolConfigEntry) -> None:
        super().__init__(entry, "mode")
        self._attr_current_option = MODE_AUTO

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in POOL_MODES:
            self._attr_current_option = last.state
        self._publish()

    def _publish(self) -> None:
        self._entry.runtime_data.settings["mode"] = self._attr_current_option

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self._publish()
        self.async_write_ha_state()
        await self._request_run()
