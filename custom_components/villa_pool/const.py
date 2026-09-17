"""Constants for Villa Pool.

Two kinds of thing live here:

1. The **verified entity inventory** (STORY §2, read live on HA 2026.8.3
   2026-09-16 17:25). These are only the *defaults* the config flow pre-fills —
   the entity actually used at runtime always comes from the config entry, so a
   renamed entity is fixed in the UI, never here.
2. The **owner's fixed decisions** (STORY §3, taken 2026-09-16) and the control
   constants derived from them. §3 is closed: do not reopen T_min 27.0, grid
   heating allowed, never in F2, 80 % filtration, antifreeze 30 % tunable, or
   COP 2.8 for estimates.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "villa_pool"

PLATFORMS: Final = [
    "binary_sensor",
    "number",
    "select",
    "sensor",
    "switch",
    "time",
]

# --- Engine cadence ----------------------------------------------------------
# STORY §5: tick every 60 s. The owner's PV data is 5-minute; nothing in this
# control law needs faster, and the PdC is cloud-polled at ~5 min anyway.
UPDATE_INTERVAL: Final = timedelta(seconds=60)


# --- Config-entry keys for the entity pickers (STORY §2) ---------------------
# One key per input the integration reads. The config flow shows an entity
# selector for each, pre-filled with the verified default below.
CONF_PUMP_SWITCH: Final = "pump_switch"
CONF_PUMP_MODE: Final = "pump_mode_select"
CONF_PUMP_SPEED: Final = "pump_speed_number"
CONF_PUMP_FLOW: Final = "pump_flow_sensor"
CONF_PUMP_POWER: Final = "pump_power_sensor"
CONF_PUMP_PROBLEM: Final = "pump_problem"
CONF_PUMP_FLOW_WARNING: Final = "pump_flow_pressure_warning"
CONF_PUMP_RUNNING: Final = "pump_running"
CONF_PUMP_HOURS_24H: Final = "pump_hours_24h"
CONF_WORKDAY: Final = "workday_sensor"
CONF_PDC_CLIMATE: Final = "pdc_climate"
CONF_PDC_RUNNING: Final = "pdc_running"
CONF_PDC_FAULT: Final = "pdc_fault"
CONF_PDC_POWER: Final = "pdc_power_sensor"
CONF_PDC_ENERGY: Final = "pdc_energy_sensor"
CONF_PDC_AIR_TEMP: Final = "pdc_air_temp_sensor"
CONF_CHLORINATOR_SWITCH: Final = "chlorinator_switch"
CONF_CHLORINATOR_RUNNING: Final = "chlorinator_running"
CONF_CHLORINATOR_HOURS: Final = "chlorinator_hours_today"
CONF_WATER_TEMP: Final = "water_temp_sensor"
CONF_OUTDOOR_TEMP: Final = "outdoor_temp_sensor"
CONF_SOLAR_HEADROOM: Final = "solar_headroom_sensor"
CONF_GRID_POWER: Final = "grid_power_sensor"
CONF_TARIFF_BAND: Final = "tariff_band_sensor"
CONF_COVER_CLOSED: Final = "cover_closed"
CONF_POOL_IN_USE: Final = "pool_in_use"
CONF_NOTIFY_TARGET: Final = "notify_target"

# --- Verified defaults (STORY §2) --------------------------------------------
# Pump on/off: the template helper wrapping valve.pompa_piscina_water.
# NEVER the valve (STORY §2) — the switch is the supported lever.
DEFAULT_PUMP_SWITCH: Final = "switch.pompa_piscina"
DEFAULT_PUMP_MODE: Final = "select.pompa_piscina_pump_mode"
DEFAULT_PUMP_SPEED: Final = "number.pompa_piscina_manual_percentage_power"
DEFAULT_PUMP_FLOW: Final = "sensor.pompa_piscina_volume_flow_rate"
DEFAULT_PUMP_POWER: Final = "sensor.pompa_piscina_power"
DEFAULT_PUMP_PROBLEM: Final = "binary_sensor.pompa_piscina_problem"
DEFAULT_PUMP_FLOW_WARNING: Final = "binary_sensor.pompa_piscina_flow_pressure_warning"
# The ONE truth for "the pump is moving water" (Phase 0, STORY §1/§6).
DEFAULT_PUMP_RUNNING: Final = "binary_sensor.pool_pompa_in_marcia"
DEFAULT_PUMP_HOURS_24H: Final = "sensor.pool_pompa_ore_ultime_24h"
DEFAULT_WORKDAY: Final = "binary_sensor.workday_sensor_it"
DEFAULT_PDC_CLIMATE: Final = "climate.pool_pdc_piscina"
# NOT binary_sensor.pool_pdc_online — that reads off while running (STORY §2).
DEFAULT_PDC_RUNNING: Final = "binary_sensor.pool_pdc_acceso"
DEFAULT_PDC_FAULT: Final = "binary_sensor.pool_pdc_guasto"
DEFAULT_PDC_POWER: Final = "sensor.shellypro3em63_a4f00fcd881c_phase_a_power"
DEFAULT_PDC_ENERGY: Final = "sensor.shellypro3em63_a4f00fcd881c_phase_a_energy"
# The air the evaporator actually sees; falls back to the Ecowitt outdoor probe.
DEFAULT_PDC_AIR_TEMP: Final = "sensor.pool_pdc_temp_ambiente"
DEFAULT_CHLORINATOR_SWITCH: Final = "switch.clorinatore"
# Source of truth for "the cell is producing" — the switch state is not evidence.
DEFAULT_CHLORINATOR_RUNNING: Final = "binary_sensor.salt_chlorinator_running"
DEFAULT_CHLORINATOR_HOURS: Final = "sensor.salt_chlorinator_runtime_today"
DEFAULT_WATER_TEMP: Final = "sensor.gw3000a_soil_temperature_1"
DEFAULT_OUTDOOR_TEMP: Final = "sensor.gw3000a_outdoor_temperature"
# Already excludes the PdC, so it does not collapse when the PdC starts (§2).
DEFAULT_SOLAR_HEADROOM: Final = "sensor.solar_headroom_for_heater"
DEFAULT_GRID_POWER: Final = (
    "sensor.energy_grid_grid_consumption_power_grid_injection_power_net_power"
)
DEFAULT_TARIFF_BAND: Final = "sensor.fascia_oraria"
DEFAULT_POOL_IN_USE: Final = "input_boolean.pool_in_use"
DEFAULT_NOTIFY_TARGET: Final = "notify.mobile_app_matphone16"
# The cover sensor does NOT exist yet (owner installs 20-21/9, id TBD — STORY
# §1/§2: "ask, don't guess"). Its picker is optional and defaults to empty; the
# cover rules stay inert until the owner supplies the real entity id.
DEFAULT_COVER_CLOSED: Final = ""


# --- Owner decisions, §3 (FIXED — do not reopen) ------------------------------
# Guaranteed minimum water temperature and its hysteresis.
DEFAULT_MIN_TEMP: Final = 27.0
MIN_TEMP_HYSTERESIS: Final = 0.5          # stop at 27.5
DEFAULT_SOLAR_TARGET_TEMP: Final = 28.0

# Solar headroom thresholds (pool first; no battery-SOC gate).
DEFAULT_SOLAR_ON_W: Final = 3000.0
DEFAULT_SOLAR_OFF_W: Final = 2500.0

# Pump speeds. 80 % is the calibrated bypass working point; the chlorinator's
# cell flow-switch minimum is UNKNOWN, hence the guardrail in §6.
DEFAULT_FILTRATION_SPEED: Final = 80
DEFAULT_PDC_SPEED: Final = 80
DEFAULT_ANTIFREEZE_SPEED: Final = 30      # tunable 30-80 (§3)
ANTIFREEZE_SPEED_MIN: Final = 30
ANTIFREEZE_SPEED_MAX: Final = 80
PUMP_SPEED_MIN: Final = 30
PUMP_SPEED_MAX: Final = 120
PUMP_SPEED_STEP: Final = 5
# Below this speed the chlorinator must never be enabled (§6) until the
# step-down test has established the cell's real flow-switch minimum.
CHLORINE_MIN_PUMP_SPEED: Final = 80

# Antifreeze band (§3): engage below 0 °C, release at +2 °C.
DEFAULT_ANTIFREEZE_ON_C: Final = 0.0
DEFAULT_ANTIFREEZE_OFF_C: Final = 2.0

# Targets.
DEFAULT_TARGET_TURNOVERS: Final = 1.0
DEFAULT_TARGET_CHLORINE_HOURS: Final = 8.0
DEFAULT_WINTER_CHLORINE_HOURS: Final = 2.0
DEFAULT_COVER_CHLORINE_FACTOR: Final = 0.5
DEFAULT_WINTER_HOURS: Final = 2.0
# Conservative side of the documented 67-81 m3 range (§3); turnovers = m3 / this.
POOL_VOLUME_M3: Final = 90.0

# The tariff band grid heating is never allowed in (§3: F1 ~ F3, F2 expensive).
# Reason on BANDS, never on prices — the PUN index changes monthly.
BAND_F1: Final = "F1"
BAND_F2: Final = "F2"
BAND_F3: Final = "F3"
GRID_FORBIDDEN_BANDS: Final = (BAND_F2,)

# Energy component per band, giu-26 (Sorgenia, PUN-indexed). DIAGNOSTIC ONLY —
# exposed as settings so the estimate can be refreshed without a release.
DEFAULT_PRICE_F1: Final = 0.164
DEFAULT_PRICE_F2: Final = 0.193
DEFAULT_PRICE_F3: Final = 0.166

# --- COP model (§5.4) ---------------------------------------------------------
# ONE measured point: COP 2.82 at ~18 °C air (night 16->17/9, 95 Hz, covered).
# The slope is a Carnot-scaled guess and is LOW confidence — estimates only.
COP_REF: Final = 2.82
COP_T_REF: Final = 18.0
COP_SLOPE: Final = 0.10        # per °C
COP_MIN: Final = 1.5
COP_MAX: Final = 5.5

# --- Timers (§5.1, §5.2) ------------------------------------------------------
# Confirmation that the pump is really moving water. pool_pompa_in_marcia has
# NO built-in delay_on/delay_off (§1) — every consumer debounces itself.
PUMP_CONFIRM_S: Final = 60
# Pump keeps running after the PdC stops, to carry the residual heat away.
POSTRUN_S: Final = 300                  # 5 min
# Compressor protection.
PDC_MIN_ON_S: Final = 1800              # 30 min
PDC_MIN_OFF_S: Final = 900              # 15 min
# Solar dwell: headroom must hold before starting, and must fail for longer
# before stopping (a passing cloud must not cut a run).
SOLAR_ON_DWELL_S: Final = 600           # 10 min
SOLAR_OFF_DWELL_S: Final = 900          # 15 min
# A cloud-polled PdC flickers unavailable/unknown between polls: treat that as
# "no new information", never as a state change, and judge nothing about the
# machine for this long after a command (§5.2 — T02 lagged ~1 h once).
PDC_STALE_GRACE_S: Final = 900          # 15 min
# Chlorine is cut off if the cover has been closed longer than this (§5.3).
COVER_CLOSED_CHLORINE_CUTOFF_H: Final = 24.0
# switch.pool_maintenance freezes all actuation and auto-releases after this.
MAINTENANCE_AUTO_OFF_S: Final = 4 * 3600

# --- Heating-session log (§5.4) ----------------------------------------------
# Water: 1 m3 lifted by 1 K costs 1.163 kWh. The owner's own night measurement
# is 90 m3 x 1.163 x 0.5 K = 52.3 kWh_th against ~18.6 kWh_el = COP 2.82, which
# is the single point the whole COP model rests on — so a session logged with
# this constant is directly comparable with it.
KWH_PER_M3_K: Final = 1.163
# Below these a session is logged but no COP is computed: the probe reads to
# 0.1 °C, so a smaller rise is mostly quantisation, and a short run has not had
# time to show one at all.
SESSION_MIN_MINUTES: Final = 20.0
SESSION_MIN_DELTA_T: Final = 0.2

# --- Daytime grid top-up (§5.4, owner confirmed 2026-09-17) -------------------
# GRID inside the SOLAR window when the sun is not there. Default ON, as §5.4
# specified for the case the owner confirmed it. The F2 veto still applies and
# is what keeps Saturday daytime out.
DEFAULT_GRID_DAY_TOPUP: Final = True

# --- Actuation (§8 steps 2-3) -------------------------------------------------
# How long a lever is given to adopt a command before the supervisor judges it.
# The local levers answer in seconds (tuya-local bridge, Shelly relay), so 2 min
# is already generous. The PdC is cloud-polled at ~5 min and §5.2 says to judge
# nothing about it for 10-15 min after a command, so it gets the full grace —
# which is also what enforces §6's "never write hvac_mode more than once per
# MIN_ON/MIN_OFF window", MIN_OFF being 15 min.
SETTLE_LOCAL_S: Final = 120
# The PdC's hvac_mode is the one that cycles a compressor, so it gets the full
# grace — which is also what enforces §6's "never write hvac_mode more than once
# per MIN_ON/MIN_OFF window", MIN_OFF being 15 min.
SETTLE_PDC_MODE_S: Final = PDC_STALE_GRACE_S
# The setpoint cycles nothing, and a machine that ignored it (some controllers
# refuse a target while off) should not be left on the wrong one for a quarter
# of an hour. 10 min is the bottom of §5.2's own "judge nothing for 10-15 min"
# range and two cloud-poll intervals, so a re-read is genuinely post-command.
SETTLE_PDC_SETPOINT_S: Final = 600
# A single write may not hold the engine's lock longer than this. A cloud lever
# that never answers would otherwise make the supervisor deaf for every tick
# after it, which is a far worse failure than one missed command.
WRITE_TIMEOUT_S: Final = 10
# Commands one intent is worth before the supervisor stops driving that lever
# (§8 step 2: "never fight a manual override, re-assert before concluding
# manual"). One command, then one re-assert, then hands off.
WRITE_ATTEMPTS: Final = 2
# The pump's own controller has to be in Manual for the percentage to mean
# anything at all (STORY §2: "Controller uses Manual only").
PUMP_MODE_MANUAL: Final = "Manual"
# Read-back slop. A setpoint written as 27.0 may come back as 27, and a speed
# written as 80 as 80.0; neither is a disagreement worth a second command.
SETPOINT_TOLERANCE: Final = 0.2
PUMP_SPEED_TOLERANCE: Final = 0.5
# Blocks worth waking the owner for. A mode change or maintenance is the owner's
# own doing and "pump not in marcia" is routine; these two are hardware saying
# something is wrong (STORY §7.5).
NOTIFIABLE_BLOCKS: Final = ("PdC fault", "pump problem")

# --- Windows (§3: same every day -> time entities, not schedules) ------------
DEFAULT_PUMP_START: Final = "08:00"
DEFAULT_PUMP_END: Final = "20:00"
DEFAULT_PDC_SOLAR_START: Final = "10:00"
DEFAULT_PDC_SOLAR_END: Final = "18:00"
DEFAULT_PDC_GRID_START: Final = "23:00"   # crosses midnight
DEFAULT_PDC_GRID_END: Final = "07:00"
DEFAULT_CHLORINE_START: Final = "09:00"
DEFAULT_CHLORINE_END: Final = "21:00"
# The hour by which the daily targets must be met; after it, catch-up may
# extend the pump (§4).
DEFAULT_DEADLINE: Final = "19:00"
DEFAULT_WINTER_START: Final = "12:00"

# --- Modes (§4) ---------------------------------------------------------------
MODE_AUTO: Final = "auto"
MODE_FILTRATION_ONLY: Final = "filtration_only"
MODE_WINTER: Final = "winter"
MODE_MANUAL: Final = "manual"
MODE_CLOSED: Final = "closed"
POOL_MODES: Final = [
    MODE_AUTO,
    MODE_FILTRATION_ONLY,
    MODE_WINTER,
    MODE_MANUAL,
    MODE_CLOSED,
]
# Modes in which the PdC is unconditionally BLOCKED (§5.2).
PDC_BLOCKED_MODES: Final = (MODE_MANUAL, MODE_CLOSED, MODE_WINTER)

# --- PdC states (§5.2) --------------------------------------------------------
PDC_OFF: Final = "off"
PDC_SOLAR: Final = "solar"
PDC_GRID: Final = "grid"
PDC_BLOCKED: Final = "blocked"
PDC_STATES: Final = (PDC_OFF, PDC_SOLAR, PDC_GRID, PDC_BLOCKED)

# --- Pump demand requesters (§5.1), for the reason line ----------------------
REQ_WINDOW: Final = "window"
REQ_PDC: Final = "pdc"
REQ_CHLORINE: Final = "chlorine"
REQ_IN_USE: Final = "pool_in_use"
REQ_ANTIFREEZE: Final = "antifreeze"
REQ_CATCHUP: Final = "catchup"
REQ_POSTRUN: Final = "postrun"
REQ_WINTER: Final = "winter"
