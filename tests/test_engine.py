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
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.villa_pool.actuator import PDC_ACTUATION_IMPLEMENTED
from custom_components.villa_pool.const import (
    DEFAULT_ANTIFREEZE_SPEED,
    DEFAULT_CHLORINATOR_SWITCH,
    DEFAULT_FILTRATION_SPEED,
    DEFAULT_MIN_TEMP,
    DEFAULT_PDC_CLIMATE,
    DEFAULT_PUMP_RUNNING,
    DEFAULT_PUMP_SWITCH,
    DEFAULT_SOLAR_TARGET_TEMP,
    DEFAULT_TARIFF_BAND,
    DEFAULT_WATER_TEMP,
    DOMAIN,
    MODE_CLOSED,
    MODE_MANUAL,
    MODE_WINTER,
    PDC_BLOCKED,
    PDC_GRID,
    PDC_SOLAR,
)
from custom_components.villa_pool.engine import ACTUATION_IMPLEMENTED

from .conftest import ENTRY_DATA

TICK = timedelta(seconds=60)


async def setup_pool(
    hass: HomeAssistant, *, pdc_temperature: float | None = 29.0, **states
) -> MockConfigEntry:
    """Seed the input entities, then load the integration.

    `pdc_temperature` is the climate entity's `temperature` attribute — the
    machine's own target, which the aquatemp fork reports whether it is heating
    or not. 29.0 is what the owner's manual runs of 16/9 left it on. `None`
    models a climate that reports no target at all, which the supervisor must
    treat as "unknown" rather than argue with.
    """
    defaults = {
        DEFAULT_PUMP_RUNNING: "on",
        DEFAULT_PUMP_SWITCH: "on",
        "binary_sensor.pompa_piscina_problem": "off",
        "binary_sensor.pompa_piscina_flow_pressure_warning": "off",
        "sensor.pompa_piscina_volume_flow_rate": "8",
        "sensor.pompa_piscina_power": "427",
        "select.pompa_piscina_pump_mode": "Manual",
        "number.pompa_piscina_manual_percentage_power": "80",
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
    if pdc_temperature is not None:
        hass.states.async_set(
            DEFAULT_PDC_CLIMATE,
            defaults[DEFAULT_PDC_CLIMATE],
            {"temperature": pdc_temperature},
        )

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


async def go_live(hass: HomeAssistant) -> None:
    """Turn `switch.pool_dry_run` off — the owner's deliberate act."""
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.pool_dry_run"}, blocking=True
    )
    await hass.async_block_till_done()


# Our OWN settings entities. A test flipping `switch.pool_dry_run` is
# configuring the supervisor, not actuating the pool, and must not show up as a
# write.
OWN_PREFIXES = ("switch.pool_", "select.pool_mode", "number.pool_", "time.pool_")


def record_calls(hass: HomeAssistant) -> list[dict]:
    """Record every service call, without intercepting any of them.

    NOT `async_mock_service`: that REPLACES the handler for a domain's service,
    and `switch`, `number` and `select` all register their own handlers when
    this integration forwards its platforms — silently overwriting the mock. A
    "nothing was written" test built on it passes because nothing is listening,
    not because nothing was called. A bus listener cannot be overwritten, and
    it observes the real call rather than standing in for it.
    """
    calls: list[dict] = []

    @callback
    def _record(event) -> None:
        calls.append(event.data)

    hass.bus.async_listen(EVENT_CALL_SERVICE, _record)
    return calls


def _entities(call: dict) -> list[str]:
    target = (call.get("service_data") or {}).get("entity_id")
    if isinstance(target, str):
        return [target]
    return list(target or [])


def writes(calls, domain: str | None = None, service: str | None = None) -> list[dict]:
    """The recorded calls that actually moved something in the pool."""
    out = []
    for call in calls:
        if domain and call["domain"] != domain:
            continue
        if service and call["service"] != service:
            continue
        if any(e.startswith(OWN_PREFIXES) for e in _entities(call)):
            continue
        out.append(call)
    return out


def pushes(calls, tag: str) -> list:
    """Captured `notify` calls carrying one tag.

    The supervisor has several notification sources (hardware blocks, latched
    levers, antifreeze), so a bare count of pushes tells you nothing about
    which fired.
    """
    return [c for c in calls if (c.data.get("data") or {}).get("tag") == tag]


def entities_of(calls) -> list[str]:
    """The entity ids a list of recorded calls targeted, in order."""
    out: list[str] = []
    for call in calls:
        out.extend(_entities(call))
    return out


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

async def test_a_write_path_exists(hass: HomeAssistant) -> None:
    """v0.1.0 had none at all; from v0.2.0 the capability is present and the
    dry-run switch is what gates it."""
    assert ACTUATION_IMPLEMENTED is True


