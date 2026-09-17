"""Settings exposed as `number.*` (STORY §4).

Every one restores across restarts and publishes into `runtime_data.settings`,
which is what the engine reads. Defaults come from the owner's fixed decisions
in §3 — changing them here is a dashboard action, not a release.
"""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.const import UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import VillaPoolConfigEntry
from .const import (
    ANTIFREEZE_SPEED_MAX,
    ANTIFREEZE_SPEED_MIN,
    DEFAULT_ANTIFREEZE_OFF_C,
    DEFAULT_ANTIFREEZE_ON_C,
    DEFAULT_ANTIFREEZE_SPEED,
    DEFAULT_COVER_CHLORINE_FACTOR,
    DEFAULT_FILTRATION_SPEED,
    DEFAULT_MIN_TEMP,
    DEFAULT_PDC_SPEED,
    DEFAULT_PRICE_F1,
    DEFAULT_PRICE_F2,
    DEFAULT_PRICE_F3,
    DEFAULT_SOLAR_OFF_W,
    DEFAULT_SOLAR_ON_W,
    DEFAULT_SOLAR_TARGET_TEMP,
    DEFAULT_TARGET_CHLORINE_HOURS,
    DEFAULT_TARGET_TURNOVERS,
    DEFAULT_WINTER_CHLORINE_HOURS,
    DEFAULT_WINTER_HOURS,
    PUMP_SPEED_MAX,
    PUMP_SPEED_MIN,
    PUMP_SPEED_STEP,
)
from .entity import PoolEntity


@dataclass(frozen=True)
class Setting:
    """One number setting."""

    key: str
    name: str
    default: float
    minimum: float
    maximum: float
    step: float
    unit: str | None = None
    icon: str | None = None
    device_class: NumberDeviceClass | None = None
    config: bool = True


SETTINGS: tuple[Setting, ...] = (
    # --- targets -------------------------------------------------------------
    Setting("target_turnovers", "Target turnovers", DEFAULT_TARGET_TURNOVERS,
            0.5, 2.0, 0.1, icon="mdi:sync"),
    Setting("target_chlorine_hours", "Target chlorine hours",
            DEFAULT_TARGET_CHLORINE_HOURS, 0, 12, 0.5, "h", "mdi:test-tube"),
    Setting("winter_chlorine_hours", "Winter chlorine hours",
            DEFAULT_WINTER_CHLORINE_HOURS, 0, 12, 0.5, "h", "mdi:snowflake"),
    Setting("cover_chlorine_factor", "Cover chlorine factor",
            DEFAULT_COVER_CHLORINE_FACTOR, 0.1, 1.0, 0.05, None,
            "mdi:sun-snowflake-variant"),
    Setting("winter_hours", "Winter pump hours", DEFAULT_WINTER_HOURS,
            0, 12, 0.5, "h", "mdi:pump"),
    # --- temperatures --------------------------------------------------------
    Setting("min_temp", "Minimum temperature", DEFAULT_MIN_TEMP, 20, 32, 0.1,
            UnitOfTemperature.CELSIUS, "mdi:thermometer-low",
            NumberDeviceClass.TEMPERATURE),
    Setting("solar_target_temp", "Solar target temperature",
            DEFAULT_SOLAR_TARGET_TEMP, 20, 34, 0.1, UnitOfTemperature.CELSIUS,
            "mdi:solar-power", NumberDeviceClass.TEMPERATURE),
    Setting("antifreeze_on_c", "Antifreeze engage", DEFAULT_ANTIFREEZE_ON_C,
            -10, 5, 0.5, UnitOfTemperature.CELSIUS, "mdi:snowflake-alert",
            NumberDeviceClass.TEMPERATURE),
    Setting("antifreeze_off_c", "Antifreeze release", DEFAULT_ANTIFREEZE_OFF_C,
            -5, 10, 0.5, UnitOfTemperature.CELSIUS, "mdi:snowflake-off",
            NumberDeviceClass.TEMPERATURE),
    # --- solar thresholds ----------------------------------------------------
    Setting("solar_on_w", "Solar headroom on", DEFAULT_SOLAR_ON_W,
            500, 8000, 100, UnitOfPower.WATT, "mdi:weather-sunny",
            NumberDeviceClass.POWER),
    Setting("solar_off_w", "Solar headroom off", DEFAULT_SOLAR_OFF_W,
            500, 8000, 100, UnitOfPower.WATT, "mdi:weather-partly-cloudy",
            NumberDeviceClass.POWER),
    # --- pump speeds ---------------------------------------------------------
    Setting("filtration_speed", "Filtration speed", DEFAULT_FILTRATION_SPEED,
            PUMP_SPEED_MIN, PUMP_SPEED_MAX, PUMP_SPEED_STEP, "%", "mdi:pump"),
    Setting("pdc_speed", "PdC pump speed", DEFAULT_PDC_SPEED,
            PUMP_SPEED_MIN, PUMP_SPEED_MAX, PUMP_SPEED_STEP, "%",
            "mdi:heat-pump"),
    Setting("antifreeze_speed", "Antifreeze speed", DEFAULT_ANTIFREEZE_SPEED,
            ANTIFREEZE_SPEED_MIN, ANTIFREEZE_SPEED_MAX, PUMP_SPEED_STEP, "%",
            "mdi:snowflake"),
    # --- tariff (diagnostic only — feeds the cost estimate, not the law) ----
    Setting("price_f1", "Price F1", DEFAULT_PRICE_F1, 0, 2, 0.001,
            "EUR/kWh", "mdi:currency-eur"),
    Setting("price_f2", "Price F2", DEFAULT_PRICE_F2, 0, 2, 0.001,
            "EUR/kWh", "mdi:currency-eur"),
    Setting("price_f3", "Price F3", DEFAULT_PRICE_F3, 0, 2, 0.001,
            "EUR/kWh", "mdi:currency-eur"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaPoolConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([PoolNumber(entry, s) for s in SETTINGS])


class PoolNumber(PoolEntity, NumberEntity, RestoreEntity):
    """A restoring setting slider that publishes into `runtime_data.settings`."""

    _attr_mode = NumberMode.BOX

    def __init__(self, entry: VillaPoolConfigEntry, setting: Setting) -> None:
        super().__init__(entry, setting.key)
        self._setting = setting
        self._attr_name = setting.name
        self._attr_icon = setting.icon
        self._attr_native_min_value = setting.minimum
        self._attr_native_max_value = setting.maximum
        self._attr_native_step = setting.step
        self._attr_native_unit_of_measurement = setting.unit
        self._attr_device_class = setting.device_class
        self._attr_native_value = setting.default
        if setting.config:
            self._attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (None, "unknown", "unavailable"):
            try:
                self._attr_native_value = float(last.state)
            except (TypeError, ValueError):
                pass
        self._publish()

    def _publish(self) -> None:
        self._entry.runtime_data.settings[self._setting.key] = self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._publish()
        self.async_write_ha_state()
        await self._request_run()
