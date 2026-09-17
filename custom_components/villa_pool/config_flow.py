"""Config + options flow for Villa Pool (single instance).

Unlike villa_hvac — which hard-codes its KNX entity map in `const.py` — this
integration puts every input behind an **entity picker**, pre-filled with the
verified inventory of STORY §2. The pool's entities come from tuya-local,
Shelly, Ecowitt and an aquatemp fork, all of which rename entities across
firmware and integration updates; a renamed probe should be a two-click fix in
the UI, not a release.

The cover sensor (`binary_sensor.pool_telo_chiuso`) is deliberately the one
picker with NO default: it does not exist yet (owner installs it 20-21/9, final
id unknown — STORY §1 says "ask, don't guess"). Left empty, every cover rule
stays inert.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
)

from .const import (
    CONF_CHLORINATOR_HOURS,
    CONF_CHLORINATOR_RUNNING,
    CONF_CHLORINATOR_SWITCH,
    CONF_COVER_CLOSED,
    CONF_GRID_POWER,
    CONF_NOTIFY_TARGET,
    CONF_OUTDOOR_TEMP,
    CONF_PDC_AIR_TEMP,
    CONF_PDC_CLIMATE,
    CONF_PDC_ENERGY,
    CONF_PDC_FAULT,
    CONF_PDC_POWER,
    CONF_PDC_RUNNING,
    CONF_POOL_IN_USE,
    CONF_PUMP_FLOW,
    CONF_PUMP_FLOW_WARNING,
    CONF_PUMP_HOURS_24H,
    CONF_PUMP_MODE,
    CONF_PUMP_POWER,
    CONF_PUMP_PROBLEM,
    CONF_PUMP_RUNNING,
    CONF_PUMP_SPEED,
    CONF_PUMP_SWITCH,
    CONF_SOLAR_HEADROOM,
    CONF_TARIFF_BAND,
    CONF_WATER_TEMP,
    CONF_WORKDAY,
    DEFAULT_CHLORINATOR_HOURS,
    DEFAULT_CHLORINATOR_RUNNING,
    DEFAULT_CHLORINATOR_SWITCH,
    DEFAULT_COVER_CLOSED,
    DEFAULT_GRID_POWER,
    DEFAULT_NOTIFY_TARGET,
    DEFAULT_OUTDOOR_TEMP,
    DEFAULT_PDC_AIR_TEMP,
    DEFAULT_PDC_CLIMATE,
    DEFAULT_PDC_ENERGY,
    DEFAULT_PDC_FAULT,
    DEFAULT_PDC_POWER,
    DEFAULT_PDC_RUNNING,
    DEFAULT_POOL_IN_USE,
    DEFAULT_PUMP_FLOW,
    DEFAULT_PUMP_FLOW_WARNING,
    DEFAULT_PUMP_HOURS_24H,
    DEFAULT_PUMP_MODE,
    DEFAULT_PUMP_POWER,
    DEFAULT_PUMP_PROBLEM,
    DEFAULT_PUMP_RUNNING,
    DEFAULT_PUMP_SPEED,
    DEFAULT_PUMP_SWITCH,
    DEFAULT_SOLAR_HEADROOM,
    DEFAULT_TARIFF_BAND,
    DEFAULT_WATER_TEMP,
    DEFAULT_WORKDAY,
    DOMAIN,
)

# (key, default, domain(s)). One row per input the integration reads.
# `domain=None` means "any domain" (the tariff-band and workday helpers are
# template sensors whose domain the owner may change).
PICKERS: tuple[tuple[str, str, list[str] | None], ...] = (
    # --- pump ---------------------------------------------------------------
    (CONF_PUMP_SWITCH, DEFAULT_PUMP_SWITCH, ["switch"]),
    (CONF_PUMP_RUNNING, DEFAULT_PUMP_RUNNING, ["binary_sensor"]),
    (CONF_PUMP_MODE, DEFAULT_PUMP_MODE, ["select"]),
    (CONF_PUMP_SPEED, DEFAULT_PUMP_SPEED, ["number"]),
    (CONF_PUMP_FLOW, DEFAULT_PUMP_FLOW, ["sensor"]),
    (CONF_PUMP_POWER, DEFAULT_PUMP_POWER, ["sensor"]),
    (CONF_PUMP_PROBLEM, DEFAULT_PUMP_PROBLEM, ["binary_sensor"]),
    (CONF_PUMP_FLOW_WARNING, DEFAULT_PUMP_FLOW_WARNING, ["binary_sensor"]),
    (CONF_PUMP_HOURS_24H, DEFAULT_PUMP_HOURS_24H, ["sensor"]),
    # --- heat pump ----------------------------------------------------------
    (CONF_PDC_CLIMATE, DEFAULT_PDC_CLIMATE, ["climate"]),
    (CONF_PDC_RUNNING, DEFAULT_PDC_RUNNING, ["binary_sensor"]),
    (CONF_PDC_FAULT, DEFAULT_PDC_FAULT, ["binary_sensor"]),
    (CONF_PDC_POWER, DEFAULT_PDC_POWER, ["sensor"]),
    (CONF_PDC_ENERGY, DEFAULT_PDC_ENERGY, ["sensor"]),
    (CONF_PDC_AIR_TEMP, DEFAULT_PDC_AIR_TEMP, ["sensor"]),
    # --- chlorinator --------------------------------------------------------
    (CONF_CHLORINATOR_SWITCH, DEFAULT_CHLORINATOR_SWITCH, ["switch"]),
    (CONF_CHLORINATOR_RUNNING, DEFAULT_CHLORINATOR_RUNNING, ["binary_sensor"]),
    (CONF_CHLORINATOR_HOURS, DEFAULT_CHLORINATOR_HOURS, ["sensor"]),
    # --- environment + energy ------------------------------------------------
    (CONF_WATER_TEMP, DEFAULT_WATER_TEMP, ["sensor"]),
    (CONF_OUTDOOR_TEMP, DEFAULT_OUTDOOR_TEMP, ["sensor"]),
    (CONF_SOLAR_HEADROOM, DEFAULT_SOLAR_HEADROOM, ["sensor"]),
    (CONF_GRID_POWER, DEFAULT_GRID_POWER, ["sensor"]),
    (CONF_TARIFF_BAND, DEFAULT_TARIFF_BAND, None),
    (CONF_WORKDAY, DEFAULT_WORKDAY, ["binary_sensor"]),
    # --- owner inputs --------------------------------------------------------
    (CONF_POOL_IN_USE, DEFAULT_POOL_IN_USE, ["input_boolean"]),
    # The one picker with no default — the sensor does not exist yet (§1).
    (CONF_COVER_CLOSED, DEFAULT_COVER_CLOSED, ["binary_sensor"]),
)


def _schema(current: dict[str, Any]) -> vol.Schema:
    """Build the picker schema, pre-filled from `current` then the §2 default.

    Every field is Optional: a pool with, say, no flow-warning sensor should
    still be configurable, and the control law already treats a missing input
    as "unknown" rather than as a value.
    """
    fields: dict[Any, Any] = {}
    for key, default, domains in PICKERS:
        value = current.get(key, default)
        selector = EntitySelector(
            EntitySelectorConfig(domain=domains) if domains else EntitySelectorConfig()
        )
        if value:
            fields[vol.Optional(key, default=value)] = selector
        else:
            # No default and nothing chosen yet: show it empty rather than
            # pre-filling a guess (STORY §1 — "ask, don't guess").
            fields[vol.Optional(key)] = selector
    fields[
        vol.Optional(
            CONF_NOTIFY_TARGET,
            default=current.get(CONF_NOTIFY_TARGET, DEFAULT_NOTIFY_TARGET),
        )
    ] = TextSelector()
    return vol.Schema(fields)


class VillaPoolConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Villa Pool."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Villa Pool", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VillaPoolOptionsFlow()


class VillaPoolOptionsFlow(OptionsFlow):
    """Re-pick any input later (a renamed entity, or the cover once installed)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
