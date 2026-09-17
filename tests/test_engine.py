"""End-to-end tests through a real Home Assistant.

These cover what the pure tests cannot: that the integration loads, that the
setting entities exist and feed the engine, that the dry run really writes
nothing, and the three acceptance criteria whose substance is HA plumbing
(§7.5 blocked-on-fault, §7.9 no-write during a polling gap, §7.10 restart).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.villa_pool.const import (
    DEFAULT_CHLORINATOR_SWITCH,
    DEFAULT_PDC_CLIMATE,
    DEFAULT_PUMP_RUNNING,
    DEFAULT_PUMP_SWITCH,
    DEFAULT_TARIFF_BAND,
    DEFAULT_WATER_TEMP,
    DOMAIN,
    PDC_BLOCKED,
    PDC_GRID,
)
from custom_components.villa_pool.engine import ACTUATION_IMPLEMENTED

from .conftest import ENTRY_DATA

TICK = timedelta(seconds=60)


async def setup_pool(hass: HomeAssistant, **states) -> MockConfigEntry:
    """Seed the input entities, then load the integration."""
    defaults = {
        DEFAULT_PUMP_RUNNING: "on",
        DEFAULT_PUMP_SWITCH: "on",
        "binary_sensor.pompa_piscina_problem": "off",
        "binary_sensor.pompa_piscina_flow_pressure_warning": "off",
        "sensor.pompa_piscina_volume_flow_rate": "8",
        "sensor.pompa_piscina_power": "427",
        DEFAULT_PDC_CLIMATE: "off",
        "binary_sensor.pool_pdc_acceso": "off",
        "binary_sensor.pool_pdc_guasto": "off",
        DEFAULT_CHLORINATOR_SWITCH: "off",
        "binary_sensor.salt_chlorinator_running": "off",
        "sensor.salt_chlorinator_runtime_today": "0",
        DEFAULT_WATER_TEMP: "26.0",
        "sensor.gw3000a_outdoor_temperature": "20.0",
        "sensor.solar_headroom_for_heater": "0",
        DEFAULT_TARIFF_BAND: "F1",
        "input_boolean.pool_in_use": "off",
    }
    # The villa is in Italy and every window in the STORY is local time; the
    # test harness otherwise defaults to UTC, which silently shifts 23:00 into
    # a different tariff band.
    await hass.config.async_set_time_zone("Europe/Rome")
    defaults.update(states)
    for entity_id, state in defaults.items():
        hass.states.async_set(entity_id, state)

    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def tick(hass: HomeAssistant, times: int = 1, freezer=None) -> None:
    """Advance `times` coordinator intervals.

    With a frozen clock the clock has to be moved explicitly, or every fired
    time-change lands on the same instant and only the first one counts.
    """
    for _ in range(times):
        if freezer is not None:
            freezer.tick(TICK)
        async_fire_time_changed(hass, dt_util.utcnow() + TICK)
        await hass.async_block_till_done()


# --- loading -----------------------------------------------------------------

async def test_integration_loads(hass: HomeAssistant) -> None:
    entry = await setup_pool(hass)
    assert entry.state.name == "LOADED"


async def test_the_settings_entities_exist(hass: HomeAssistant) -> None:
    await setup_pool(hass)
    for entity_id in (
        "switch.pool_dry_run",
        "switch.pool_grid_heating",
        "switch.pool_maintenance",
        "select.pool_mode",
        "number.pool_min_temp",
        "time.pool_pdc_grid_start",
        "sensor.pool_supervisor_reason",
        "sensor.pool_pdc_state",
        "binary_sensor.pool_solar_ok",
    ):
        assert hass.states.get(entity_id) is not None, entity_id


async def test_dry_run_is_on_by_default(hass: HomeAssistant) -> None:
    await setup_pool(hass)
    assert hass.states.get("switch.pool_dry_run").state == "on"


async def test_grid_heating_is_on_by_default(hass: HomeAssistant) -> None:
    """Owner decision §3: allowed from day one."""
    await setup_pool(hass)
    assert hass.states.get("switch.pool_grid_heating").state == "on"


async def test_min_temp_default_is_27(hass: HomeAssistant) -> None:
    await setup_pool(hass)
    state = hass.states.get("number.pool_min_temp")
    assert float(state.state) == 27.0


# --- the dry run really is dry ----------------------------------------------

async def test_v0_1_0_has_no_actuation_path(hass: HomeAssistant) -> None:
    """The capability is ABSENT, not merely disabled (see engine.py)."""
    assert ACTUATION_IMPLEMENTED is False


@pytest.mark.parametrize("domain,service", [
    ("switch", "turn_on"), ("switch", "turn_off"),
    ("climate", "set_hvac_mode"), ("climate", "set_temperature"),
    ("number", "set_value"), ("select", "select_option"),
])
async def test_no_service_calls_are_ever_made(
    hass: HomeAssistant, domain: str, service: str
) -> None:
    """Whatever the pool is doing, v0.1.0 calls nothing on it."""
    calls = async_mock_service(hass, domain, service)
    await setup_pool(
        hass,
        **{
            DEFAULT_WATER_TEMP: "24.0",          # cold: would want heating
            "sensor.solar_headroom_for_heater": "4000",   # sunny: would want SOLAR
            "input_boolean.pool_in_use": "on",   # would want pump + chlorine
        },
    )
    await tick(hass, times=20)
    assert calls == []


async def test_turning_dry_run_off_still_writes_nothing(
    hass: HomeAssistant, caplog
) -> None:
    calls = async_mock_service(hass, "switch", "turn_on")
    await setup_pool(hass, **{DEFAULT_WATER_TEMP: "24.0"})
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.pool_dry_run"}, blocking=True,
    )
    await tick(hass, times=3)
    assert calls == []
    assert "no actuation path" in caplog.text


# --- the reason surface ------------------------------------------------------

async def test_reason_sensor_explains_every_actuator(hass: HomeAssistant) -> None:
    await setup_pool(hass, **{"input_boolean.pool_in_use": "on"})
    await tick(hass, times=2)
    reason = hass.states.get("sensor.pool_supervisor_reason")
    assert "pump" in reason.state
    assert "PdC" in reason.state
    assert "chlorine" in reason.state


async def test_reason_attributes_carry_the_intent(hass: HomeAssistant) -> None:
    await setup_pool(hass, **{"input_boolean.pool_in_use": "on"})
    await tick(hass, times=2)
    attrs = hass.states.get("sensor.pool_supervisor_reason").attributes
    assert attrs["dry_run"] is True
    assert "would_pump_on" in attrs
    assert "would_pdc_state" in attrs
    assert "would_chlorine_on" in attrs


async def test_intent_changes_are_logged_at_info(
    hass: HomeAssistant, caplog
) -> None:
    """§8 step 1: 'log intended writes only' — and on change, not every tick."""
    import logging
    caplog.set_level(logging.INFO)
    await setup_pool(hass, **{"input_boolean.pool_in_use": "on"})
    await tick(hass, times=2)
    assert "DRY-RUN" in caplog.text


# --- §7.5 through a real HA --------------------------------------------------

async def test_pump_problem_blocks_the_pdc(hass: HomeAssistant) -> None:
    await setup_pool(hass, **{
        DEFAULT_WATER_TEMP: "24.0",
        "sensor.solar_headroom_for_heater": "4000",
    })
    hass.states.async_set("binary_sensor.pompa_piscina_problem", "on")
    await tick(hass, times=2)
    assert hass.states.get("sensor.pool_pdc_state").state == PDC_BLOCKED


async def test_blocked_reason_is_published(hass: HomeAssistant) -> None:
    await setup_pool(hass)
    hass.states.async_set("binary_sensor.pompa_piscina_problem", "on")
    await tick(hass, times=2)
    attrs = hass.states.get("sensor.pool_supervisor_reason").attributes
    assert attrs["blocked_reason"] is not None


# --- §7.9 through a real HA --------------------------------------------------

async def test_unavailable_pdc_suppresses_writes(hass: HomeAssistant) -> None:
    await setup_pool(hass)
    hass.states.async_set(DEFAULT_PDC_CLIMATE, "unavailable")
    await tick(hass, times=4)
    attrs = hass.states.get("sensor.pool_pdc_state").attributes
    assert attrs["write_allowed"] is False


# --- §7.2 / §7.10 with freeze_time ------------------------------------------

async def test_grid_run_starts_at_23_00(hass: HomeAssistant) -> None:
    """The night grid window, driven by the real clock."""
    with freeze_time("2026-09-16 23:00:00+02:00") as frozen:
        await setup_pool(hass, **{
            DEFAULT_WATER_TEMP: "25.6",
            DEFAULT_TARIFF_BAND: "F3",
        })
        await tick(hass, times=2, freezer=frozen)
        assert hass.states.get("sensor.pool_pdc_state").state == PDC_GRID


async def test_f2_refuses_grid_and_says_so(hass: HomeAssistant) -> None:
    """§7.4 end-to-end: Saturday inside a grid window, band F2."""
    with freeze_time("2026-09-19 23:30:00+02:00") as frozen:
        await setup_pool(hass, **{
            DEFAULT_WATER_TEMP: "25.0",
            DEFAULT_TARIFF_BAND: "F2",
        })
        await tick(hass, times=2, freezer=frozen)
        reason = hass.states.get("sensor.pool_supervisor_reason").state
        assert "band F2" in reason


async def test_restart_mid_grid_resumes_grid(hass: HomeAssistant) -> None:
    """§7.10: the PdC state is re-derived, not assumed OFF."""
    with freeze_time("2026-09-17 01:00:00+02:00") as frozen:
        await setup_pool(hass, **{
            DEFAULT_WATER_TEMP: "26.0",
            DEFAULT_TARIFF_BAND: "F3",
            "binary_sensor.pool_pdc_acceso": "on",   # it was running before
        })
        await tick(hass, freezer=frozen)
        assert hass.states.get("sensor.pool_pdc_state").state == PDC_GRID


# --- unload ------------------------------------------------------------------

async def test_unload_releases_nothing_destructive(hass: HomeAssistant) -> None:
    """STORY §6: on unload, stop deciding — do NOT turn the pump off."""
    calls = async_mock_service(hass, "switch", "turn_off")
    entry = await setup_pool(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert calls == []


# --- the §4 entity ids are a contract ----------------------------------------

async def test_story_section_4_entity_ids(hass: HomeAssistant) -> None:
    """STORY §4 names these entities explicitly, and the owner's dashboards and
    automations will reference them. They are a contract, not an implementation
    detail: under `has_entity_name` they come from the DEVICE name, so renaming
    the device silently renames all of them."""
    await setup_pool(hass)
    for entity_id in (
        # settings
        "number.pool_target_turnovers",
        "number.pool_target_chlorine_hours",
        "number.pool_winter_chlorine_hours",
        "number.pool_cover_chlorine_factor",
        "number.pool_solar_target_temp",
        "number.pool_min_temp",
        "number.pool_solar_on_w",
        "number.pool_solar_off_w",
        "number.pool_filtration_speed",
        "number.pool_pdc_speed",
        "number.pool_antifreeze_speed",
        "number.pool_antifreeze_on_c",
        "number.pool_antifreeze_off_c",
        "number.pool_winter_hours",
        # windows
        "time.pool_pump_start",
        "time.pool_pump_end",
        "time.pool_pdc_solar_start",
        "time.pool_pdc_solar_end",
        "time.pool_pdc_grid_start",
        "time.pool_pdc_grid_end",
        "time.pool_chlorine_start",
        "time.pool_chlorine_end",
        "time.pool_deadline",
        # mode + switches
        "select.pool_mode",
        "switch.pool_grid_heating",
        "switch.pool_maintenance",
        "switch.pool_chlorine_target_control",
        "switch.pool_dry_run",
        # sensors
        "sensor.pool_supervisor_reason",
        "sensor.pool_pdc_state",
        "sensor.pool_cop_stimato",
        "sensor.pool_costo_termico_stimato",
        "sensor.pool_volume_today",
        "sensor.pool_chlorine_hours_missing",
        "sensor.pool_cover_closed_for",
        "binary_sensor.pool_solar_ok",
    ):
        assert hass.states.get(entity_id) is not None, entity_id