@pytest.mark.parametrize("domain,service", [
    ("switch", "turn_on"), ("switch", "turn_off"),
    ("climate", "set_hvac_mode"), ("climate", "set_temperature"),
    ("number", "set_value"), ("select", "select_option"),
])
async def test_dry_run_calls_nothing(
    hass: HomeAssistant, domain: str, service: str
) -> None:
    """The DEFAULT configuration still writes nothing at all.

    This is the v0.1.0 guarantee, kept: `switch.pool_dry_run` is on out of the
    box, so installing or upgrading the integration never moves the pool until
    the owner says so.
    """
    await setup_pool(
        hass,
        **{
            DEFAULT_WATER_TEMP: "24.0",          # cold: would want heating
            "sensor.solar_headroom_for_heater": "4000",   # sunny: would want SOLAR
            "input_boolean.pool_in_use": "on",   # would want pump + chlorine
            DEFAULT_PUMP_SWITCH: "off",          # and every lever disagrees
            DEFAULT_CHLORINATOR_SWITCH: "off",
            "number.pompa_piscina_manual_percentage_power": "30",
            "select.pompa_piscina_pump_mode": "AI Flow",
        },
    )
    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    calls = record_calls(hass)
    await tick(hass, times=20)
    assert writes(calls, domain, service) == []


async def test_dry_run_still_says_what_it_would_have_written(
    hass: HomeAssistant, caplog
) -> None:
    import logging
    caplog.set_level(logging.INFO)
    await setup_pool(hass, **{
        "input_boolean.pool_in_use": "on",
        DEFAULT_CHLORINATOR_SWITCH: "off",
    })
    await tick(hass, times=2)
    assert "DRY-RUN would set chlorinator" in caplog.text


async def test_going_live_is_announced_loudly(
    hass: HomeAssistant, caplog
) -> None:
    """"When did it start writing?" must be answerable from the log alone."""
    await setup_pool(hass)
    await go_live(hass)
    await tick(hass, times=2)
    assert "now WRITING to the pool" in caplog.text


# --- the pump and the chlorinator (v0.2.0) -----------------------------------

async def test_the_pdc_levers_are_wired(hass: HomeAssistant) -> None:
    """STORY §8 step 3. They were deliberately absent in v0.2.0."""
    assert PDC_ACTUATION_IMPLEMENTED is True


async def test_live_turns_the_pump_on(hass: HomeAssistant) -> None:
    await setup_pool(hass, **{DEFAULT_PUMP_SWITCH: "off"})
    calls = record_calls(hass)
    await go_live(hass)
    await tick(hass, times=2)
    assert DEFAULT_PUMP_SWITCH in entities_of(writes(calls, "switch", "turn_on"))


async def test_live_sets_the_speed_the_law_asked_for(hass: HomeAssistant) -> None:
    await setup_pool(hass, **{
        "number.pompa_piscina_manual_percentage_power": "30",
    })
    calls = record_calls(hass)
    await go_live(hass)
    await tick(hass, times=2)
    sent = writes(calls, "number", "set_value")
    assert sent
    assert sent[0]["service_data"]["value"] == 80.0


async def test_live_puts_the_pump_controller_in_manual(hass: HomeAssistant) -> None:
    """STORY §2: "Controller uses Manual only" — the percentage means nothing
    while the pump is deciding its own speed."""
    await setup_pool(hass, **{"select.pompa_piscina_pump_mode": "AI Flow"})
    calls = record_calls(hass)
    await go_live(hass)
    await tick(hass, times=2)
    sent = writes(calls, "select", "select_option")
    assert sent
    assert sent[0]["service_data"]["option"] == "Manual"


async def test_live_enables_the_chlorinator(hass: HomeAssistant) -> None:
    await setup_pool(hass)
    calls = record_calls(hass)
    await go_live(hass)
    await tick(hass, times=2)
    assert DEFAULT_CHLORINATOR_SWITCH in entities_of(
        writes(calls, "switch", "turn_on")
    )


async def test_a_pool_already_doing_the_right_thing_is_not_commanded(
    hass: HomeAssistant,
) -> None:
    """The idempotence that makes a restart cheap: everything already matches,
    so nothing is sent — no logbook entry, no relay click."""
    await setup_pool(hass, **{
        DEFAULT_PUMP_SWITCH: "on",
        DEFAULT_CHLORINATOR_SWITCH: "on",
    })
    calls = record_calls(hass)
    await go_live(hass)
    await tick(hass, times=10)
    assert writes(calls) == []


async def test_a_command_is_not_repeated_every_tick(hass: HomeAssistant) -> None:
    """The device never adopts it here (a mocked service changes no state), so
    this is the worst case — and it must still be two commands, not sixty."""
    with freeze_time("2026-09-16 12:00:00+02:00") as frozen:
        await setup_pool(hass, **{DEFAULT_PUMP_SWITCH: "off"})
        calls = record_calls(hass)
        await go_live(hass)
        await tick(hass, times=60, freezer=frozen)
    pump_calls = [
        e for e in entities_of(writes(calls, "switch")) if e == DEFAULT_PUMP_SWITCH
    ]
    assert len(pump_calls) == 2


