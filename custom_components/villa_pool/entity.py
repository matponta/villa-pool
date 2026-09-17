"""Shared entity base: one device so the owner assigns the area once.

Owner rule (STORY §6): every entity/device gets the `pool` area. Hanging all of
this integration's entities off a single service device means that is one
assignment in the UI rather than thirty.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


def pool_device(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Villa Pool",
        manufacturer="Villa Pontacolone",
        model="Pool supervisor",
        entry_type=DeviceEntryType.SERVICE,
    )


class PoolEntity(Entity):
    """Common wiring for every villa_pool entity."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = pool_device(entry)

    async def _request_run(self) -> None:
        """Nudge the engine so a setting change is reflected immediately."""
        engine = getattr(self._entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.request_run()
