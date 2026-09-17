# CLAUDE.md — Villa Pool integration

Context for Claude Code working on the `villa_pool` custom Home Assistant
integration. **Read this first, then `STORY_POOL_CONTROLLER.md`.** This file is
the source of truth for how the repo works; the STORY is the source of truth for
what the owner decided and why.

## What this is

An **orchestration** integration for the pool of Villa Pontacolone. It
supervises three devices as one hydraulic system — the pump, the heat pump
(PdC) and the salt chlorinator — through **windows** (when things are
*allowed*), **daily targets** (what must be *achieved*) and **interlocks**
(what can never be violated), with the pump *following demand*.

It does NOT replace the devices' own controllers: the PdC keeps its thermostat
(HA writes only `hvac_mode` + setpoint) and the UNIKO chlorinator keeps its
cycles (HA only *enables* it).

Separate repo from `villa-hvac` deliberately: different physical domain,
different failure modes, different release cadence. It **reuses that repo's
skeleton and conventions and shares nothing at runtime.**

Target: Home Assistant **2026.8.3** (Python ≥ 3.14). Single instance,
config-flow hub.

## Status: v0.1.0 — DRY RUN. The integration writes nothing.

This is the most important fact in the repo and the easiest one to break.

`engine.py` contains **no write path at all** — not a disabled one, not one
behind a flag. There is no `hass.services.async_call` anywhere in
`custom_components/villa_pool/`. `switch.pool_dry_run` (ON by default) is a
*declaration* of that, not the gate enforcing it; turning it off logs a loud
warning and still writes nothing. `ACTUATION_IMPLEMENTED = False` in `engine.py`
is the single flag a future release flips, and
`tests/test_engine.py::test_v0_1_0_has_no_actuation_path` pins it.

Six parametrised tests assert that no `switch.turn_on/off`,
`climate.set_hvac_mode/set_temperature`, `number.set_value` or
`select.select_option` call is ever made, under conditions that would want all
of them. Keep those tests passing until §8 step 2 deliberately changes them.

**Interpretation note.** §8 step 1 says "v0.1.0 with no actuation" and §8 steps
2-3 put the pump/chlorine and PdC supervisors in v0.2.0/v0.3.0 — but step 1 also
requires "log intended writes only ... run 24 h dry, compare logs with reality",
which is only meaningful if the controller is already *deciding*. So the **full
pure control law ships in v0.1.0** and only actuation is deferred. v0.2.0 and
v0.3.0 therefore become "wire the writes for these levers + retire the old
automations", not "write the law".

## Architecture

Everything flows one way: coordinator reads → engine builds a snapshot → pure
law decides → engine reports. No module skips a step.

- `const.py` — the verified §2 entity inventory (as config-flow **defaults**
  only) and the §3 fixed decisions. Nothing reads an entity id from here at
  runtime.
- `config_flow.py` — an **entity picker per input**, pre-filled from §2. Unlike
  `villa_hvac`, which hard-codes its KNX map, the pool's entities come from
  tuya-local, Shelly, Ecowitt and an aquatemp fork, all of which rename things
  across updates — a renamed probe must be a two-click fix, not a release. The
  cover picker is the one with **no default** (§1: "ask, don't guess").
- `coordinator.py` — 60 s read-only poll of every configured input. Its one
  piece of judgement: `unavailable`/`unknown` becomes `None`, **never** `False`
  or `0`. Also integrates `volume_flow_rate` into today's m³.
- `supervisor/` — the **pure** core. No HA imports anywhere, so the whole law is
  unit-testable without HA and replayable offline against the dry-run logs.
  Submodules: `windows` (the only place midnight wrap-around is interpreted) ·
  `solar` (hysteresis + entry dwell) · `pdc` (the 4-state machine) · `chlorine`
  (enable-to-target) · `pump` (demand collection + sequencing) · `cop`
  (diagnostic model) · `law` (`decide()` + the §5.5 ladder + `restore_memory`) ·
  `model` (the data carriers).
- `engine.py` — builds `PoolState`, runs `decide()`, logs intent **on change**
  at INFO, notifies the diagnostic entities. One `asyncio.Lock` serialises the
  scheduled tick and any awaited `request_run`.
- `entity.py` / `number.py` / `time.py` / `select.py` / `switch.py` — the
  settings, as the integration's own restoring entities (§4). Each publishes
  into `runtime_data.settings`, which is what the engine reads.
- `sensor.py` / `binary_sensor.py` — the diagnostic surface.
  `sensor.pool_supervisor_reason` is the primary output of v0.1.0.

### Entity ids are a contract

STORY §4 names the entities explicitly (`switch.pool_dry_run`,
`sensor.pool_supervisor_reason`, `number.pool_min_temp`, …) and the owner's
dashboards and automations reference those ids. Under `has_entity_name` the id
prefix comes from the **device name**, so the device is called `Pool` (not
`Villa Pool`) and each entity's `name` is chosen to slugify onto the §4 id —
hence terse labels like "Solar on w" and "Min temp". Renaming the device would
silently rename all 36 entities. Pinned by
`tests/test_engine.py::test_story_section_4_entity_ids`.

### Two ordering facts that are load-bearing

1. **The engine is constructed BEFORE the platforms and started AFTER them**
   (`__init__.py`). Constructed first so the diagnostic entities can subscribe
   to it as they are added — a sensor driven by the coordinator alone publishes
   the *previous* tick's decision, i.e. a full minute stale. Started last
   because it reads the setting entities, which do not exist until the platforms
   are up.
2. **The chlorinator's *demand* is computed before the pump is sized, and its
   *enable* after** (`law.decide`). The pump follows demand and chlorine is one
   of the demands, so asking "what would chlorine want if it had flow?" is what
   breaks the circularity.

