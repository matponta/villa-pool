# STORY — Pool Controller (`villa_pool`) — brief for the coding session

Owner: Mattia. Written 2026-09-17 from the design doc *Pool Controller v2.2*
(`Home Assistant/Claude outputs/pool-controller-proposta-2026-09-16.html`, also
published as an Artifact). Every decision below was taken by the owner on
2026-09-16; do not reopen them. Everything else is engineering.

## 0. What to build, in one paragraph

A **new HACS custom integration `villa_pool`** (own repo `villa-pool`, same
skeleton and conventions as `villa-hvac`: config-flow hub, one engine tick,
pure control-law modules under `supervisor/`, settings exposed as the
integration's own entities with restore, pytest + freeze_time, STORY/CLAUDE.md
discipline, pre-tag adversarial review). It supervises the pool's pump, heat
pump (PdC) and salt chlorinator as one hydraulic system: **windows** (when
things are *allowed*), **daily targets** (what must be *achieved*) and
**interlocks** (what can never be violated), with the pump "following demand".
It does NOT replace the devices' own controllers: the PdC keeps its thermostat
(HA writes only `hvac_mode` + setpoint), the UNIKO chlorinator keeps its cycles
(HA only *enables* it).

Why a separate integration and not `villa_hvac`: different physical domain,
different failure modes, different release cadence; `villa_hvac` is live at
v0.70.0 with its own open backlog. Reuse its skeleton, share nothing at runtime.

Why not plain HA automations: a 4-state machine with hysteresis, minimum
on/off times, priorities and daily catch-up is untestable in YAML; the owner
already chose Python + tests for the HVAC supervisor.

## 1. Phase 0 — HA-native prerequisites (DONE 2026-09-17 08:50 via MCP)

These are HA config objects (UI helpers / automations), not integration code.
All verified live after creation.

