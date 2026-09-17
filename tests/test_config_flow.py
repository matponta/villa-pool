"""Config + options flow: the pickers and their §2 defaults."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_pool.config_flow import PICKERS
from custom_components.villa_pool.const import (
    CONF_COVER_CLOSED,
    CONF_PUMP_RUNNING,
    CONF_PUMP_SWITCH,
    DEFAULT_PUMP_RUNNING,
    DEFAULT_PUMP_SWITCH,
    DOMAIN,
)

from .conftest import ENTRY_DATA


async def test_user_flow_creates_the_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ENTRY_DATA
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Villa Pool"


async def test_single_instance_only(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=DOMAIN).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.ABORT


async def test_pump_pickers_default_to_the_verified_inventory(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    defaults = {
        key.schema: key.default() for key in result["data_schema"].schema
        if hasattr(key, "default") and key.default is not None
        and callable(key.default)
    }
    assert defaults[CONF_PUMP_SWITCH] == DEFAULT_PUMP_SWITCH
    assert defaults[CONF_PUMP_RUNNING] == DEFAULT_PUMP_RUNNING


async def test_the_cover_picker_has_no_default(hass: HomeAssistant) -> None:
    """STORY §1: the sensor does not exist yet — ask, don't guess."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    for key in result["data_schema"].schema:
        if key.schema == CONF_COVER_CLOSED:
            # voluptuous uses its own UNDEFINED sentinel for "no default"; what
            # matters is only that no entity id is pre-filled.
            default = key.default() if callable(key.default) else key.default
            assert not isinstance(default, str) or default == ""
            break
    else:
        raise AssertionError("the cover picker is missing from the flow")


async def test_every_input_in_story_section_2_has_a_picker(
    hass: HomeAssistant,
) -> None:
    """A new input must not be silently hard-coded."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    keys = {key.schema for key in result["data_schema"].schema}
    for picker_key, _default, _domains in PICKERS:
        assert picker_key in keys, picker_key


async def test_options_flow_can_re_pick(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**ENTRY_DATA, CONF_COVER_CLOSED: "binary_sensor.pool_telo_chiuso"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_COVER_CLOSED] == "binary_sensor.pool_telo_chiuso"