## Critical domain rules (do not re-derive)

- **Never base "pump running" on watts.** P ∝ speed³: 427 W at 80 % → ~105 W at
  50 % → ~22 W at 30 %. The only truth is
  `binary_sensor.pool_pompa_in_marcia`, and it has **no built-in
  debounce** (§1) — the 60 s confirmation is this integration's own job.
- **`unavailable` is not a state change.** The PdC is cloud-polled at ~5 min and
  flickers between polls. Reading a polling gap as "off" and restarting a
  machine that never stopped is the failure `Decision.pdc_write` exists to
  prevent. Judge nothing about the PdC for 10-15 min after a command.
- **Never enable the chlorinator below 80 % pump speed** until the step-down
  test establishes the cell's real flow-switch minimum (§6, §9).
- **F2 is never a grid-heating band** (§3). Reason on *bands*, never on prices —
  the PUN index changes monthly.
- **Do not turn the pump off on unload.** Release nothing destructive; just stop
  deciding (§6). Pinned by `test_unload_releases_nothing_destructive`.
- **On restart, re-derive the PdC state** from `pool_pdc_acceso` + water temp;
  do not assume OFF. `restore_memory` also adopts a pump that is already in
  marcia — the 60 s debounce filters a *fresh transition*, and is not a reason
  to re-prove a steady state that predates the restart. Without that, every
  restart would write `off` → `heat` and put a spurious cycle on the compressor.
- **The COP model is diagnostic only.** One measured point (2.82 at ~18 °C
  air) and a Carnot-scaled slope, LOW confidence. Nothing in the control law
  reads it, and nothing should until it is calibrated against logged sessions.
- **Assign area `pool` to every entity/device** (owner rule). All entities hang
  off one service device so this is a single assignment.
- Cards go on `pool-overview-v2`. Do not touch `pool-overview`.

## Known spec gaps found while building (owner input wanted)

These are recorded rather than silently resolved:

1. **§7.4's premise cannot fire with the §3 windows.** "Saturday 19:30 (F2):
   GRID is refused *even inside the grid window*" — but the grid window is
   23:00-07:00, so 19:30 is outside it and the band veto is never reached. The
   acceptance test therefore widens the grid window to 19:00 (they are
   owner-editable `time.*` entities) so the clause is genuinely exercised, and
   pins the band veto separately. See `TestCriterion04GridNeverInF2`.
2. **"Free hours" is unreachable with the default windows.** §5.3 wants chlorine
   enabled as soon as the PdC enters SOLAR "even before the chlorine window
   opens" — but the solar window (10-18) lies wholly inside the chlorine window
   (09-21), so that state cannot occur. The branch is implemented and tested
   with a narrowed chlorine window. Harmless, but it means the feature does
   nothing as configured.
3. **Antifreeze in `manual`/`closed` mode.** The §5.5 ladder puts
   maintenance/manual above antifreeze, so those modes currently freeze
   antifreeze too. Defensible (the owner has taken control) but worth a
   decision, since `closed` is exactly when the pipes are most at risk.
4. **The SOLAR target has no hysteresis, by specification.** §5.2 stops SOLAR
   at `water >= solar_target` and restarts below it — one threshold, both ways.
   With the probe dithering on 28.0 that gives ~4 compressor starts over 5 h
   (bounded by MIN_OFF, so not runaway). Adding a band would fix it but would
   also stop the pool topping up on *free* sun between 27.5 and 28.0, so the
   trade is the owner's, not an engineering call. Unlike the GRID hysteresis bug
   (which was a real defect — the band existed and was being ignored), this is
   the spec working as written.
5. **§5.4's daytime GRID top-up is NOT implemented** — it is explicitly marked
   "PROPOSED, owner to confirm". `switch.pool_grid_day_topup` does not exist.

## Dev / deploy

- **Code** lives here (git repo root = this folder). Push to GitHub; this also
  enables HACS install. Note `villa-hvac` is NOT a sibling on this machine — it
  is at `~/Documents/Claude/Projects/Home Assistant/villa-hvac`.
- **Test harness** is pinned to the exact deploy target:
  `pytest-homeassistant-custom-component==0.13.357` bundles
  `homeassistant==2026.8.3`, the owner's live HA. "CI green" therefore means
  green-on-target. Bump the pin, the CI `python-version` and `hacs.json`
  together and deliberately.
- `python3.14 -m venv .venv && .venv/bin/pip install -r requirements-test.txt`,
  then `.venv/bin/python -m pytest -q` and
  `.venv/bin/ruff check custom_components/villa_pool tests`.
- **Deploy to HA**: HACS custom repository → install → restart. Or copy
  `custom_components/villa_pool/` into `/config/custom_components/` and restart.

## Release discipline (from villa-hvac)

- One PR per step of STORY §8, tagged. Bump `manifest.json` `version` in the
  same commit as the change it describes.
- **Pre-tag adversarial review**: before tagging, re-read the diff hunting for
  the failure mode rather than confirming the happy path. Record what the review
  found in `NEXT_SESSION.md`, including "nothing".
- `NEXT_SESSION.md` carries the kickstart prompt and the live-verify result of
  each release. Write the *verification* result, not just the intent.
- Keep `CLAUDE.md` current in the same PR as the behaviour it documents.

## Before the next step

- **The cover sensor entity id is still unknown.** `binary_sensor.pool_telo_chiuso`
  is a placeholder; the owner installs the sensor 20-21/9. Ask for the real id,
  set it in the options flow, then wire the §1 `pool_allerta_telo_aperto_chiudi_casa`
  alert. Until then every cover rule is inert by construction.
- Check `automation.pool_test_cop_notturno` is disabled before any actuating
  release — the integration must not fight a running one-shot (§6).