- `binary_sensor.pool_pompa_in_marcia` (template helper, entry
  `01M2Q22H1WBGWW4C3BS6ZYJXNC`, area pool, device_class running) — ON when
  `switch.pompa_piscina` is on AND `sensor.pompa_piscina_volume_flow_rate` ≥
  `input_number.pool_portata_minima` AND `binary_sensor.pompa_piscina_problem`
  is off. **Fallback**: if the tuya-local switch or flow is `unavailable`/
  `unknown`, it falls back to phase C > 300 W (valid only while the pump runs
  at 80 %). **No delay_on/delay_off** — the UI template flow has no such
  field, so every consumer applies its own debounce (cutoff `for: 2 min`;
  the integration's 60 s confirmation is its own job).
- `input_number.pool_portata_minima` = 1 m³/h (0–10, step 1, area pool).
- `sensor.pool_pump_running_num` (template entry `01KZNH0A5ANRG1C1CJR77D2D2V`)
  re-anchored: `1 if pool_pompa_in_marcia on else 0`. Feeds the Riemann
  `sensor.pool_pump_runtime_total` → 30-day chart unchanged.
- `sensor.pool_pompa_ore_ultime_24h` (history_stats, entry
  `01M2Q23NFBRDN1XBJPPP23YSMC`): hours ON of `pool_pompa_in_marcia` in the
  trailing 24 h, recorder-backed (restart-proof).
- `automation.pool_allerta_pompa_ferma_da_24h` re-anchored: numeric_state
  `sensor.pool_pompa_ore_ultime_24h` below 0.05. The old
  `sensor.pool_pump_max_power_24h` (statistics on phase C) is now unused —
  left in place, safe to delete.
- `automation.pool_chlorinator_safety_cutoff_on_low_circulation` re-anchored
  AND re-enabled: trigger `pool_pompa_in_marcia` → `off` for 2 min, condition
  `switch.clorinatore` on → switch off + persistent notification
  (`pool_clorinatore_cutoff`) + push matphone16. Stale "300 W / 3 min" text
  fixed.
- Workday integration added (entry `01M2Q258X6Y20NMKQ8BYR8934M`, country IT,
  workdays Mon–Sat, excludes Sun + holidays, language it_IT) →
  `binary_sensor.workday_sensor_it` (off = Sunday or Italian holiday) and
  `calendar.workday_sensor_it_calendar`.
- `sensor.fascia_oraria` (template helper, entry `01M2Q26MMVTDY0P7QGR862V402`,
  renamed from `sensor.fascia_oraria_energia`): `F1|F2|F3` on the ARERA
  calendar — Mon–Fri F3 00–07 · F2 07–08 · F1 08–19 · F2 19–23 · F3 23–24;
  Sat F3 00–07 · F2 07–23 · F3 23–24; Sun/holidays F3. Verified `F1` at
  Thu 08:52.

NOT done (waits for the cover sensor, owner installs 20–21/9):
- `automation.pool_allerta_telo_aperto_chiudi_casa` — trigger
  `input_button.chiudi_notte` pressed; condition `binary_sensor.pool_telo_chiuso`
  not `on`; action push `notify.mobile_app_matphone16`, `data.entity_id:
  camera.g6_bbq_high_resolution_channel` (iOS camera attachment),
  `push.interruption-level: time-sensitive`; repeat once after 20 min if still
  open; distinct message when the sensor is `unavailable`. Final entity id
  unknown at brief time: **ask, don't guess**.

The integration consumes `pool_pompa_in_marcia`, `fascia_oraria` and
`pool_telo_chiuso` as inputs; it does not re-implement them.

**Measured overnight (16→17/9, `automation.pool_test_cop_notturno`, 23:01–05:00,
pool covered, pump 80 %, compressor 95 Hz, air ~18 °C):** COP **2.82**,
8.75 kW thermal (52.5 kWh_th / ~18.6 kWh_el in 6 h), water 26.2 → 26.7 °C in
the test window (27.2 by 08:50 — the PdC was left running on its own
thermostat, setpoint 29, still drawing 3.1 kW at 08:55 = 31 kWh since 23:01).
Use COP ≈ 2.8 (not 4) for night-time GRID estimates and the outdoor-air model
in §5.4 for everything else.

## 2. Verified entity inventory (HA 2026.8.3, read 2026-09-16 17:25)

Inputs the integration reads (all exist unless marked):

| Role | Entity | Notes |
|---|---|---|
| Pump on/off | `switch.pompa_piscina` | Template helper wrapping `valve.pompa_piscina_water`. Use the switch, never the valve. |
| Pump mode | `select.pompa_piscina_pump_mode` | `Manual` / `AI Flow` / `Boost`. Controller uses Manual only. |
| Pump speed | `number.pompa_piscina_manual_percentage_power` | 30–120 %, step 5. |
| Pump flow | `sensor.pompa_piscina_volume_flow_rate` | m³/h, resolution 1. 8 at 80 %. |
| Pump power | `sensor.pompa_piscina_power` | W. 427 at 80 %. |
| Pump faults | `binary_sensor.pompa_piscina_problem`, `binary_sensor.pompa_piscina_flow_pressure_warning` | attr `fault_code`. |
| Pump running (truth) | `binary_sensor.pool_pompa_in_marcia` | Phase 0, live. No built-in debounce — confirm 60 s in the integration. |
| Pump hours last 24 h | `sensor.pool_pompa_ore_ultime_24h` | Phase 0, history_stats. Diagnostics. |
| Holiday/Sunday | `binary_sensor.workday_sensor_it` | Phase 0. off = Sunday or Italian holiday. |
| PdC | `climate.pool_pdc_piscina` | hvac_modes `off/cool/heat/auto`, 15–35 °C. Cloud polling ~5 min, entities flicker `unavailable`/`unknown` between polls — treat as "no change", never as "off". |
| PdC really running | `binary_sensor.pool_pdc_acceso` | Do NOT use `binary_sensor.pool_pdc_online` (reads off while running). |
| PdC fault | `binary_sensor.pool_pdc_guasto` | |
| PdC electrical power | `sensor.shellypro3em63_a4f00fcd881c_phase_a_power` | W, ~3.1 kW at 95 Hz. Also `binary_sensor.pool_heater_running` (> 1 kW, 30 s delay_off). |
| PdC energy | `sensor.shellypro3em63_a4f00fcd881c_phase_a_energy` | kWh, PdC only (enabled 16/9). |
| Chlorinator enable | `switch.clorinatore` | Shelly relay. **Enable only** — the UNIKO decides when the cell produces. |
| Chlorinator really producing | `binary_sensor.salt_chlorinator_running` | Phase C power/stdev template. Source of truth. |
| Chlorinator hours today | `sensor.salt_chlorinator_runtime_today` | h, history_stats, resets midnight. |
| Pool water temp | `sensor.gw3000a_soil_temperature_1` | "Pool Temp", Ecowitt probe, 0.1 °C. |
| Outdoor temp | `sensor.gw3000a_outdoor_temperature` | °C, Ecowitt GW3000A (verified 17/9, 20.0 °C). Antifreeze input. |
| Solar headroom | `sensor.solar_headroom_for_heater` | W = PV (`sensor.panel_production_power`, kW) − house (`sensor.shelly_consumo_casa_power`) − pool excluding heater. **Already excludes the PdC**, so it does not collapse when the PdC starts. PV part is Fusion Solar cloud, ~5 min. |
| Grid power | `sensor.energy_grid_grid_consumption_power_grid_injection_power_net_power` | W, positive = import (Fusion Solar Casa). Diagnostics only. |
| Tariff band | `sensor.fascia_oraria` | Phase 0, live (`F1`/`F2`/`F3`). |
| Cover closed | `binary_sensor.pool_telo_chiuso` | Owner installs 20–21/9. Id TBD. |
| Bathing override | `input_boolean.pool_in_use` | Exists, on the dashboard. Keep as input. |
| Night routine | `input_button.chiudi_notte` | "Chiudi Casa". |
| Notify | `notify.mobile_app_matphone16` | iOS. |

Deprecated after this work: `binary_sensor.can_run_heater_on_solar` (dry 3 kW
threshold, no hysteresis), `automation.pool_chlorinator_daily_3h_run` (12:00,
6 h), `automation.pool_chlorinator_follows_pool_in_use` (absorbed). Keep the
alert automations (`pool_allerta_*`) — they are the owner's independent watchdogs.

## 3. Owner decisions (2026-09-16) — fixed

| Topic | Decision |
|---|---|
| Battery vs pool | Pool first. Headroom stays PV − house − pool, no SOC gate. Thresholds 3000 W on / 2500 W off. |
| PdC from grid | Allowed from day one (`grid_heating` default ON). Allowed with the cover open. "Grid" in practice = battery first, then grid (Huawei self-consumption): say so in the manual. |
| Guaranteed minimum | **27.0 °C**, hysteresis +0.5 (stop at 27.5). Solar target 28.0. |
| Tariff | Sorgenia, PUN-indexed per band. giu-26 energy component ≈ F1 0.164 · F2 0.193 · F3 0.166 €/kWh. **F1 ≈ F3, F2 is the expensive band** → grid heating is never allowed in F2. Reason on bands, never on prices (the index changes monthly). *(Amended 2026-09-18: the veto is the **night** window's. §5.4's daytime top-up runs in any band, F2 included — warm air buys ~24 % more heat per kWh, which is the same size as F2's ~17 % surcharge, so the veto only ever moved the run to a colder hour. See §5.4.)* |
| Windows | Same every day → time entities, not schedules. Pump 08:00–20:00 · PdC solar 10:00–18:00 · PdC grid 23:00–07:00 (crosses midnight) · chlorine 09:00–21:00. *(Confirmed 17/9 and shipped: §5.4's daytime GRID top-up, `switch.pool_grid_day_topup`, default ON — band-independent since the 18/9 amendment.)* |
| COP | Measured 2.82 (night, ~18 °C air, 95 Hz). Model COP vs outdoor air per §5.4; use it for estimates, log sessions to calibrate. |
| Filtration speed | 80 % for now (bypass calibrated at 80 %, ΔT 2 K; chlorinator flow-switch minimum unknown). Step-down test later. |
| Winter mode | Pump ≥ 2 h/day from 12:00 at 80 % with chlorine enabled; PdC off. **Antifreeze**: outdoor < 0 °C → pump continuous at `antifreeze_speed` (default 30 %, **tunable 30–80**), chlorinator OFF, release at ≥ +2 °C. *(Amended 2026-09-17: antifreeze also runs in `manual`/`closed` — see §5.5.)* |
| Cover | Physical sensor is primary. Image detection (AI Task) postponed. Alert at Chiudi Casa if open. |
| Pool volume | 90 m³ for turnover math (documents say 67–81; 90 is the conservative side). |

## 4. Entities the integration exposes

Follow `villa_hvac` naming (`unique_id` suffix stable, `RestoreEntity`, options
overridable). Suggested set:

**Settings (`number.*`)**: `pool_target_turnovers` (0.5–2, default 1.0) ·
`pool_target_chlorine_hours` (0–12, default 8) · `pool_winter_chlorine_hours`
(default 2) · `pool_cover_chlorine_factor` (0.1–1, default 0.5) ·
`pool_solar_target_temp` (default 28.0) · `pool_min_temp` (default 27.0) ·
`pool_solar_on_w` (3000) · `pool_solar_off_w` (2500) · `pool_filtration_speed`
(30–120, default 80) · `pool_pdc_speed` (default 80) · `pool_antifreeze_speed`
(30–80, default 30) · `pool_antifreeze_on_c` (0.0) · `pool_antifreeze_off_c`
(2.0) · `pool_winter_hours` (default 2).

**Windows (`time.*`)**: `pool_pump_start/end`, `pool_pdc_solar_start/end`,
`pool_pdc_grid_start/end`, `pool_chlorine_start/end`, `pool_deadline`
(default 19:00 — the hour by which targets must be met, after which catch-up
may extend the pump). Windows may cross midnight.

**Mode & switches**: `select.pool_mode` = `auto | filtration_only | winter |
manual | closed` · `switch.pool_grid_heating` (default ON) ·
`switch.pool_maintenance` (freezes all actuation, auto-off after 4 h with a
notification) · `switch.pool_chlorine_target_control` (escape hatch: off =
chlorine follows the window only).

**Sensors (truth & KPIs)**: `sensor.pool_cop_stimato`,
`sensor.pool_costo_termico_stimato`, `sensor.pool_last_session_*` (§5.4) ·
`sensor.pool_volume_today` (m³, integrates
`volume_flow_rate` per tick, resets at midnight; turnovers = /90) ·
`sensor.pool_chlorine_hours_missing` · `sensor.pool_pdc_state`
(`off|solar|grid|blocked`) with attributes `reason`, `since`, `min_on_until` ·
`binary_sensor.pool_solar_ok` (hysteresis + 10 min dwell) ·
`sensor.pool_cover_closed_for` (h) · `sensor.pool_supervisor_reason` (one line:
why each actuator is where it is — this is what the owner reads first when
something looks wrong).

## 5. Control law (pure modules, unit-tested)

Tick every 60 s (owner's PV data is 5-min; nothing here needs faster).

### 5.1 Pump — follows demand

`pump_wanted = any(window_pump, pdc_needs_flow, chlorine_wanted, pool_in_use,
antifreeze, catchup)`; `speed = max()` of the requesters' speeds (PdC → pdc_speed
80; filtration/chlorine → filtration_speed; antifreeze alone → antifreeze_speed).
Post-run: pump stays on `POSTRUN` (5 min) after the PdC stops. Start order: pump
ON → wait `pool_pompa_in_marcia` (60 s) → PdC / chlorine. Stop order: PdC OFF →
post-run → chlorine OFF → pump OFF. Winter: ignore the summer window; run from
12:00 for `winter_hours` at 80 % with chlorine; antifreeze is orthogonal and wins.

### 5.2 PdC — 4-state machine

States `OFF | SOLAR | GRID | BLOCKED`. HA writes `climate.set_hvac_mode`
(`heat`/`off`) and `climate.set_temperature`; the machine's own thermostat does
the rest.

- `OFF → SOLAR`: `pool_solar_ok` for 10 min AND water < solar_target AND (solar
  window OR grid window) AND pump in marcia.
- `SOLAR → OFF`: solar not ok for 15 min AND `MIN_ON` (30 min) elapsed; OR water
  ≥ solar_target. Setpoint in SOLAR = solar_target.
- `OFF → GRID`: `grid_heating` on AND water < min_temp AND grid window AND
  `fascia_oraria != F2` AND pump in marcia. Setpoint in GRID = min_temp.
  *(Amended 2026-09-18: the band test applies to the NIGHT grid window only.
  Inside §5.4's daytime window the band is not read at all.)*
- `GRID → OFF`: water ≥ min_temp + 0.5; OR grid window ends; OR band becomes F2
  *(night window only, as above — a daytime top-up is not stopped by the band
  turning F2, which is what stops it stopping itself every Saturday at 07:00)*.
- `GRID → SOLAR`: solar ok 10 min. `SOLAR → GRID`: solar not ok AND water <
  min_temp AND grid_heating AND grid window.
- `* → BLOCKED`: pump not in marcia, `pool_pdc_guasto`, `pompa_piscina_problem`,
  maintenance, mode manual/closed/winter. BLOCKED writes `off` immediately.
- `MIN_OFF` 15 min between two starts (compressor protection).
- Treat PdC `unavailable`/`unknown` as "no new information" (cloud polling gap),
  never as a state change. Judge nothing about the PdC for 10–15 min after a
  command (T02 lagged ~1 h once during the 16/9 bypass test).

### 5.3 Chlorinator — enable to target

`chlorine_wanted = pump in marcia AND chlorine window AND hours_today <
target_effective AND NOT cover_closed_for > 24 h AND NOT maintenance AND NOT
antifreeze`. `target_effective = target × (cover_factor if cover closed else 1)`,
winter → winter target. `pool_in_use` forces `chlorine_wanted` (but never
overrides the hydraulic interlock or the 24 h cover rule). Prefer "free hours":
enable as soon as the PdC enters SOLAR even before the chlorine window opens.
Catch-up: at `pool_deadline`, if hours are missing, keep pump + chlorine on until
target or midnight. Measure only `salt_chlorinator_runtime_today`; the switch
state is not evidence of production.

### 5.4 COP model — outdoor temperature matters (owner ask 2026-09-17)

The PdC is air-source: its COP falls with air temperature. The only measured
point is the night test of 16→17/9: **COP 2.82 at ~18 °C air**
(`sensor.pool_pdc_temp_ambiente` 19.5 at 08:50, GW3000A 18–20 overnight),
water 26–27 °C, compressor pinned at 95 Hz (full load). Everything else is a
model until more sessions are logged.

- Expose `sensor.pool_cop_stimato` = `COP_REF + COP_SLOPE × (T_air − T_REF)`,
  with `COP_REF 2.82`, `T_REF 18`, `COP_SLOPE 0.10 /°C` (Carnot-scaled guess,
  ~3.5 %/°C — LOW confidence; clamp 1.5–5.5). Air = `sensor.pool_pdc_temp_ambiente`
  (the air the evaporator sees), fallback `sensor.gw3000a_outdoor_temperature`.
  Indicative values: 10 °C → ~2.0 · 18 °C → 2.8 (measured) · 28 °C → ~3.8.
- Expose `sensor.pool_costo_termico_stimato` (€/kWh_th) =
  `energy_price(band) × (1 − solar_share) / COP_est`, with band prices as
  `number.*` settings (giu-26: F1 0.164 · F2 0.193 · F3 0.166 €/kWh, energy
  component only) and `solar_share = clamp(headroom / PdC_power, 0, 1)`.
  Diagnostics first; it becomes a decision input only after calibration.
- **Log every heating session** (start/end, kWh phase A, ΔT water, mean T_air,
  mean Hz, cover state) into a `sensor.pool_last_session_*` set so the slope
  can be fitted from data. Daytime sessions are contaminated by solar gain on
  the pool; a clean daytime COP needs the return-line probe after the bypass
  mix (owner's DS18B20 plan) — until then only night sessions calibrate.
- Full load is the worst COP regime for an inverter HP. `H08` (max heating
  frequency, 95 Hz) is NOT writable from HA (aquatemp fork exposes it as
  `sensor.pool_pdc_frequenza_max_riscaldamento` only). Lowering it on the
  machine's controller for the night window (e.g. 70 Hz) would raise COP at
  the cost of a longer run — a manual experiment for the owner, not a
  controller feature.

**Consequence for GRID — CONFIRMED by the owner 2026-09-17, shipped in v0.5.0.**
`switch.pool_grid_day_topup` exists and defaults ON. It allows GRID inside the
SOLAR window when SOLAR conditions fail; the night window stays as the fallback.

> **AMENDMENT 2026-09-18 (owner) — the daytime top-up ignores the tariff band.**
> v0.5.0 kept §3's F2 veto over the top-up. With the §3 windows that had exactly
> one effect: Saturday is F2 from 07:00 to 23:00 and the solar window is
> 10:00-18:00, so Saturday was the one day the pool could not top up by day
> (weekday F2 — 07-08 and 19-23 — falls outside the solar window entirely). And
> the veto did not save the heat, it deferred it to 23:00, where the kWh is
> ~17 % cheaper but the air is 8-10 K colder and the COP correspondingly worse.
> Those two effects are the same size, so the veto was buying the pool one cold
> day a week for no saving. The **night** window keeps the veto: there nothing
> varies but the band, so there F2 is simply dearer. The reason line names the
> band when a top-up runs in a vetoed one (`daytime top-up (band F2)`) — the
> exemption is not a licence to hide the dearest kWh the pool buys. Acceptance
> criterion §7.4 narrows to the night window as a result; see the note there.

Replayed over a September day: the marginal cost
is **0.0501 vs 0.0659 €/kWh_th, 24 % cheaper by day** — consistent with the
30-45 % estimated below, which assumed colder October nights. It costs one extra
compressor start a day, not a string of them. **It also roughly doubles the daily
spend**, because the pool now reaches and holds the minimum instead of drifting
below it: cheaper per kWh, more kWh. Acceptance criterion §7.1 changes as a
result — see the note there.

**Original proposal, for the record.** The 23–07 GRID window
was chosen for "F3 + covered". With F1 ≈ F3 in price and ~8–10 K warmer air by
day, a grid top-up in F1 costs an estimated 30–45 % less per thermal kWh than
the same top-up at night (e.g. Oct: 0.166/2.0 = 0.083 vs 0.164/3.5 = 0.047
€/kWh_th; LOW-MEDIUM confidence on the COP curve). Proposed amendment to §5.2:
- Day window (08–19, F1): if headroom ok → SOLAR (setpoint solar_target);
  else if water < min_temp → **GRID-day** (setpoint min_temp; whatever solar
  exists lowers the import).
- Night window (23–07, F3): GRID-night only if water is still < min_temp —
  a fallback, not the default.
- F2 (07–08, 19–23, Sat 07–23): never.
Implementation: keep `time.pool_pdc_grid_start/end` as the *night* window and
add `switch.pool_grid_day_topup` (default ON if the owner confirms) that
enables GRID inside the solar window when SOLAR conditions fail. The daily
deadline logic then reads: "reach min_temp by 19:00 in F1; if not, resume at
23:00 in F3".

### 5.5 Priority ladder (first match wins)

1 maintenance/manual → 2 fault or pump not in marcia → 3 cover closed > 24 h
(chlorine OFF, even with pool_in_use) → 4 antifreeze → 5 pool_in_use → 6 SOLAR →
7 GRID → 8 targets/catch-up → 9 pump window.

**AMENDMENT 2026-09-17 (owner), shipped in v0.4.0 — antifreeze outranks `manual`
and `closed`.** As written, rung 1 sat above antifreeze, so the supervisor
stopped protecting the pipes in `closed` — which is the mode the pool spends the
entire winter in, unattended, and therefore exactly when they are most at risk.
Being wrong one way costs a stopped pump for a few hours; the other way it costs
burst pipes. In those two modes a freeze now runs the pump at `antifreeze_speed`
and cuts the chlorinator (which §6 requires anyway before the speed may drop
below 80 %); the PdC is left alone, because the mode is still the owner's.
Having *started* the pump, the supervisor also stops it when the freeze
releases.

`maintenance` is deliberately unchanged and still freezes antifreeze too:
someone is physically at the pool, possibly with it drained or the valves shut,
so starting a pump under them is a hazard rather than a protection — and it
expires by itself after 4 h, where `closed` lasts months.

## 6. Guardrails (learned the hard way on this system)

- **Never base "pump running" on phase C watts.** P ∝ speed³: 427 W at 80 % →
  ~105 W at 50 % → ~22 W at 30 %. Only `pool_pompa_in_marcia`.
- **Never lower pump speed below 80 % while the chlorinator is enabled** until
  the step-down test is done (cell flow switch minimum unknown).
- **Never write `hvac_mode` more than once per MIN_ON/MIN_OFF window**; log every
  write with the reason.
- The owner's `automation.pool_test_cop_notturno` (one-shot 23:00–05:00, 16→17/9,
  self-disables) forces the PdC; the integration must not fight a running
  one-shot — check it is disabled before first deploy.
- On integration unload/reload: release nothing destructive (do not turn the
  pump off), just stop writing. On HA restart: restore settings; re-derive the
  PdC state from `pool_pdc_acceso` + water temp, do not assume OFF.
- Do not touch `pool-overview` (original dashboard); cards go on
  `pool-overview-v2`.
- Assign area `pool` to every entity/device (owner rule).

## 7. Acceptance criteria

1. With the 16/9 17:25 snapshot (water 25.6, headroom 0 W, 17:25, F1 on a
   weekday, pool_in_use on) the PdC is OFF (not in a window / no solar), the
   pump is ON at 80 % (pool_in_use), chlorine enabled.
   **AMENDED 2026-09-17 by the §5.4 confirmation**: 17:25 on a weekday is F1 and
   inside the solar window, with the water 1.4 K below the minimum — so with
   `grid_day_topup` ON (the default) the right answer is now GRID at the minimum
   setpoint, not OFF. The criterion as originally written is still pinned, with
   the switch off, in `TestCriterion01SnapshotOf16September`; the amended
   behaviour is pinned beside it and in `TestDaytimeGridTopUp`.
2. Same day 23:00 (F3, grid window, grid_heating on): PdC → GRID, setpoint 27;
   at 27.5 → OFF; pump post-run 5 min then follows other demand.
3. A 3 kW headroom pulse of 5 min does not start the PdC; 10 min does; a 12 min
   cloud does not stop it before MIN_ON.
4. Saturday 19:30 (F2): GRID is refused even inside the grid window; `reason`
   says `band F2`.
   **NARROWED 2026-09-18 by the §5.4 amendment**: this is now a statement about
   the NIGHT grid window. 19:30 is outside the solar window, so the criterion
   itself is untouched and still pinned in `TestCriterion04GridNeverInF2` — but
   "GRID is never in F2" is no longer true of the pool as a whole. Saturday
   midday in F2 now heats, and that is pinned too, in `TestDaytimeGridTopUp`.
5. Pump reported `problem` while PdC in SOLAR → PdC `off` within one tick,
   chlorine off, notification, state BLOCKED with reason.
6. Cover closed 25 h → chlorine off even with pool_in_use; reopen → resumes.
7. Winter mode, outdoor −1 °C at 03:00 → pump ON at antifreeze_speed, chlorine
   off; outdoor +2.5 → pump off (unless winter 12:00 slot).
8. `hours_today` 5 at 19:00 with target 8 → pump + chlorine stay on; at 8 h → off.
9. PdC entities `unavailable` for 4 min → no state change, no write.
10. Restart HA at 01:00 while GRID active → after restart the state is GRID
    again (or OFF if water ≥ 27.5), never a spurious extra start.

## 8. Order of work (one PR each, tag each)

0. Phase 0 objects — DONE 17/9 (see §1); only the cover alert is pending.
1. Repo scaffold from villa-hvac: manifest, config flow (entity pickers for the
   inputs above with the defaults pre-filled), engine tick, CLAUDE.md, tests
   skeleton, HACS install → v0.1.0 with **no actuation** (dry-run switch ON by
   default; log intended writes only). Run 24 h dry, compare logs with reality.
   Code DONE 17/9; **the 24 h dry run is still outstanding.**
2. Pump + chlorine supervisor (5.1, 5.3, ladder) → v0.2.0. Retire the two
   chlorinator automations. Code DONE 17/9; **the two automations are still
   live** — retiring them is an HA-side step for the owner.
3. PdC state machine (5.2) → v0.3.0. First real night of GRID observed with
   phase A energy. Code DONE 17/9; the observed night is still outstanding.
4. Winter mode + antifreeze → v0.4.0 (before November). DONE 17/9.
5. Dashboard cards on `pool-overview-v2` + manual PDF (villa-hvac style).

## 9. Open items handed over (not blockers)

- Flow at 30 % (verify at first cold snap; tune `pool_portata_minima`).
- Chlorinator minimum speed (step test 80 → 30 %, 5 min per step, watching
  `salt_chlorinator_running`).
- Chlorine target is a proxy in hours; the real target is FAC 1.5–2 ppm. An ORP
  probe (ESPHome) is in the owner's June plan; when the Modbus bridge to the
  UNIKO lands, switch the target from hours to estimated grams.
- Pool volume 67–90 m³ discrepancy (documents vs owner).

## 10. Kick-off prompt (paste into Claude Code in the new repo)

> Read `STORY_POOL_CONTROLLER.md` fully, then `../villa-hvac/CLAUDE.md` for the
> conventions (engine tick, pure supervisor modules, entity patterns, tests,
> STORY discipline). Scaffold `villa_pool` as a HACS custom integration with a
> config flow whose entity pickers default to the inventory in §2. Ship v0.1.0
> as dry-run only (no writes, log intended actions with reasons). Do not reopen
> the decisions in §3. Ask me for the cover sensor entity id before wiring it
> (it does not exist yet). Write the tests for §7 before the control law.