async def test_a_manual_override_is_re_asserted_once_then_left_alone(
    hass: HomeAssistant, caplog
) -> None:
    """"Never fight a manual override, re-assert before concluding manual"."""
    with freeze_time("2026-09-16 21:30:00+02:00") as frozen:
        # Outside every window with the chlorine target met: the supervisor
        # wants both off, and the owner keeps the chlorinator on regardless.
        await setup_pool(hass, **{
            "sensor.salt_chlorinator_runtime_today": "8",
            DEFAULT_CHLORINATOR_SWITCH: "on",
        })
        calls = record_calls(hass)
        await go_live(hass)
        await tick(hass, times=30, freezer=frozen)
    chlorine_calls = [
        e for e in entities_of(writes(calls, "switch", "turn_off"))
        if e == DEFAULT_CHLORINATOR_SWITCH
    ]
    assert len(chlorine_calls) == 2
    assert "Not driving chlorinator" in caplog.text


async def test_the_latch_lifts_when_the_intent_changes(hass: HomeAssistant) -> None:
    """Having given up on a lever, the supervisor must still command it the
    moment it wants something different — otherwise one argument disables it
    until the next restart."""
    with freeze_time("2026-09-16 21:30:00+02:00") as frozen:
        await setup_pool(hass, **{
            "sensor.salt_chlorinator_runtime_today": "8",
            DEFAULT_CHLORINATOR_SWITCH: "on",
        })
        calls = record_calls(hass)
        await go_live(hass)
        await tick(hass, times=30, freezer=frozen)
        assert len([
            e for e in entities_of(writes(calls, "switch", "turn_off"))
            if e == DEFAULT_CHLORINATOR_SWITCH
        ]) == 2
        # The owner gets in the water: chlorine is wanted again.
        hass.states.async_set("input_boolean.pool_in_use", "on")
        hass.states.async_set(DEFAULT_CHLORINATOR_SWITCH, "off")
        await tick(hass, times=2, freezer=frozen)
    assert DEFAULT_CHLORINATOR_SWITCH in entities_of(
        writes(calls, "switch", "turn_on")
    )


async def test_maintenance_freezes_rather_than_switching_everything_off(
    hass: HomeAssistant,
) -> None:
    """STORY §4 says maintenance "freezes all actuation". The owner flipped it
    to work on the pool; stopping the pump under them is the opposite of what
    they asked for."""
    await setup_pool(hass, **{
        DEFAULT_PUMP_SWITCH: "off",          # every lever disagrees with intent
        DEFAULT_CHLORINATOR_SWITCH: "off",
    })
    await go_live(hass)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.pool_maintenance"}, blocking=True
    )
    calls = record_calls(hass)
    await tick(hass, times=5)
    assert writes(calls) == []


async def test_the_chlorinator_is_cut_before_the_pump(hass: HomeAssistant) -> None:
    """Stop order, STORY §5.1: the cell must never be left enabled with the
    pump already stopping."""
    with freeze_time("2026-09-16 21:30:00+02:00") as frozen:
        await setup_pool(hass, **{
            "sensor.salt_chlorinator_runtime_today": "8",
            DEFAULT_PUMP_SWITCH: "on",
            DEFAULT_CHLORINATOR_SWITCH: "on",
        })
        calls = record_calls(hass)
        await go_live(hass)
        await tick(hass, times=2, freezer=frozen)
    order = entities_of(writes(calls, "switch", "turn_off"))
    assert DEFAULT_CHLORINATOR_SWITCH in order
    assert DEFAULT_PUMP_SWITCH in order
    assert order.index(DEFAULT_CHLORINATOR_SWITCH) < order.index(DEFAULT_PUMP_SWITCH)


async def test_the_pump_is_not_stopped_under_a_running_pdc(
    hass: HomeAssistant, caplog
) -> None:
    """The hydraulic interlock, from the actuator's side.

    v0.2.0 does not drive the heat pump, so it may well be running on its own
    thermostat when our pump window closes — and in v0.3.0 it keeps reading
    `acceso` for minutes of cloud lag after we have told it to stop. Removing
    flow from a running compressor is the one mistake worth holding filtration
    hostage to avoid.
    """
    import logging
    caplog.set_level(logging.INFO)
    with freeze_time("2026-09-16 21:30:00+02:00") as frozen:
        await setup_pool(hass, **{
            "sensor.salt_chlorinator_runtime_today": "8",   # nothing owed
            DEFAULT_PUMP_SWITCH: "on",
            "binary_sensor.pool_pdc_acceso": "on",          # but it is heating
        })
        calls = record_calls(hass)
        await go_live(hass)
        await tick(hass, times=5, freezer=frozen)
    assert DEFAULT_PUMP_SWITCH not in entities_of(
        writes(calls, "switch", "turn_off")
    )
    assert "Holding off on the pump" in caplog.text
    # And the hold is visible, not merely safe: the guard never times out, so a
    # stuck `pool_pdc_acceso` would otherwise keep the pump running for ever
    # with nothing on the dashboard to say why.
    attrs = hass.states.get("sensor.pool_supervisor_reason").attributes
    assert "pump" in attrs["holding"]


