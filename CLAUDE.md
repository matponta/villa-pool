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

## Status: v0.8.0 in the repo; deployed and actuating since 2026-09-17 20:04.

**The integration is live on the owner's HA and `switch.pool_dry_run` is OFF.**
Anything in this repo that reads as "not yet deployed" is stale — `NEXT_SESSION.md`
opens with the verified live status. Treat changes accordingly: they reach a
pool that is being driven, not a dry run.

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

### Antifreeze outranks `manual` and `closed` (owner amendment, 2026-09-17)

STORY §5.5 as written put rung 1 above antifreeze, so the supervisor stopped
protecting the pipes in `closed` — the mode the pool spends the whole winter in,
unattended. The amendment is recorded in the STORY itself, dated; this is the
engineering half of it.

In `manual` and `closed`, a freeze now:

- runs the pump at `antifreeze_speed`, and **cuts the chlorinator** — which is
  not optional, because §6 refuses to take the speed below 80 % while the cell
  is enabled, so antifreeze that did not cut it could not run at all;
- leaves the PdC alone (`pdc_write=False`): it is blocked either way, and
  writing `off` to it is more than freeze protection asked for;
- **stops the pump again when the freeze releases.** `Memory.antifreeze_owns_pump`
  is what makes that possible. Without it the release reverts to "not driving
  anything" and the pump the supervisor started runs until someone visits the
  pool house. The stop stays asserted rather than firing once, so a command
  that does not land is still re-asserted.

`maintenance` is deliberately unchanged and still freezes antifreeze too:
someone is physically at the pool, possibly with it drained, and it expires by
itself after 4 h where `closed` lasts months.

**The antifreeze latch is re-derived on restart against the RELEASE threshold**,
not the engage one. At +1 °C, inside the 0..+2 band, a fresh `Memory` cannot
know whether antifreeze was running; answering "no" stops the pump mid-cold-snap,
answering "yes" costs ~22 W until the air passes +2. Same asymmetry
`antifreeze_step` already applies to an unknown temperature.

### The daytime grid top-up (§5.4, owner-confirmed 2026-09-17)

`switch.pool_grid_day_topup` (default ON) lets GRID run inside the **solar**
window when SOLAR conditions fail. `pdc.grid_window()` is the one place that
decides which window authorises a grid run, and returns `night` or `day` so the
reason line can name it — both on entry AND while it holds, because the entry
reason scrolls past in one tick and these two cost very different amounts.

**The band veto is night-only (owner amendment, 2026-09-18).** v0.5.0 kept §3's
F2 refusal over the top-up, and with the §3 windows that had exactly one effect:
Saturday is F2 07:00-23:00 against a 10:00-18:00 solar window, so Saturday was
the one day the pool could not top up (weekday F2, 07-08 and 19-23, is outside
the solar window altogether). It also did not save the heat — it deferred the
run to 23:00, where the kWh is ~17 % cheaper and the air 8-10 K colder. Same
size, opposite signs. `pdc.in_day_topup_window()` is now both the day window
*and* the band exemption, asked separately from `grid_window()` because the two
windows can be made to overlap and what lifts the veto is that the DAY window
authorises — not which window the label names. The night window keeps the veto,
which is what §7.4 is now about.

The reason line names the band when a top-up runs in a vetoed one
(`daytime top-up (band F2)`). Exempt is not the same as hidden: that is the
dearest electrical kWh the pool buys and the line is the only place it shows.

**A restart mid top-up adopts the run** (v0.7.0). `restore_memory` used to ask
its own questions about windows and bands, and the copies drifted twice in two
days — first when the daytime window appeared, then when this amendment made the
veto night-only. It is now handed `grid_conditions(state, running=True)`'s own
answer and holds no opinion, so **the supervisor adopts exactly what the law
would authorise this tick**. Adopting anything else is worse than adopting
nothing: the next tick refuses it, `_stop` fires, and the supervisor stops a run
it never began.

**It roughly doubles the daily spend.** Measured by replaying a September day:
the marginal cost is 24 % lower by day (0.0501 vs 0.0659 €/kWh_th) and it costs
one extra compressor start, but the pool now *reaches and holds* the minimum
instead of drifting below it — so it delivers about twice the heat. Cheaper per
kWh, more kWh. If the bill is the complaint, the lever is the switch.

