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

## Status: v0.3.0 — the pump, the chlorinator and the PdC are all driven.

**`switch.pool_dry_run` is the gate, it is ON by default, and it is now real.**
Through v0.1.0 there was no write path at all and the switch was only a
declaration; from v0.2.0 it is what decides whether a tick's decision becomes
service calls. A fresh install or an upgrade therefore still writes nothing
until the owner turns it off, which is logged at WARNING on the transition.

- `ACTUATION_IMPLEMENTED` in `engine.py` — a write path exists (True from
  v0.2.0). Historical marker; it no longer gates anything.
- `PDC_ACTUATION_IMPLEMENTED` in `actuator.py` — True from v0.3.0. It was False
  in v0.2.0 and the climate levers were not built at all, which is how §8 step
  2's "leaving the PdC untouched" was made structural rather than a promise.
- `tests/test_engine.py::test_dry_run_calls_nothing` keeps the v0.1.0 guarantee
  for the DEFAULT configuration, across all six services.

**Interpretation note.** §8 step 1 says "v0.1.0 with no actuation" and §8 steps
2-3 put the pump/chlorine and PdC supervisors in v0.2.0/v0.3.0 — but step 1 also
requires "log intended writes only ... run 24 h dry, compare logs with reality",
which is only meaningful if the controller is already *deciding*. So the **full
pure control law shipped in v0.1.0** and only actuation was deferred. v0.2.0 and
v0.3.0 are therefore "wire the writes for these levers + retire the old
automations", not "write the law".

### How a write happens

`law.decide()` says what the pool should be doing. `supervisor/actuation.py`
(pure) answers, per lever, whether to send a command about it; `actuator.py`
owns the entity ids, the services, the order and the logging. Four rules:

1. **Idempotent** — a lever is written only when the device disagrees. The
   agreement check comes *before* the "is this a new intent" check, which is
   what makes a restart against an already-correct pool send nothing.
2. **Re-assert once, then stop** — a disagreement inside the settle window is
   patience (the tuya bridge and the aquatemp cloud both lag); after it, one
   more command, and then the lever is latched and left alone. The latch clears
   by itself when our intent changes or the device comes back. It is never
   called "manual" in a message, because a human and an unresponsive device
   leave identical evidence.
3. **A read gap is not evidence** — an `unavailable` lever is neither written
   to nor held responsible, the same rule the PdC has had since v0.1.0.
4. **Order inside a tick is hydraulic** — PdC off ▸ chlorine off ▸ pump off on
   the way down; pump ▸ PdC ▸ chlorine on the way up. Two guards back that up
   when a command has not landed yet: the speed is never dropped below 80 %
   while `switch.clorinatore` still reads on (§6), and the pump is never
   stopped while `binary_sensor.pool_pdc_acceso` still reads on. Both defer
   rather than cancel, and both fail towards *more* flow.

### The PdC is the lever that can be damaged by being asked twice

It gets three protections the others do not:

- **A polling gap is not information.** `decision.pdc_write` false (the climate
  entity is `unavailable`/`unknown`) means the climate levers are not even
  built this tick. No state change, no command. This is the single behaviour
  the 24 h dry run was told to watch, because it is the one that would cause a
  double compressor start.
- **`hvac_mode` is rate-limited to once per compressor grace** (15 min =
  MIN_OFF, STORY §6) — but only for a *repeat*. A change of mind is never rate
  limited, which is what lets §7.5's "PdC `off` within one tick" hold even when
  the grace has just started.
- **The setpoint has a shorter grace** (10 min, the bottom of §5.2's range and
  two cloud polls). It cycles nothing, and a controller that refuses a target
  while it is off should not be left on the wrong one for a quarter of an hour.
  It is written *before* `heat`, so the machine never starts against whatever
  target ran last; if the attribute is unreadable it is not written blind.

Replaying a September night offline gives 5 commands in 8 h for 2 heating runs,
and a night of flickering `unavailable` gives exactly the same 5, at the same
minutes. That replay is the check to re-run before any change to the machine or
the planner — it is what caught the v0.1.0 short-cycle.