async def test_the_pump_stops_once_the_pdc_has_wound_down(
    hass: HomeAssistant,
) -> None:
    """The interlock defers the stop; it does not cancel it."""
    with freeze_time("2026-09-16 21:30:00+02:00") as frozen:
        await setup_pool(hass, **{
            "sensor.salt_chlorinator_runtime_today": "8",
            DEFAULT_PUMP_SWITCH: "on",
            "binary_sensor.pool_pdc_acceso": "on",
        })
        calls = record_calls(hass)
        await go_live(hass)
        await tick(hass, times=3, freezer=frozen)
        hass.states.async_set("binary_sensor.pool_pdc_acceso", "off")
        await tick(hass, times=2, freezer=frozen)
    assert DEFAULT_PUMP_SWITCH in entities_of(writes(calls, "switch", "turn_off"))


async def test_the_speed_is_not_dropped_while_the_cell_is_still_enabled(
    hass: HomeAssistant, caplog
) -> None:
    """STORY §6: never below 80 % with the chlorinator enabled, until the
    step-down test establishes the cell's real flow-switch minimum.

    Antifreeze outside the pump window wants 30 %. The chlorinator has been
    told to stop, but the relay still reads on — so the speed waits.
    """
    import logging
    caplog.set_level(logging.INFO)
    with freeze_time("2026-09-16 21:30:00+02:00") as frozen:
        await setup_pool(hass, **{
            "sensor.gw3000a_outdoor_temperature": "-1.0",   # antifreeze
            "sensor.salt_chlorinator_runtime_today": "8",
            DEFAULT_CHLORINATOR_SWITCH: "on",               # not obeyed yet
            "number.pompa_piscina_manual_percentage_power": "80",
        })
        calls = record_calls(hass)
        await go_live(hass)
        await tick(hass, times=3, freezer=frozen)
        assert writes(calls, "number", "set_value") == []
        assert "below the 80% chlorine minimum" in caplog.text
        # The relay finally opens, and only then does the pump slow down.
        hass.states.async_set(DEFAULT_CHLORINATOR_SWITCH, "off")
        await tick(hass, times=2, freezer=frozen)
    sent = writes(calls, "number", "set_value")
    assert sent
    assert sent[0]["service_data"]["value"] == 30.0


async def test_a_latch_does_not_survive_a_trip_back_through_dry_run(
    hass: HomeAssistant,
) -> None:
    """While the supervisor was not writing, anyone could have moved anything.
    Carrying an argument across that gap would silence a lever that now needs
    driving."""
    with freeze_time("2026-09-16 21:30:00+02:00") as frozen:
        await setup_pool(hass, **{
            "sensor.salt_chlorinator_runtime_today": "8",
            DEFAULT_CHLORINATOR_SWITCH: "on",
        })
        calls = record_calls(hass)
        await go_live(hass)
        await tick(hass, times=30, freezer=frozen)
        before = len(entities_of(writes(calls, "switch", "turn_off")))
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.pool_dry_run"}, blocking=True
        )
        await tick(hass, times=2, freezer=frozen)
        await go_live(hass)
        await tick(hass, times=2, freezer=frozen)
    assert len(entities_of(writes(calls, "switch", "turn_off"))) > before


async def test_an_unreadable_lever_is_never_written_to(hass: HomeAssistant) -> None:
    """The §5.2 rule generalised: a device we cannot read is one we know
    nothing about, so we neither command it nor hold it against it."""
    await setup_pool(hass, **{
        "number.pompa_piscina_manual_percentage_power": "unavailable",
    })
    calls = record_calls(hass)
    await go_live(hass)
    await tick(hass, times=5)
    assert writes(calls, "number", "set_value") == []


async def test_a_pump_fault_notifies_the_owner(hass: HomeAssistant) -> None:
    """STORY §7.5 asks for a notification alongside the block."""
    calls = async_mock_service(hass, "notify", "mobile_app_matphone16")
    await setup_pool(hass)
    await go_live(hass)
    await tick(hass)
    hass.states.async_set("binary_sensor.pompa_piscina_problem", "on")
    await tick(hass, times=2)
    assert calls
    assert "pump problem" in calls[-1].data["message"]


