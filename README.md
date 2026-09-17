# Villa Pool

Custom Home Assistant integration that **supervises** the pool of Villa
Pontacolone. It does not replace the equipment's own controllers — it reads the
water and air probes, the PV headroom, the tariff band and the pump/PdC/
chlorinator state, and coordinates the three as one hydraulic system:
filtration windows, a guaranteed minimum water temperature, solar-first
heating, chlorination to a daily target, and winter antifreeze.

> **Status: v0.3.0 — the pump, the chlorinator and the heat pump are all
> driven.** `switch.pool_dry_run` is **ON by default** and is what gates every
> write, so a fresh install still only decides, reports and logs. Turning it
> off is the owner's deliberate act and is announced in the log.

Target: Home Assistant **2026.8.3** (Python ≥ 3.14). Single instance,
config-flow. Full engineering context lives in [`CLAUDE.md`](./CLAUDE.md); the
owner's decisions and their rationale in
[`STORY_POOL_CONTROLLER.md`](./STORY_POOL_CONTROLLER.md).

## Design in one paragraph

Everything writes through **one engine** (`engine.py`): each 60 s tick it builds
a `PoolState` from the coordinator's reads plus the integration's own setting
entities, hands it to a **pure** control law (`supervisor/`, no Home Assistant
imports anywhere), and reports the result. The law is a single function
`(PoolState, Memory) -> (Decision, Memory)` — the pump follows demand, the PdC
runs a four-state machine (`OFF | SOLAR | GRID | BLOCKED`) with minimum on/off
times, and the chlorinator is enabled towards a daily hours target — resolved
through a fixed priority ladder: maintenance/manual ▸ fault or no flow ▸ cover
closed > 24 h ▸ antifreeze ▸ bathing ▸ solar ▸ grid ▸ targets/catch-up ▸ pump
window.

Commands are **idempotent**: a lever is written to only when the device
disagrees with the decision, so a restart against a pool that is already doing
the right thing sends nothing at all. A lever that does not adopt a command is
re-asserted exactly once and then left alone — the supervisor keeps deciding and
reporting, but stops arguing with whoever is moving it. Order inside a tick is
hydraulic, not cosmetic: flow is never taken away before the things drawing
through it, so the chlorinator is cut before the pump, and the pump is held on
while the heat pump still reads as running.

The heat pump gets two extra protections, because it is the only lever that can
be damaged by being asked twice. Its entities are cloud-polled and flicker
`unavailable` between polls, and that is treated as *no information*: no state
change and no command. A repeat of the same `hvac_mode` is rate-limited to once
per compressor grace (15 min, STORY §6), while a *change* of mind is never
rate-limited — so a pump fault still stops it within one tick. Replaying a
September night offline gives five commands in eight hours for two heating
runs, and a night of flickering `unavailable` produces exactly the same five,
at the same minutes.

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/matponta/villa-pool`,
   category **Integration**.
2. Install **Villa Pool**, then restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Villa Pool.**
4. Confirm the entity pickers. They are pre-filled with the verified inventory;
   leave **Cover closed** empty until that sensor exists.
5. Assign the *Pool* device to the **pool** area.
6. Leave `switch.pool_dry_run` **on** for 24 h and compare the log with what
   the pool actually did. Only then turn it off — and first retire the two
   chlorinator automations and confirm `automation.pool_test_cop_notturno` is
   disabled (see `NEXT_SESSION.md`), or they and the supervisor will take turns
   on the same relays.

## What it exposes

**Settings** — `number.*` (minimum temperature 27.0, solar target 28.0, solar
headroom on/off 3000/2500 W, filtration/PdC/antifreeze speeds 80/80/30 %,
antifreeze band 0/+2 °C, chlorine and turnover targets, per-band prices) ·
`time.*` (pump, PdC solar, PdC grid, chlorine windows, targets deadline, winter
slot) · `select.pool_mode` (`auto | filtration_only | winter | manual | closed`)
· `switch.*` (`dry_run` ON, `grid_heating` ON, `chlorine_target_control` ON,
`maintenance` with a 4 h auto-off — which *freezes* actuation, and does not
switch the pool off).

**Diagnostics** — `sensor.pool_supervisor_reason` (one line: why each actuator
is where it is — read this first) · `sensor.pool_pdc_state` with `reason` /
`since` / `last_stop` · `sensor.pool_cop_stimato` and
`sensor.pool_costo_termico_stimato` (estimates only) ·
`sensor.pool_chlorine_hours_missing` · `sensor.pool_volume_today` (with
turnovers) · `sensor.pool_cover_closed_for` · `binary_sensor.pool_solar_ok`.

`sensor.pool_supervisor_reason` also carries `writes`, `last_write`, `latched`
(levers the supervisor has stopped driving because something else kept moving
them back) and `holding` (levers it wants to move but is holding back for a
hydraulic reason). If the pool is not following, read those two first.

## Key verified facts

Measured live; do not re-derive them (see `CLAUDE.md`):

- **"Pump running" is never watts.** P ∝ speed³, so a power threshold silently
  lies below 80 %. The only truth is `binary_sensor.pool_pompa_in_marcia`, and
  it carries no debounce of its own — the integration confirms for 60 s.
- **The PdC is cloud-polled** and flickers `unavailable` between polls. That is
  "no new information", never a state change, and never a reason to write.
- **COP 2.82 at ~18 °C air**, measured overnight 16→17/9 with the pool covered
  and the compressor at full load. Use ~2.8, not 4, for night estimates.
- **F2 is the expensive tariff band**; F1 ≈ F3. Grid heating is never allowed in
  F2, and the decision is made on the *band*, never on a price.

## Tests

190 tests, all pure-fast except the end-to-end ones, which run against the exact
deploy-target HA.

```bash
python3.14 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
```

`tests/test_acceptance.py` is organised as one class per acceptance criterion of
STORY §7, so a failure points straight back at the brief.

## Licence

MIT.
