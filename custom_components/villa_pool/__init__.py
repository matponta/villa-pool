"""The Villa Pool supervision integration.

Supervises the pool's pump, heat pump (PdC) and salt chlorinator as one
hydraulic system: windows (when things are allowed), daily targets (what must
be achieved) and interlocks (what can never be violated), with the pump
following demand.

It does NOT replace the devices' own controllers — the PdC keeps its thermostat
(HA would write only `hvac_mode` + setpoint) and the UNIKO keeps its cycles (HA
would only *enable* it).

**v0.1.0 writes nothing at all.** See `engine.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .coordinator import VillaPoolCoordinator
from .engine import SupervisorEngine


@dataclass
class PoolRuntime:
    """What lives on `entry.runtime_data`.

    `settings` is the live mirror of the integration's own setting entities
    (numbers, times, the mode select, the switches), keyed by the same suffix
    their unique_id uses. Each entity writes its value here on add and on
    change, so the engine reads plain Python values and never has to go back
    through the state machine to find its own configuration.
    """

    coordinator: VillaPoolCoordinator
    engine: SupervisorEngine | None = None
    settings: dict[str, Any] = field(default_factory=dict)


VillaPoolConfigEntry = ConfigEntry[PoolRuntime]


async def async_setup_entry(hass: HomeAssistant, entry: VillaPoolConfigEntry) -> bool:
    """Set up Villa Pool from a config entry."""
    coordinator = VillaPoolCoordinator(hass, entry)
    runtime = PoolRuntime(coordinator=coordinator)
    entry.runtime_data = runtime

    await coordinator.async_config_entry_first_refresh()

    # The engine is CONSTRUCTED before the platforms so the diagnostic entities
    # can subscribe to it as they are added — otherwise they would publish one
    # tick (a full minute) behind every decision. It is only STARTED afterwards,
    # because it reads the setting entities through `runtime.settings` and those
    # do not exist until the platforms are up.
    engine = SupervisorEngine(hass, entry, coordinator)
    runtime.engine = engine

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    engine.start()
    entry.async_on_unload(engine.stop)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: VillaPoolConfigEntry) -> None:
    """Reload when the entity pickers change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VillaPoolConfigEntry) -> bool:
    """Unload a config entry.

    STORY §6: release nothing destructive on unload — do NOT turn the pump off,
    just stop deciding. In v0.1.0 there is nothing to release in any case, but
    the rule is recorded here because it is the one a future actuating version
    must not quietly break.
    """
    engine = getattr(entry.runtime_data, "engine", None)
    if engine is not None:
        engine.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