async def test_a_routine_block_does_not_notify(hass: HomeAssistant) -> None:
    """"Pump not in marcia" happens every evening at 20:00. Waking the owner
    for it would train them to ignore the channel."""
    calls = async_mock_service(hass, "notify", "mobile_app_matphone16")
    await setup_pool(hass, **{DEFAULT_PUMP_RUNNING: "off"})
    await go_live(hass)
    await tick(hass, times=5)
    assert calls == []


# --- v0.3.0: the heat pump ---------------------------------------------------

def mock_climate(hass: HomeAssistant) -> None:
    """Register the climate services.

    The `climate` component is not loaded in these tests (this integration
    forwards no climate platform), so without this the calls would raise
    `ServiceNotFound` and never reach the bus — a test that then asserted "no
    climate call" would be proving nothing.
    """
    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")


async def grid_night(hass: HomeAssistant, frozen, *, pdc_temperature=29.0, **states):
    """A cold September night inside the grid window, live."""
    await setup_pool(hass, pdc_temperature=pdc_temperature, **{
        DEFAULT_WATER_TEMP: "25.6",
        DEFAULT_TARIFF_BAND: "F3",
        **states,
    })
    mock_climate(hass)
    calls = record_calls(hass)
    await go_live(hass)
    await tick(hass, times=3, freezer=frozen)
    return calls


async def test_grid_writes_the_setpoint_and_then_heat(hass: HomeAssistant) -> None:
    """§5.2: HA writes only `hvac_mode` and the setpoint — and the setpoint
    first, so the machine never starts against whatever ran last."""
    with freeze_time("2026-09-16 23:00:00+02:00") as frozen:
        calls = await grid_night(hass, frozen)
        assert hass.states.get("sensor.pool_pdc_state").state == PDC_GRID
    climate = writes(calls, "climate")
    services = [c["service"] for c in climate]
    assert services == ["set_temperature", "set_hvac_mode"]
    assert climate[0]["service_data"]["temperature"] == DEFAULT_MIN_TEMP
    assert climate[1]["service_data"]["hvac_mode"] == "heat"


async def test_the_solar_setpoint_is_the_solar_target(hass: HomeAssistant) -> None:
    with freeze_time("2026-09-16 12:00:00+02:00") as frozen:
        await setup_pool(hass, **{
            DEFAULT_WATER_TEMP: "26.0",
            "sensor.solar_headroom_for_heater": "4000",
        })
        mock_climate(hass)
        calls = record_calls(hass)
        await go_live(hass)
        await tick(hass, times=13, freezer=frozen)   # past the 10 min dwell
        assert hass.states.get("sensor.pool_pdc_state").state == PDC_SOLAR
    temps = writes(calls, "climate", "set_temperature")
    assert temps
    assert temps[0]["service_data"]["temperature"] == DEFAULT_SOLAR_TARGET_TEMP


async def test_reaching_the_target_writes_hvac_mode_off(hass: HomeAssistant) -> None:
    with freeze_time("2026-09-16 23:00:00+02:00") as frozen:
        calls = await grid_night(
            hass, frozen, pdc_temperature=DEFAULT_MIN_TEMP,
            **{DEFAULT_PDC_CLIMATE: "heat"},
        )
        # An hour of heating later the water is past min_temp + 0.5.
        frozen.tick(timedelta(hours=1))
        hass.states.async_set(DEFAULT_WATER_TEMP, "27.6")
        await tick(hass, times=2, freezer=frozen)
    modes = [c["service_data"]["hvac_mode"]
             for c in writes(calls, "climate", "set_hvac_mode")]
    assert modes[-1] == "off"


async def test_a_pump_fault_stops_the_pdc_within_one_tick(
    hass: HomeAssistant,
) -> None:
    """§7.5, now as a real command. The settle window rate-limits a REPEAT of a
    command, never a change of mind — so the safety write is not delayed by the
    15 min compressor grace that had just started."""
    with freeze_time("2026-09-16 23:00:00+02:00") as frozen:
        # The machine is already heating on the right target, so nothing has
        # been sent — and the 15 min grace is running from a command we cannot
        # see. The safety write must ignore it.
        calls = await grid_night(hass, frozen, **{
            DEFAULT_PDC_CLIMATE: "heat",
            "binary_sensor.pool_pdc_acceso": "on",
        })
        assert hass.states.get("sensor.pool_pdc_state").state == PDC_GRID
        hass.states.async_set("binary_sensor.pompa_piscina_problem", "on")
        await tick(hass, freezer=frozen)
        assert hass.states.get("sensor.pool_pdc_state").state == PDC_BLOCKED
    modes = [c["service_data"]["hvac_mode"]
             for c in writes(calls, "climate", "set_hvac_mode")]
    assert modes == ["off"]


