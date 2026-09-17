"""The supervisor's own switches (STORY §4), including the v0.1.0 dry-run flag.

None of these actuate anything themselves — they are inputs to the control law,
restored across restarts and published into `runtime_data.settings`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity

from . import VillaPoolConfigEntry
from .const import MAINTENANCE_AUTO_OFF_S
from .entity import PoolEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Flag:
    key: str
    name: str
    default: bool
    icon: str
    description: str


FLAGS: tuple[Flag, ...] = (
    Flag(
        "dry_run", "Dry run", True, "mdi:file-eye-outline",
        "ON: the supervisor only says what it would do. v0.1.0 has no write "
        "path at all, so turning this off does not actuate anything yet.",
    ),
    Flag(
        "grid_heating", "Grid heating", True, "mdi:transmission-tower",
        "Allow the PdC to heat from the grid (owner decision §3: ON from day "
        "one). Never in the F2 band, whatever this says.",
    ),
    Flag(
        "chlorine_target_control", "Chlorine target control", True,
        "mdi:test-tube",
        "OFF: the chlorinator follows its window only and ignores the daily "
        "hours target (escape hatch).",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaPoolConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entities: list[SwitchEntity] = [PoolFlagSwitch(entry, f) for f in FLAGS]
    entities.append(MaintenanceSwitch(entry))
    async_add_entities(entities)


class PoolFlagSwitch(PoolEntity, SwitchEntity, RestoreEntity):
    """A restoring boolean setting."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaPoolConfigEntry, flag: Flag) -> None:
        super().__init__(entry, flag.key)
        self._flag = flag
        self._attr_name = flag.name
        self._attr_icon = flag.icon
        self._attr_is_on = flag.default

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON
        self._publish()

    def _publish(self) -> None:
        self._entry.runtime_data.settings[self._flag.key] = self._attr_is_on

    @property
    def extra_state_attributes(self) -> dict:
        return {"description": self._flag.description}

    async def _set(self, value: bool) -> None:
        self._attr_is_on = value
        self._publish()
        self.async_write_ha_state()
        await self._request_run()

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)


class MaintenanceSwitch(PoolEntity, SwitchEntity, RestoreEntity):
    """Freezes all actuation, and turns itself off after 4 h (STORY §4).

    The auto-off exists because the failure mode of a maintenance switch is
    always the same: someone flips it to work on the pool, and then nobody
    remembers. A pool left unfiltered and unchlorinated for a week is a much
    worse outcome than an unexpected re-arm, so this expires on its own and
    says so in the log.
    """

    _attr_name = "Maintenance"
    _attr_icon = "mdi:wrench-clock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaPoolConfigEntry) -> None:
        super().__init__(entry, "maintenance")
        self._attr_is_on = False
        self._cancel = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON
        self._publish()
        if self._attr_is_on:
            # Restored ON after a restart: re-arm the timer rather than letting
            # a restart silently make the freeze permanent.
            self._arm()

    def _publish(self) -> None:
        self._entry.runtime_data.settings["maintenance"] = self._attr_is_on

    def _arm(self) -> None:
        self._disarm()
        self._cancel = async_call_later(
            self.hass, timedelta(seconds=MAINTENANCE_AUTO_OFF_S), self._expire
        )

    def _disarm(self) -> None:
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    @callback
    def _expire(self, _now) -> None:
        self._cancel = None
        if not self._attr_is_on:
            return
        _LOGGER.warning(
            "Maintenance auto-off after %.0f h — the supervisor is deciding again.",
            MAINTENANCE_AUTO_OFF_S / 3600,
        )
        self._attr_is_on = False
        self._publish()
        self.async_write_ha_state()
        self.hass.async_create_task(self._request_run())

    async def async_added_to_hass_cleanup(self) -> None:  # pragma: no cover
        self._disarm()

    async def async_will_remove_from_hass(self) -> None:
        self._disarm()
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self._publish()
        self.async_write_ha_state()
        self._arm()
        await self._request_run()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self._publish()
        self.async_write_ha_state()
        self._disarm()
        await self._request_run()
