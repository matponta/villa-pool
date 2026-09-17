"""DataUpdateCoordinator for Villa Pool — read-only.

Every configured input is read once per 60 s tick into a plain dict. The
coordinator does no interpretation beyond coercing types and distinguishing
"unknown" from a value: all judgement lives in the pure `supervisor/` package.

The one piece of domain knowledge here is which reads are allowed to be
missing. `unavailable`/`unknown` becomes `None`, never `False` and never `0` —
a tuya-local switch dropping off the bus must not read as "the pump stopped",
and a cloud-polled climate between polls must not read as "the PdC is off"
(STORY §5.2, §6).
"""
from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHLORINATOR_HOURS,
    CONF_CHLORINATOR_RUNNING,
    CONF_CHLORINATOR_SWITCH,
    CONF_COVER_CLOSED,
    CONF_GRID_POWER,
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
    CONF_PUMP_POWER,
    CONF_PUMP_PROBLEM,
    CONF_PUMP_RUNNING,
    CONF_SOLAR_HEADROOM,
    CONF_TARIFF_BAND,
    CONF_WATER_TEMP,
    CONF_WORKDAY,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

UNKNOWN = ("unavailable", "unknown", "", None)


class VillaPoolCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls HA state for every pool input."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, name="Villa Pool", update_interval=UPDATE_INTERVAL
        )
        self.entry = entry
        # m3 integrated from the flow sensor across today (STORY §4), reset at
        # midnight by the sensor that owns it.
        self.volume_today_m3 = 0.0
        self._last_volume_ts = None

    # --- configuration -------------------------------------------------------

    def eid(self, key: str) -> str | None:
        """The entity id configured for `key`, or None if the picker is empty."""
        merged = {**self.entry.data, **self.entry.options}
        value = merged.get(key)
        return value or None

    # --- typed reads ---------------------------------------------------------

    def _raw(self, key: str) -> str | None:
        entity_id = self.eid(key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in UNKNOWN:
            return None
        return state.state

    def num(self, key: str) -> float | None:
        raw = self._raw(key)
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def onoff(self, key: str) -> bool | None:
        """Tri-state: True / False / None (unknown).

        The None is the point. `binary_sensor.pool_pompa_in_marcia` going
        `unavailable` is not the pump stopping.
        """
        raw = self._raw(key)
        if raw is None:
            return None
        return raw == "on"

    def text(self, key: str) -> str | None:
        return self._raw(key)

    def available(self, key: str) -> bool:
        """Is the entity present AND carrying a usable state right now?"""
        return self._raw(key) is not None

    # --- volume integration --------------------------------------------------

    def _integrate_volume(self, flow_m3h: float | None, pump_running: bool | None) -> None:
        """Accumulate m3 filtered today from the flow sensor (STORY §4).

        Credited only while the pump is genuinely in marcia, and only for a
        plausible gap (<= 3 tick intervals) so a restart or an outage we did not
        observe is never counted as hours of filtration. Turnovers are this
        divided by POOL_VOLUME_M3.
        """
        now = dt_util.utcnow()
        last, self._last_volume_ts = self._last_volume_ts, now
        if flow_m3h is None or pump_running is not True or last is None:
            return
        gap_s = (now - last).total_seconds()
        if 0 < gap_s <= 3 * UPDATE_INTERVAL.total_seconds():
            self.volume_today_m3 += flow_m3h * gap_s / 3600.0

    def reset_volume(self) -> None:
        """Midnight reset, driven by the sensor that owns the daily total."""
        self.volume_today_m3 = 0.0

    def seed_volume(self, value: float) -> None:
        """Restore today's accumulated volume after a restart."""
        if value >= 0:
            self.volume_today_m3 = value

    # --- the tick ------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        pump_running = self.onoff(CONF_PUMP_RUNNING)
        pump_flow = self.num(CONF_PUMP_FLOW)
        self._integrate_volume(pump_flow, pump_running)
        return {
            # pump
            "pump_running": pump_running,
            "pump_problem": self.onoff(CONF_PUMP_PROBLEM) is True,
            "pump_flow_warning": self.onoff(CONF_PUMP_FLOW_WARNING) is True,
            "pump_flow": pump_flow,
            "pump_power": self.num(CONF_PUMP_POWER),
            # heat pump
            "pdc_running": self.onoff(CONF_PDC_RUNNING),
            "pdc_fault": self.onoff(CONF_PDC_FAULT) is True,
            "pdc_available": self.available(CONF_PDC_CLIMATE),
            "pdc_power": self.num(CONF_PDC_POWER),
            # Cumulative kWh on phase A. Read for the §5.4 session log, which
            # brackets it across a heating run; nothing in the control law uses
            # it.
            "pdc_energy": self.num(CONF_PDC_ENERGY),
            "pdc_air_temp": self.num(CONF_PDC_AIR_TEMP),
            # chlorinator
            "chlorine_switch": self.onoff(CONF_CHLORINATOR_SWITCH),
            "chlorine_running": self.onoff(CONF_CHLORINATOR_RUNNING),
            "chlorine_hours_today": self.num(CONF_CHLORINATOR_HOURS) or 0.0,
            # environment + energy
            "water_temp": self.num(CONF_WATER_TEMP),
            "outdoor_temp": self.num(CONF_OUTDOOR_TEMP),
            "headroom_w": self.num(CONF_SOLAR_HEADROOM),
            "grid_power": self.num(CONF_GRID_POWER),
            "band": self.text(CONF_TARIFF_BAND),
            "workday": self.onoff(CONF_WORKDAY),
            # owner inputs
            "pool_in_use": self.onoff(CONF_POOL_IN_USE) is True,
            "cover_closed": self.onoff(CONF_COVER_CLOSED),
            "volume_today_m3": self.volume_today_m3,
        }