async def test_no_climate_write_during_a_polling_gap(hass: HomeAssistant) -> None:
    """§7.9. The rule that stops a machine which never stopped being started
    twice — and the one the whole dry run was told to watch."""
    with freeze_time("2026-09-16 23:00:00+02:00") as frozen:
        await setup_pool(hass, **{
            DEFAULT_WATER_TEMP: "25.6",
            DEFAULT_TARIFF_BAND: "F3",
            DEFAULT_PDC_CLIMATE: "unavailable",
        })
        mock_climate(hass)
        calls = record_calls(hass)
        await go_live(hass)
        await tick(hass, times=6, freezer=frozen)
    assert writes(calls, "climate") == []


async def test_hvac_mode_is_not_re_commanded_inside_the_compressor_grace(
    hass: HomeAssistant,
) -> None:
    """§6: "never write `hvac_mode` more than once per MIN_ON/MIN_OFF window".

    The mocked service changes no state, so the machine never appears to adopt
    the command — the worst case, and it must still be two writes in an hour,
    not sixty.
    """
    with freeze_time("2026-09-16 23:00:00+02:00") as frozen:
        calls = await grid_night(hass, frozen)
        await tick(hass, times=60, freezer=frozen)
    assert len(writes(calls, "climate", "set_hvac_mode")) == 2


async def test_a_restart_mid_grid_does_not_re_command_the_machine(
    hass: HomeAssistant,
) -> None:
    """§7.10: "never a spurious extra start". The machine is already in `heat`
    at the right target, so the re-derived GRID state sends nothing at all."""
    with freeze_time("2026-09-17 01:00:00+02:00") as frozen:
        await setup_pool(hass, pdc_temperature=DEFAULT_MIN_TEMP, **{
            DEFAULT_WATER_TEMP: "26.0",
            DEFAULT_TARIFF_BAND: "F3",
            "binary_sensor.pool_pdc_acceso": "on",
            DEFAULT_PDC_CLIMATE: "heat",
        })
        mock_climate(hass)
        calls = record_calls(hass)
        await go_live(hass)
        await tick(hass, times=3, freezer=frozen)
        assert hass.states.get("sensor.pool_pdc_state").state == PDC_GRID
    assert writes(calls, "climate") == []


async def test_an_unreadable_setpoint_is_not_written_blind(
    hass: HomeAssistant,
) -> None:
    """A climate entity that reports no `temperature` while off tells us
    nothing about its target, so we do not argue with it. The mode still goes
    out, and the setpoint follows once the attribute appears."""
    with freeze_time("2026-09-16 23:00:00+02:00") as frozen:
        calls = await grid_night(hass, frozen, pdc_temperature=None)
        assert writes(calls, "climate", "set_temperature") == []
        hass.states.async_set(DEFAULT_PDC_CLIMATE, "heat", {"temperature": 29.0})
        await tick(hass, times=2, freezer=frozen)
    temps = writes(calls, "climate", "set_temperature")
    assert temps
    assert temps[0]["service_data"]["temperature"] == DEFAULT_MIN_TEMP


async def test_the_pdc_is_stopped_before_the_pump(hass: HomeAssistant) -> None:
    """Stop order, §5.1: `PdC OFF -> post-run -> chlorine OFF -> pump OFF`.

    Within one tick the only part that can be got wrong is asking the pump to
    stop before the heat pump, and that is the part that costs a compressor.
    """
    recorded: list[str] = []

    @callback
    def _seen(event) -> None:
        data = event.data
        if data["domain"] == "climate" and data["service"] == "set_hvac_mode":
            recorded.append("pdc")
        if data["domain"] == "switch" and data["service"] == "turn_off":
            if DEFAULT_PUMP_SWITCH in _entities(data):
                recorded.append("pump")

    with freeze_time("2026-09-16 23:00:00+02:00") as frozen:
        await setup_pool(hass, **{
            DEFAULT_WATER_TEMP: "25.6",
            DEFAULT_TARIFF_BAND: "F3",
            "sensor.salt_chlorinator_runtime_today": "8",
            DEFAULT_PDC_CLIMATE: "heat",
        })
        mock_climate(hass)
        hass.bus.async_listen(EVENT_CALL_SERVICE, _seen)
        await go_live(hass)
        await tick(hass, times=3, freezer=frozen)
        # Grid heating is switched off: the PdC must stop, and only then may
        # the pump follow.
        await hass.services.async_call(
            "switch", "turn_off",
            {"entity_id": "switch.pool_grid_heating"}, blocking=True,
        )
        hass.states.async_set("binary_sensor.pool_pdc_acceso", "off")
        await tick(hass, times=12, freezer=frozen)
    assert "pdc" in recorded
    assert "pump" in recorded
    assert recorded.index("pdc") < recorded.index("pump")


async def test_the_writes_are_visible_on_the_reason_sensor(
    hass: HomeAssistant,
) -> None:
    await setup_pool(hass, **{DEFAULT_PUMP_SWITCH: "off"})
    await go_live(hass)
    await tick(hass, times=2)
    attrs = hass.states.get("sensor.pool_supervisor_reason").attributes
    assert attrs["dry_run"] is False
    assert attrs["writes"] >= 1
    assert attrs["last_write"]