**In `auto` mode the supervisor owns the machine.** If the owner starts the PdC
by hand it will be commanded back off, twice, and then the lever latches and it
is left alone. The intended escape hatches are `select.pool_mode = manual` and
`switch.pool_maintenance`, not winning the argument.

`switch.pool_maintenance` and the `manual`/`closed` modes set
`Decision.actuate=False`, which means **hands off every lever** — not "write
everything off". Freezing is what §4 asks for, and stopping the pump under an
owner who flipped maintenance to work on the pool is the opposite of it.

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
  `actuation` (should this lever be commanded at all) · `model` (the data
  carriers).
- `engine.py` — builds `PoolState`, runs `decide()`, logs intent **on change**
  at INFO, notifies the diagnostic entities, then hands the decision to the
  actuator. One `asyncio.Lock` serialises the scheduled tick and any awaited
  `request_run`.
- `actuator.py` — the HA half of the write path (levers, services, order,
  notifications). Writes are bounded by `WRITE_TIMEOUT_S`: a cloud lever that
  never answers must not hold the engine's lock and make the supervisor deaf.
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

### Two clocks, and why

`PoolState.now` is naive **local** time and answers only "where in the day are
we" — the §3 windows, the deadline, the winter slot. `PoolState.mono` is the
same instant in **UTC** and is what every *duration* is measured against
(MIN_ON, MIN_OFF, the 60 s pump confirmation, the solar dwells, the post-run,
the settle windows). Everything in `Memory` is stamped in the UTC frame.

This is not tidiness. Across the March transition local time jumps an hour
forward, so a stamp from 01:55 reads as 70 minutes old at 03:05 when ten real
minutes have passed — and MIN_OFF is fifteen, so the compressor gets a restart
it has not earned. October does the mirror image: an hour in which nothing can
elapse at all. Once a year each, invisible in a dry run, and pinned by
`tests/test_supervisor.py::TestDaylightSaving` — including a test that shows
the same situation restarting the machine when the anchor is dropped.

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
- **Do not test writes with `async_mock_service` for `switch`/`number`/
  `select`.** It replaces the domain's service handler, and those components
  register their own when the integration forwards its platforms — so the mock
  is silently overwritten and a "nothing was written" assertion passes because
  nothing was listening. `tests/test_engine.py::record_calls` listens on
  `EVENT_CALL_SERVICE` instead, which observes the real call and cannot be
  overwritten. (The v0.1.0 dry-run tests were weaker than they looked for
  exactly this reason.)
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

## Before turning `switch.pool_dry_run` off

These are HA-side steps, not code, and none of them has been done from here —
retiring a live automation while the pool has no other chlorination control
would leave the cell unmanaged, so it waits for the owner.

1. **Retire `automation.pool_chlorinator_daily_3h_run` (12:00, 6 h) and
   `automation.pool_chlorinator_follows_pool_in_use`** (STORY §2, §8 step 2).
   Both write `switch.clorinatore`. Left enabled they and the supervisor take
   turns on the relay, and the supervisor will read the disagreement as a
   manual override and latch the lever — correctly, and uselessly.
2. **Keep every `pool_allerta_*`** automation and
   `automation.pool_chlorinator_safety_cutoff_on_low_circulation`. They are the
   owner's independent watchdogs and are deliberately outside this integration.
   The cutoff never fights the supervisor: when flow is lost the law wants the
   cell off too.
3. **Confirm `automation.pool_test_cop_notturno` is disabled** (§6) — the
   integration must not fight a running one-shot.
4. Run 24 h dry first and compare the log with the pool's real logbook.

## Before the next step

- **The cover sensor entity id is still unknown.** `binary_sensor.pool_telo_chiuso`
  is a placeholder; the owner installs the sensor 20-21/9. Ask for the real id,
  set it in the options flow, then wire the §1 `pool_allerta_telo_aperto_chiudi_casa`
  alert. Until then every cover rule is inert by construction.
- Check `automation.pool_test_cop_notturno` is disabled before any actuating
  release — the integration must not fight a running one-shot (§6).
- **§5.4's daytime GRID top-up is still PROPOSED** and unimplemented;
  `switch.pool_grid_day_topup` does not exist. Owner to confirm.