### The ORP/pH chlorine trim (§5.3, §9 — owner go-ahead 2026-09-21)

§9 recorded that "chlorine target is a proxy in hours; the real target is FAC
1.5-2 ppm". v0.8.0 closes half of that with a YINMIK WF-3188 tester in the
skimmer. **It ships inert**: `switch.pool_orp_control` defaults OFF, and with
it off — or with no probe, or a stale reading — `decide()` is the v0.6.0 hours
law. `tests/test_water.py::TestDegradesToTheHoursLaw` is the
`test_dry_run_calls_nothing` of this release.

**The trim moves the hours TARGET; it never switches the cell.** Four things
fall out of that one choice:

1. **Hysteresis is free.** Bang-bang on ORP would chatter the relay; trimming
   an hours target leaves the existing integrator in the loop. §9 gap 4 is
   what the other choice looks like on the SOLAR target.
2. **A stale reading is not a special case** — trim 0.0, §5.3 unchanged. The
   PdC's "a read gap is not evidence", applied to chemistry.
3. **A failed probe cannot run away.** The bound is in hours.
4. **ORP never gates the cell on or off**, and cannot: no flow, no reading.
   The hours law is always the cold start. The corollary is a real limitation
   — the trim can EXTEND a run in progress but cannot start one from a fully
   stopped pool. In practice the pump runs its filtration window anyway.

**The skimmer lies, so a reading only counts after a flush.** Measured
2026-09-21: stagnant EC 7.01 mS/cm against 8.906 flushed, **21 % low**, with
the owner's own reference tool putting the pool at 8.61 — two instruments, one
conclusion. `WATER_FLUSH_S` (180 s, measured settling ~95 s from confirmation
plus margin) is counted against `Memory.pump_running_since`, so it *includes*
the 60 s pump confirmation. After a restart with the pump already running the
reading is pessimistically stale for ~2 min, because `restore_memory` stamps
that field exactly `PUMP_CONFIRM_S` back; deliberate, and it costs nothing.

**EXTENSION ONLY — a cut oscillates.** Found in the pre-tag review and
verified by simulation. 20:30, pump window shut and chlorine window open, 5.6 h
done against a 6.0 h target, ORP above target: fresh reading → cut → target
5.5 → met → chlorine off → pump off → reading stale → trim 0 → target 6.0 →
0.4 h missing → catch-up demands the pump → on → fresh → cut returns. **A
5-minute pump cycle, 12 starts an hour.** The trim's input depends on the
thing it controls. Extension has no such loop: raising the target keeps the
pump running and the reading fresh, and going stale falls back to the hours
law, which settles to "off" and stays there. Pinned by
`TestStability::test_no_pump_cycling_at_the_target_boundary`, which was
confirmed to FAIL with the cut reintroduced. A cut could be made safe with a
"chlorine done for today" latch in `Memory`, reset at midnight and re-derived
on restart — a separate step with its own restart semantics.

**The pH ceiling is the rung that matters.** Chlorine's active form is HOCl and
its share collapses as pH rises (~75 % at 7.0, ~50 % at 7.5, ~22 % at 8.0),
while a salt cell *raises* pH as a byproduct — the owner's log drifts to
7.7-7.8 and is corrected with 2 kg of pH-. Above `ph_ceiling` the extension is
suspended and the reason line asks for acid. Without it the loop would extend,
see no improvement, extend again and hit its cap every day, winding up against
a constraint it has no authority over. **An unknown pH suspends it too**: the
ceiling exists to prevent windup, so losing the ceiling loses the extension.