# --- v0.4.0: winter and antifreeze -------------------------------------------

async def set_mode(hass: HomeAssistant, mode: str) -> None:
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.pool_mode", "option": mode}, blocking=True,
    )
    await hass.async_block_till_done()


async def test_the_antifreeze_sensor_exists(hass: HomeAssistant) -> None:
    """A new entity id, so it is a contract from here on."""
    await setup_pool(hass)
    assert hass.states.get("binary_sensor.pool_antifreeze") is not None


async def test_the_winter_noon_slot_drives_the_pump(hass: HomeAssistant) -> None:
    """§3: winter runs the pump from 12:00 for `winter_hours` at 80 %."""
    with freeze_time("2026-12-15 12:30:00+01:00") as frozen:
        await setup_pool(hass, **{
            DEFAULT_PUMP_SWITCH: "off",
            "sensor.gw3000a_outdoor_temperature": "5.0",
            DEFAULT_WATER_TEMP: "8.0",
        })
        calls = record_calls(hass)
        await set_mode(hass, MODE_WINTER)
        await go_live(hass)
        await tick(hass, times=2, freezer=frozen)
    assert DEFAULT_PUMP_SWITCH in entities_of(writes(calls, "switch", "turn_on"))
    speeds = [c["service_data"]["value"] for c in writes(calls, "number", "set_value")]
    assert speeds == [] or speeds[0] == float(DEFAULT_FILTRATION_SPEED)


async def test_winter_stops_the_heat_pump(hass: HomeAssistant) -> None:
    """§5.2: winter is in `PDC_BLOCKED_MODES`, and from v0.3.0 that is a real
    `off` command rather than only a state."""
    with freeze_time("2026-12-15 12:30:00+01:00") as frozen:
        await setup_pool(hass, pdc_temperature=DEFAULT_MIN_TEMP, **{
            "sensor.gw3000a_outdoor_temperature": "5.0",
            DEFAULT_WATER_TEMP: "8.0",
            DEFAULT_PDC_CLIMATE: "heat",
        })
        mock_climate(hass)
        calls = record_calls(hass)
        await set_mode(hass, MODE_WINTER)
        await go_live(hass)
        await tick(hass, times=2, freezer=frozen)
        assert hass.states.get("sensor.pool_pdc_state").state == PDC_BLOCKED
    modes = [c["service_data"]["hvac_mode"]
             for c in writes(calls, "climate", "set_hvac_mode")]
    assert modes == ["off"]


async def test_antifreeze_cuts_the_cell_before_it_slows_the_pump(
    hass: HomeAssistant,
) -> None:
    """§6 in the antifreeze path: the cell's flow-switch minimum is unknown, so
    30 % must wait until `switch.clorinatore` genuinely reads off — not merely
    until it has been told to."""
    with freeze_time("2026-12-15 03:00:00+01:00") as frozen:
        await setup_pool(hass, **{
            "sensor.gw3000a_outdoor_temperature": "-1.0",
            DEFAULT_WATER_TEMP: "8.0",
            DEFAULT_CHLORINATOR_SWITCH: "on",
            "number.pompa_piscina_manual_percentage_power": "80",
        })
        calls = record_calls(hass)
        await set_mode(hass, MODE_WINTER)
        await go_live(hass)
        await tick(hass, times=2, freezer=frozen)
        assert DEFAULT_CHLORINATOR_SWITCH in entities_of(
            writes(calls, "switch", "turn_off")
        )
        assert writes(calls, "number", "set_value") == []
        # The relay opens, and only then does the pump slow down.
        hass.states.async_set(DEFAULT_CHLORINATOR_SWITCH, "off")
        await tick(hass, times=2, freezer=frozen)
    sent = writes(calls, "number", "set_value")
    assert sent
    assert sent[0]["service_data"]["value"] == float(DEFAULT_ANTIFREEZE_SPEED)


@pytest.mark.parametrize("mode", [MODE_MANUAL, MODE_CLOSED])
async def test_antifreeze_drives_the_pump_even_in_a_frozen_mode(
    hass: HomeAssistant, mode: str
) -> None:
    """Owner amendment 2026-09-17. `closed` is the mode the pool spends the
    whole winter in, unattended — which is exactly when the pipes are at
    risk."""
    with freeze_time("2026-12-15 03:00:00+01:00") as frozen:
        await setup_pool(hass, **{
            "sensor.gw3000a_outdoor_temperature": "-1.0",
            DEFAULT_WATER_TEMP: "8.0",
            DEFAULT_PUMP_SWITCH: "off",
        })
        calls = record_calls(hass)
        await set_mode(hass, mode)
        await go_live(hass)
        await tick(hass, times=2, freezer=frozen)
    assert DEFAULT_PUMP_SWITCH in entities_of(writes(calls, "switch", "turn_on"))
    assert hass.states.get("binary_sensor.pool_antifreeze").state == "on"


@pytest.mark.parametrize("mode", [MODE_MANUAL, MODE_CLOSED])
async def test_a_frozen_mode_on_a_mild_night_still_drives_nothing(
    hass: HomeAssistant, mode: str
) -> None:
    """The override is antifreeze, not a general licence to actuate."""
    with freeze_time("2026-12-15 03:00:00+01:00") as frozen:
        await setup_pool(hass, **{
            "sensor.gw3000a_outdoor_temperature": "12.0",
            DEFAULT_WATER_TEMP: "8.0",
            DEFAULT_PUMP_SWITCH: "off",
            DEFAULT_CHLORINATOR_SWITCH: "on",
        })
        calls = record_calls(hass)
        await set_mode(hass, mode)
        await go_live(hass)
        await tick(hass, times=3, freezer=frozen)
    assert writes(calls) == []


async def test_maintenance_still_freezes_through_a_freeze(
    hass: HomeAssistant,
) -> None:
    """The one case the amendment deliberately left alone: someone is at the
    pool, possibly with it drained."""
    with freeze_time("2026-12-15 03:00:00+01:00") as frozen:
        await setup_pool(hass, **{
            "sensor.gw3000a_outdoor_temperature": "-1.0",
            DEFAULT_WATER_TEMP: "8.0",
            DEFAULT_PUMP_SWITCH: "off",
        })
        await go_live(hass)
        await hass.services.async_call(
            "switch", "turn_on",
            {"entity_id": "switch.pool_maintenance"}, blocking=True,
        )
        calls = record_calls(hass)
        await tick(hass, times=3, freezer=frozen)
    assert writes(calls) == []
    # Engaged, and deliberately ignored — the sensor must still say so.
    assert hass.states.get("binary_sensor.pool_antifreeze").state == "on"


async def test_antifreeze_notifies_on_both_edges(hass: HomeAssistant) -> None:
    calls = async_mock_service(hass, "notify", "mobile_app_matphone16")
    with freeze_time("2026-12-15 03:00:00+01:00") as frozen:
        await setup_pool(hass, **{
            "sensor.gw3000a_outdoor_temperature": "12.0",
            DEFAULT_WATER_TEMP: "8.0",
        })
        await set_mode(hass, MODE_WINTER)
        await go_live(hass)
        await tick(hass, times=2, freezer=frozen)
        assert pushes(calls, "pool_antifreeze") == []   # a mild night is no event
        hass.states.async_set("sensor.gw3000a_outdoor_temperature", "-1.0")
        await tick(hass, times=2, freezer=frozen)
        sent = pushes(calls, "pool_antifreeze")
        assert len(sent) == 1
        assert "antifreeze engaged" in sent[0].data["title"].lower()
        hass.states.async_set("sensor.gw3000a_outdoor_temperature", "3.0")
        await tick(hass, times=2, freezer=frozen)
    sent = pushes(calls, "pool_antifreeze")
    assert len(sent) == 2
    assert "released" in sent[1].data["title"].lower()


async def test_antifreeze_does_not_notify_once_per_tick(
    hass: HomeAssistant,
) -> None:
    """Edge-triggered, like the fault blocks. A freeze lasts days."""
    calls = async_mock_service(hass, "notify", "mobile_app_matphone16")
    with freeze_time("2026-12-15 03:00:00+01:00") as frozen:
        await setup_pool(hass, **{
            "sensor.gw3000a_outdoor_temperature": "-5.0",
            DEFAULT_WATER_TEMP: "8.0",
        })
        await set_mode(hass, MODE_WINTER)
        await go_live(hass)
        await tick(hass, times=30, freezer=frozen)
    assert len(pushes(calls, "pool_antifreeze")) == 1


async def test_a_restart_inside_the_hysteresis_band_keeps_protecting(
    hass: HomeAssistant,
) -> None:
    """The latch is history a fresh start does not have. At +1 °C, inside the
    0..+2 band, re-deriving from the ENGAGE threshold would stop the pump in
    the middle of a cold snap."""
    with freeze_time("2026-12-15 03:00:00+01:00") as frozen:
        await setup_pool(hass, **{
            "sensor.gw3000a_outdoor_temperature": "1.0",
            DEFAULT_WATER_TEMP: "8.0",
            DEFAULT_PUMP_SWITCH: "off",
        })
        calls = record_calls(hass)
        await set_mode(hass, MODE_WINTER)
        await go_live(hass)
        await tick(hass, times=2, freezer=frozen)
    assert hass.states.get("binary_sensor.pool_antifreeze").state == "on"
    assert DEFAULT_PUMP_SWITCH in entities_of(writes(calls, "switch", "turn_on"))


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