**The target is owner-settable on purpose.** Cyanuric acid suppresses ORP for
a given FAC and accumulates from the slow tablets, and the probe carries its
own offset (−102 mV against the owner's reference on 2026-09-21). Setting
`number.pool_orp_target` from THIS pool's own readings absorbs both, which is
the absolute-threshold form of a baseline-delta.

**The measurement that motivated it**, 2026-09-21: the cell ran 6.69 h against
a 6.0 h target — `chlorine_hours_missing` 0, the day "done" — and the
reference measured FAC 1.2 ppm, under §9's 1.5-2. The hours proxy said done;
the water said short. `TestTheGapSection9Recorded` replays exactly that.

**Still unresolved: whether the probe deserves this.** pH and ORP had not
settled after 16 min of flow (EC settled in 90 s and held), and ORP read
−102 mV against the reference whose own mV series is erratic. The switch is
OFF until a long run says pH and ORP plateau. EC agreed to 3.4 %, so the
salinity channel is trustworthy today.

### The heating-session log (§5.4)

`supervisor/session.py` brackets each heating run and records what it cost, so
the COP model can eventually be **fitted** rather than guessed. Three sensors —
COP, mean air, electrical kWh — because a fit needs COP against air temperature
as two recorded series, and an attribute is awkward to graph.

The arithmetic is the owner's own: `90 m³ × 1.163 × ΔT` thermal over the phase-A
kWh. The night of 16→17/9 comes back as 2.81 against the 2.82 recorded in §1,
which is the check that the log is comparable with the one measurement the model
rests on (`TestTheMeasuredNightReproduces`).

Two rules keep it honest, and both matter more than completeness:

- **It refuses to invent a COP.** Too short, ΔT inside the probe's 0.1 °C
  resolution, a missing reading, a meter that did not move — each publishes the
  session with `cop` unset and a `note` saying which. This exists to be fitted;
  a wrong point is worse than no point, because it would be believed.
- **Only night sessions are `clean`.** §5.4: daytime runs are contaminated by
  solar gain on the pool, and the compressor would take the credit. `all_night`
  is ANDed every tick, so a run that crosses sunrise is disqualified — which now
  matters, because the day top-up makes daytime GRID runs ordinary.

It brackets on the SUPERVISOR's state, not `pool_pdc_acceso`: the machine's own
flag is cloud-polled and flickers, and a flicker would chop one run into several.
A run already going at startup is not adopted — its start reading was never
taken. That rule is enforced by `was_running`, which the engine seeds from the
*restored* memory (v0.7.0): seeded blindly False, a restart that adopts a run
reads as a false->true edge and brackets the tail of it, then publishes it as
`clean` because it is a night run.

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
  (diagnostic model) · `water` (is the chemistry reading real, and the ORP
  trim) · `law` (`decide()` + the §5.5 ladder + `restore_memory`) ·
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

### The live entity ids are NOT the §4 ids

Checked on the owner's HA 2026-09-17: the deployed integration registers as
`sensor.pool_poolbrain_supervisor_reason`, `switch.pool_poolbrain_dry_run`,
`number.pool_poolbrain_min_temp` and so on — the device carries the name
**PoolBrain**. STORY §4 and `test_story_section_4_entity_ids` specify `*.pool_*`,
and that test is not wrong: a fresh install with the device named `Pool` really
does produce those ids. Both are true and they do not match.

`pool-overview-v2` and `Villa-Pool-Manual-v0.6.0.html` use the LIVE ids, because
that is what the owner's system answers to. **A fresh re-add of the integration
would produce the §4 ids and break every card on that dashboard.** Do not
"fix" either side without deciding which one is the contract.

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
- **F2 is never a NIGHT grid-heating band** (§3, narrowed by the 2026-09-18
  amendment — §5.4's daytime top-up ignores the band). Reason on *bands*, never
  on prices — the PUN index changes monthly.
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
  do not assume OFF. Whether a running machine is ADOPTED is
  `grid_conditions`' answer and nothing else — asked separately it missed
  §5.4's daytime window, then the 2026-09-18 band amendment, and a restart mid
  top-up re-derived OFF while the machine was genuinely heating.
  `restore_memory` also adopts a pump that is already in
  marcia — the 60 s debounce filters a *fresh transition*, and is not a reason
  to re-prove a steady state that predates the restart. Without that, every
  restart would write `off` → `heat` and put a spurious cycle on the compressor.
- **The COP model is diagnostic only.** One measured point (2.82 at ~18 °C
  air) and a Carnot-scaled slope, LOW confidence. Nothing in the control law
  reads it, and nothing should until it is calibrated against logged sessions.
- **Assign area `pool` to every entity/device** (owner rule). All entities hang
  off one service device so this is a single assignment.
- Cards go on `pool-overview-v2`. Do not touch `pool-overview`. That dashboard
  is **storage-mode and hand-built by the owner** — it is not generated from
  this repo, and a full-config replacement would throw away work that lives
  nowhere else. Add cards with `patch` against a fresh `config_hash`, record
  what was added in a `dashboard_*_cards.yaml` here, and write down the inverse.

## Known spec gaps found while building (owner input wanted)

These are recorded rather than silently resolved:

1. **§7.4's premise cannot fire with the §3 windows.** "Saturday 19:30 (F2):
   GRID is refused *even inside the grid window*" — but the grid window is
   23:00-07:00, so 19:30 is outside it and the band veto is never reached. The
   acceptance test therefore widens the grid window to 19:00 (they are
   owner-editable `time.*` entities) so the clause is genuinely exercised, and
   pins the band veto separately. See `TestCriterion04GridNeverInF2`. Since the
   2026-09-18 amendment the criterion is also narrower than it reads: it is
   about the night window, and 19:30 being outside the solar window is what
   keeps it exercising the veto at all.
2. **"Free hours" is unreachable with the default windows.** §5.3 wants chlorine
   enabled as soon as the PdC enters SOLAR "even before the chlorine window
   opens" — but the solar window (10-18) lies wholly inside the chlorine window
   (09-21), so that state cannot occur. The branch is implemented and tested
   with a narrowed chlorine window. Harmless, but it means the feature does
   nothing as configured.
3. ~~**Antifreeze in `manual`/`closed` mode.**~~ **RESOLVED 2026-09-17**: the
   owner amended §5.5 so antifreeze outranks both. Shipped in v0.4.0; see
   *Antifreeze outranks `manual` and `closed`* above. `maintenance` still wins.
4. **The SOLAR target has no hysteresis, by specification.** §5.2 stops SOLAR
   at `water >= solar_target` and restarts below it — one threshold, both ways.
   With the probe dithering on 28.0 that gives ~4 compressor starts over 5 h
   (bounded by MIN_OFF, so not runaway). Adding a band would fix it but would
   also stop the pool topping up on *free* sun between 27.5 and 28.0, so the
   trade is the owner's, not an engineering call. Unlike the GRID hysteresis bug
   (which was a real defect — the band existed and was being ignored), this is
   the spec working as written.
5. ~~**§5.4's daytime GRID top-up is NOT implemented.**~~ **CONFIRMED by the
   owner and shipped in v0.5.0**: `switch.pool_grid_day_topup`, default ON. See
   *The daytime grid top-up* below. It amends acceptance criterion §7.1, which
   is recorded in the STORY rather than quietly dropped.
6. **`sensor.pool_last_session_*` was specified and never built** — §4 lists it
   and §5.4 explains why it matters, and it was missing from v0.1.0 through
   v0.4.0 without ever being recorded as a gap. Shipped in v0.5.0. Worth
   remembering as a class of bug: the things §4 lists are easy to check off by
   eye and easy to miss one of.

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

## Before turning `switch.pool_dry_run` off — DONE 2026-09-17

All four steps below were completed by the owner, verified live on 2026-09-17:
both chlorinator automations are `off`, the safety cutoff and every
`pool_allerta_*` are `on`, and `pool_test_cop_notturno` is `off`. The
integration has been actuating since 20:04. Kept here as the checklist for any
future re-deploy.

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
- **§9's "flow at 30 %" is now load-bearing.** `binary_sensor.pool_pompa_in_marcia`
  needs flow >= `input_number.pool_portata_minima` (1 m3/h). If the pump at
  `antifreeze_speed` does not reach that, the sensor reads OFF while the pump is
  genuinely running — which the integration survives (nothing in the antifreeze
  path needs the confirmation) but which WILL make
  `automation.pool_allerta_pompa_ferma_da_24h` cry wolf through a cold snap.
  Measure the flow at 30 % at the first cold weather and tune
  `pool_portata_minima`, or raise `number.pool_antifreeze_speed`.
