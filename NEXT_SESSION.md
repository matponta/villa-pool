# Next session — kickstart prompts

## LIVE STATUS — read this before believing any "NOT YET DEPLOYED" below

Checked against the owner's Home Assistant on **2026-09-17 ~21:30**, and it
contradicts what the release notes under it say. **v0.4.0 is deployed and
actuating.**

- Config entry `01M2QBJM448GYF8R0R7K7VNSZ0`, `villa_pool`, state `loaded`,
  entities registered at 20:04 local. Confirmed as v0.4.0 by the presence of
  `binary_sensor.pool_poolbrain_antifreeze` and the `writes` / `last_write` /
  `latched` / `holding` attributes.
- **`switch.pool_poolbrain_dry_run` is OFF** — it is writing. At the time of the
  check: `writes: 2`, `last_write: switch.turn_off pump='off'`, `latched: []`,
  `holding: []`. Nothing contested, nothing deferred.
- The deploy checklist was followed: `pool_chlorinator_daily_3h_run` **off**,
  `pool_chlorinator_follows_pool_in_use` **off**, `pool_test_cop_notturno`
  **off**, the safety cutoff and every `pool_allerta_*` **on**.
- Owner-tuned settings, deliberately away from the shipped defaults: min temp
  **29.0** (default 27.0), solar target **30.0** (28.0), chlorine hours **7**
  (8). `sensor.pool_poolbrain_cover_closed_for` is `unavailable` — the cover
  sensor still does not exist, so every cover rule is still inert.

**This is a snapshot ~1.5 h after going live, not the 24 h comparison §8 step 1
asked for.** That comparison has still not been written up. The claim worth
checking over a full day is unchanged: every state change on
`switch.pompa_piscina`, `switch.clorinatore` and `climate.pool_pdc_piscina`
should be attributable to a `WRITE` line in the `villa_pool` log with its
reason, and to nothing else.

### The live entity ids are NOT the §4 ids

Live: `sensor.pool_poolbrain_supervisor_reason`,
`switch.pool_poolbrain_dry_run`, `number.pool_poolbrain_min_temp`, … The device
carries the name **PoolBrain**, so every id has that in it. STORY §4 and
`tests/test_engine.py::test_story_section_4_entity_ids` specify `*.pool_*`, and
that test passes because a fresh install with the device named `Pool` really
does produce those ids. **Both are true and they do not match.** The dashboard
and the manual use the live ids. A fresh re-add of the integration would produce
the §4 ids and break every card. Worth a deliberate decision rather than
discovering it during a reinstall.

---

## v0.6.0 — a restart mid daytime top-up (2026-09-18)

One defect, found by reading v0.5.0's diff back, plus a second one that the fix
for the first uncovered.

**`restore_memory` only knew the NIGHT grid window.** `engine._restore` handed
it `in_window(now, pdc_grid_start, pdc_grid_end)` — 23:00-07:00 — so a restart
at 15:00 during a §5.4 daytime top-up fell through to "nothing we recognise
authorises this" and re-derived `PDC_OFF` while the machine was genuinely
heating. The next tick entered GRID again as a *fresh* run. It pre-dates v0.5.0's
tag in the sense that it shipped with it, and it bites every daytime top-up, F1
weekdays included.

**What it actually cost**, since it is easy to over-state: nothing at the
compressor. The actuator is idempotent and the PdC was already on `heat` at the
same setpoint, so no command was sent and no cycle was added. What it cost was a
reset `pdc_since` and a session bracket that never happened.

**The fix is a shape change, not a second window check.** `restore_memory` now
takes `grid_window: str | None` — `pdc.grid_window()`'s own answer — instead of
`in_grid_window: bool`. That function is the one place that knows there are two
windows, so handing it the answer is what keeps them from drifting apart again.
The band veto still applies to BOTH windows, exactly as `grid_conditions` does.

**273 tests.**

### The band veto is NOT exempt for the daytime window

Worth writing down because it was assumed the other way when this was reported.
STORY §5.4 ("the F2 veto is untouched"), §3 ("F2: never") and `grid_conditions`
all agree: the veto is uniform. Exempting the daytime window in `restore_memory`
would have adopted GRID on Saturday at 15:00 (F2 07:00-23:00, inside the solar
window) — and the very next tick's `grid_conditions(running=True)` would have
refused it and called `_stop`, so the supervisor would have **stopped a run it
never began** and stamped a MIN_OFF the compressor had not earned. Pinned by
`TestRestartDuringADaytimeTopUp::test_f2_is_not_adopted_either`.

### Pre-tag adversarial review — one more real defect

1. **A restart that adopts a run brackets a session for it.** `session.py` says
   in its own docstring that "a run already in progress at startup is NOT
   adopted", and enforces it through `was_running` — which `engine.__init__`
   seeds `False` unconditionally. So the first tick after a restart that adopts
   a run reads as a false->true edge, opens a bracket at the restart instant,
   and on the night path publishes it as `clean`: a COP point measuring only the
   tail of a run. **This is a v0.5.0 defect on the night path already**, not
   something this release introduced — but the fix above would have extended it
   to every daytime top-up, which is how it surfaced. `_pdc_was_running` is now
   seeded from the restored memory. Pinned by
   `test_a_restart_mid_run_does_not_bracket_a_session`.

Also checked, and clean: the night path restores exactly as it did (`grid_window`
returns `night` wherever the old bool was True); `_restore` reads
`grid_day_topup` through the same settings path `grid_heating` and `min_temp`
already used, and the engine is started after the platforms, so the switch has
restored by then; `session.py`'s `in_night_window` still uses the night window
alone, which is what `all_night` means; nothing else re-derives a window.

**The pure tests could not have caught this one.** The bug was in `engine.py`'s
call, and every `supervisor/` test passed throughout. The test that fails
against the old wiring is `test_restart_mid_daytime_topup_resumes_it`, and it
has to assert on the *reason* — `sensor.pool_pdc_state` said GRID either way,
because the next tick started a new run. "heating on grid" (holding) vs "water
below minimum" (entering) is the whole difference.

### Still to do for this release

Nothing HA-side. No new entity, no dashboard card, no automation to retire — it
is a restart-path fix. Upgrade and restart.

---

## v0.5.0 — daytime grid top-up + the heating-session log (2026-09-17)

Two things the owner asked for on the same breath, and they turned out to
interact.

**`switch.pool_grid_day_topup`, default ON.** §5.4's proposal, confirmed. GRID
runs inside the SOLAR window when the sun is not there; the night window stays
as the fallback; the F2 veto is untouched, which is what keeps Saturday daytime
(F2 07:00-23:00) out even though the solar window is wide open. `grid_window()`
is the one place that decides which window authorises a run, and the reason line
names which — on entry *and* while it holds.

**`sensor.pool_last_session_*`** — specified in §4, explained in §5.4, and never
built. It was missing from v0.1.0 through v0.4.0 without ever being recorded as
a gap. Three sensors (COP, mean air, kWh), because fitting needs two recorded
series and an attribute is awkward to graph.

**They interact.** The session log marks a run `clean` only if it stayed wholly
in the night, because §5.4 says daylight contaminates it with solar gain. That
rule was cheap when GRID only ran at night — the day top-up is precisely what
makes daytime grid runs ordinary, so `all_night` is ANDed every tick rather than
inferred from the mode.

**265 tests.**

### What the replay measured

A September day, overcast, water starting 1.4 K low, with and without the
top-up:

| | senza | con |
|---|---|---|
| acqua a fine giornata | 24.67 °C | **26.14 °C** |
| avviamenti compressore | 1 | 2 |
| comandi `hvac_mode` | 3 | 5 |
| costo marginale | 0.0659 €/kWh_th | **0.0501 €/kWh_th** |
| spesa giornaliera | 4.61 € | **8.12 €** |

24 % cheaper per thermal kWh, consistent with §5.4's 30-45 % estimate (which
assumed colder October nights), for one extra compressor start — no churn.

**But the daily spend roughly doubles**, and that is the thing to say out loud:
the pool now reaches and holds the minimum instead of drifting below it, so it
delivers about twice the heat. Cheaper per kWh, more kWh. If the bill is ever
the complaint, the switch is the lever.

### Pre-tag adversarial review — two real findings

1. **A running top-up was indistinguishable from a night run.** The entry reason
   named it; the *holding* reason did not, and holding is what the owner
   actually reads an hour later. Two runs that cost meaningfully different
   amounts looked identical on the dashboard. Fixed in `_hold`, pinned by
   `test_and_keeps_saying_so_while_it_runs`.
2. **It changes acceptance criterion §7.1.** At 17:25 on a weekday, F1, water
   1.4 K low, the answer is no longer "PdC OFF". Rather than quietly editing the
   criterion, §7.1 is still pinned exactly as written (with the switch off,
   which is still a supported configuration) and the amended behaviour is pinned
   beside it. The same treatment was needed for §7.3, whose solar-dwell snapshot
   also sits below the minimum. Both amendments are recorded in the STORY,
   dated.

Also checked: the session log refuses to invent a COP in five distinct ways
(too short, ΔT inside the probe's resolution, missing water, missing energy, a
meter that did not move) and publishes the session anyway with a `note`; a run
already going at startup is not adopted; a polling gap does not chop one run
into several; the measured night of 16→17/9 comes back as 2.81 against the 2.82
in §1.

### Still to do for this release

`dashboard_v0.5.0_cards.yaml` is written but **not applied** — unlike the v0.4.0
cards, these reference entities that do not exist on the live system until
v0.5.0 is installed. Apply them after the upgrade.

---

## Dashboard cards + owner manual (2026-09-17) — no version bump

STORY §8 step 5. Documentation and dashboard only; it carried no change to
`custom_components/`, so no release and no tag.

STORY §8 step 5. Mostly delivery rather than code.

**The dashboard already existed** and is extensive and hand-built — this did not
rebuild it. Two cards were added to `pool-overview-v2`, for the surface v0.2.0
through v0.4.0 introduced and the dashboard predated:

1. **Attuazione** (controller section) — commands sent since start, the last one,
   and a warning naming any lever the supervisor has stopped driving (`latched`)
   or is holding back for a hydraulic reason (`holding`). This is the card to
   read when the pool is not doing what the reason line says it should.
2. **Antigelo attivo** (settings section) — `binary_sensor.pool_poolbrain_antifreeze`,
   which until v0.4.0 was only readable as an attribute.

Both applied live and verified: the template renders, and the cards resolve at
`views[0].sections[1].cards[2]` and `views[0].sections[8].cards[17]`. The
dashboard is storage-mode, so `dashboard_v0.4.0_cards.yaml` in this repo is the
versioned record — including the exact inverse, two JSON Patch removes, if it
ever has to come out.

**The manual** is `Villa-Pool-Manual-v0.4.0.html`, in Italian, matching the
villa-hvac convention (those were HTML printed to PDF from a browser — Skia/PDF,
8 pages). Print it to `Villa-Pool-Manual-v0.4.0.pdf` with Cmd-P → Save as PDF;
the print stylesheet is A4 with 16 mm margins and avoids breaking inside
callouts and tables. Eleven sections in hydraulic order, covering what the owner
actually needs at the pool house: the dry-run switch, the five modes, the
priority ladder, why "pump running" is never watts, the four PdC states and the
three rules that protect the compressor, the chlorinator's two overriding rules,
winter and antifreeze, how commands are sent, and a four-step "when something
looks wrong". Also published as an artifact for reading on a phone.

### Pre-tag adversarial review

Nothing found in the cards — they are additive, use existing card types, and the
Jinja was evaluated against the live system before it went in rather than after.
The findings of this step were all in the *discovery*, not the code: the repo
believed nothing had been deployed when in fact everything had, and the live
entity ids diverge from the §4 contract. Both are recorded above.

---

## v0.4.0 — winter mode and antifreeze (2026-09-17)

STORY §8 step 4, a month and a half early. The pure law for §5.1's winter slot
and the antifreeze latch shipped back in v0.1.0 and was already tested at the
§7.7 level, so this release is the live wiring, the visibility, an owner
decision and two real gaps. **229 tests.**

**The owner amended STORY §5.5.** Antifreeze now outranks `manual` and `closed`.
As written, rung 1 sat above antifreeze, so the supervisor stopped protecting
the pipes in `closed` — the mode the pool spends the whole winter in,
unattended, and therefore exactly when they are most at risk. In those two modes
a freeze runs the pump at `antifreeze_speed` and cuts the chlorinator (which §6
requires anyway before the speed may drop below 80 %), and leaves the PdC alone.
The amendment is recorded in the STORY itself, dated. `maintenance` is
deliberately unchanged and still freezes antifreeze too: someone is physically
at the pool, possibly with it drained, and it expires by itself after 4 h where
`closed` lasts months.

**New: `binary_sensor.pool_antifreeze`** (device class `cold`), carrying the
thresholds, the speed and `overrides_mode`. In winter it is the one thing worth
being able to see at a glance, and reading it out of an attribute on the reason
sensor is not glancing. New entity id, so it is a contract from here on.

**New: a push on both edges of the freeze latch** (owner's call), edge-triggered
like the hardware blocks — a freeze lasts days, not ticks.

### Pre-tag adversarial review — two real defects, and a replay

1. **Antifreeze released in a frozen mode left the pump running — for the rest
   of the winter.** The override starts the pump; the release reverted to
   "Frozen: mode closed — supervisor is not driving anything", which is
   hands-off and therefore never commands it back off. In `closed`, nobody is
   looking. Found by asking what the *end* of the episode looked like rather
   than the start. `Memory.antifreeze_owns_pump` now records that the pump is
   ours, and the stop stays asserted rather than firing once, so a command that
   does not land is still re-asserted. Handed back the moment the mode leaves
   the frozen set.

2. **A restart inside the hysteresis band silently dropped antifreeze.** The
   latch is history and `restore_memory` did not re-derive it, so at +1 °C —
   inside the 0..+2 band — a restart mid-cold-snap answered "not freezing" and
   stopped the pump. It is now re-derived against the RELEASE threshold, not
   the engage one: answering "yes" costs ~22 W until the air passes +2,
   answering "no" costs a stopped pump in a cold snap. Same asymmetry
   `antifreeze_step` already applies to an unknown temperature.

3. **Replayed a winter week offline through the law and the planner**, with the
   air swinging through zero every night plus a two-day hard freeze. Six
   antifreeze episodes, the shortest **10 h 42 m** — no chattering, which is
   what the 0/+2 band exists to prevent. In `closed`: **one pump command per
   transition** (11 for 6 episodes), one speed command and one chlorine command
   *for the whole week*, and the invariant "antifreeze off for >10 min implies
   the pump is off" held on every tick. In `winter`: 3 pump commands a day,
   which is the noon slot plus the nightly freeze — correct, not noise.

Also checked and found correct: the §6 speed guardrail applies in the antifreeze
path too (30 % waits until `switch.clorinatore` genuinely reads off, not merely
until it has been told to); the override sets `actuate=True` while the plain
freeze leaves it False; a frozen mode that never froze still drives nothing.

### A §9 open item that just became load-bearing

`binary_sensor.pool_pompa_in_marcia` requires flow >= `input_number.pool_portata_minima`
(1 m³/h). **Nobody has measured the flow at 30 %.** If it is below 1 m³/h the
sensor reads OFF while the pump is genuinely running. The integration survives
that — nothing in the antifreeze path needs the confirmation, the PdC is blocked
and the cell is off anyway — but `automation.pool_allerta_pompa_ferma_da_24h`
will cry wolf through every cold snap. Measure it at the first cold weather and
either tune `pool_portata_minima` or raise `number.pool_antifreeze_speed`
(it is tunable 30-80 precisely for this).

### What to watch on the first freezing night

1. **`binary_sensor.pool_antifreeze` goes on below 0 and off at +2**, not at
   +0.1. One push each way.
2. **The cell is cut BEFORE the speed drops.** In the log:
   `WRITE switch.turn_off ... clorinatore`, then `Holding off on the pump speed`
   until the relay reads off, then `WRITE number.set_value ... 30.0`. If the
   speed goes first, stop and revert.
3. **The episode ends.** When the air passes +2, `WRITE switch.turn_off` for the
   pump — including in `closed` mode. This is defect 1 above; it is the one
   worth checking by hand.
4. **`automation.pool_allerta_pompa_ferma_da_24h` does not fire** while the pump
   runs at 30 %. If it does, that is the §9 flow item, not the supervisor.

### Kickstart prompt for the next session

> Read `CLAUDE.md` then `STORY_POOL_CONTROLLER.md`. v0.1.0-v0.4.0 are built but
> **never deployed and never dry-run** — that is now the whole critical path,
> not a formality. Deploy, follow "Before turning the dry run off" in
> `CLAUDE.md`, and record the result under "Dry-run result" below. Then STORY
> §8 step 5: dashboard cards on `pool-overview-v2` (never `pool-overview`) plus
> the owner manual, villa-hvac style. The owner still owes answers on: the cover
> sensor entity id, §5.4's daytime GRID top-up (`switch.pool_grid_day_topup`,
> still PROPOSED), and the SOLAR target's lack of hysteresis. Measure the pump
> flow at 30 % before winter (§9) — it decides whether `pool_portata_minima`
> needs tuning.

---

## v0.3.0 — the PdC state machine actuates (2026-09-17) — superseded; see LIVE STATUS

STORY §8 step 3. The law is again unchanged; `PDC_ACTUATION_IMPLEMENTED` flips
to True and the climate levers are built. HA writes only `climate.set_hvac_mode`
(`heat`/`off`) and `climate.set_temperature` — the machine's own thermostat does
the rest (§5.2). `switch.pool_dry_run` still gates everything and is still ON by
default. **190 tests.**

**The setpoint goes out before `heat`**, so the machine never starts against
whatever target ran last (the owner's manual runs left it on 29). If the climate
entity reports no `temperature` attribute the setpoint is not written blind —
the mode still goes out and the setpoint follows on the tick after the attribute
appears.

**Three graces, and the difference between them matters.** A *repeat* of the
same `hvac_mode` is rate-limited to once per 15 min (= MIN_OFF; STORY §6's
"never write `hvac_mode` more than once per MIN_ON/MIN_OFF window"). The
setpoint gets 10 min — the bottom of §5.2's range and two cloud polls — because
it cycles nothing. A *change* of mind is never rate-limited at all, which is
what lets §7.5's "PdC `off` within one tick" hold even when the compressor grace
has just started.

### Pre-tag adversarial review — one real defect, and a replay

1. **The setpoint lever kept its state across a run.** `_pdc_targets` returned
   only the mode lever when the machine was not heating, so the setpoint lever
   was never cleared and carried its `writes` count from the last run into the
   next one. A lever left holding `writes=2` from yesterday evening latches
   itself on the *first* disagreement tonight — no re-assert, no second chance,
   and the machine silently left on the wrong target for the night. Both levers
   are now always built, the setpoint one with no opinion when it has none.

2. **Replayed a September night offline through the law *and* the planner** —
   the technique that caught the v0.1.0 short-cycle, and the one to re-run
   before any change to the machine or the write planner. 23:00→07:00 from
   25.6 °C gives **2 compressor starts and 5 commands** (heat, setpoint, off,
   heat, off). Then the same night with the PdC flickering `unavailable` for one
   minute in five plus a four-minute outage: **the same 5 commands, at the same
   minutes.** Zero extra. That is the release's central claim and it is now
   measured rather than argued. A partly-cloudy day gives 2 `hvac_mode`
   commands, 2 chlorine commands, 0 pump commands and 0 speed commands.

Also checked and found correct: `blocked` and `off` both resolve to one `off`
command (the difference between them is a reason, not an instruction); a fault
block notifies once, on the edge; the climate levers are not even constructed
during a polling gap, so the lever memories cannot drift inside one; a restart
against a machine already heating on the right target sends nothing at all
(§7.10's "never a spurious extra start", now as a command rather than a state).

### The behaviour change the owner should expect

**In `auto` mode the supervisor now owns the heat pump.** Starting it by hand
will be undone: commanded off, re-asserted 15 min later, and then the lever
latches and it is left alone with a notification. The escape hatches are
`select.pool_mode = manual` and `switch.pool_maintenance` — not winning the
argument. This is new, and it is the thing most likely to feel wrong on the
first evening.

### What to watch on the first live night

1. **`grep set_hvac_mode`** — two per heating run, no more. A third inside 15
   minutes means the grace regressed.
2. **`sensor.pool_pdc_state` across a polling gap.** `write_allowed` goes false
   while the climate entity is `unavailable`; the state must not move, and no
   command may appear in that window.
3. **The setpoint the machine actually ends up on** — 27.0 on a grid night,
   28.0 on solar. If it stays on 29 the `temperature` attribute is not readable
   and the setpoint is being (correctly) withheld; that needs the aquatemp fork
   looking at, not a code change.
4. **The first stop.** `set_hvac_mode -> off`, then `Holding off on the pump`
   for a few minutes while `pool_pdc_acceso` catches up, then the pump. If the
   pump is ever commanded off *before* the PdC, stop and revert.
5. **Phase A energy** over the night against `sensor.pool_cop_stimato` — §8
   step 3 asks for the first real GRID night observed with it.

### Kickstart prompt (v0.3.0 -> v0.4.0) — DONE, this is what v0.4.0 did

> Read `CLAUDE.md` then `STORY_POOL_CONTROLLER.md`. v0.2.0 and v0.3.0 are
> built but **not deployed and never dry-run** — do that first (see "Before
> turning the dry run off" in `CLAUDE.md`) and record the result under
> "Dry-run result" below. Then STORY §8 step 4: winter mode + antifreeze
> (§5.1 winter slot, the antifreeze latch, `pdc` blocked in winter) → v0.4.0,
> before November. The law for it already exists and is tested at the pure
> level — check what is missing is only the live wiring and the acceptance
> §7.7 end-to-end. While you are there, the owner still owes answers on: the
> cover sensor entity id, §5.4's daytime GRID top-up (`switch.pool_grid_day_topup`,
> still PROPOSED), the SOLAR target's lack of hysteresis, and whether
> antifreeze should survive `manual`/`closed` mode.

---

## v0.2.0 — the pump and the chlorinator actuate (2026-09-17) — superseded; see LIVE STATUS

STORY §8 step 2. The control law is unchanged; what is new is that a decision
can now become a service call. **`switch.pool_dry_run` is ON by default and is
what gates it**, so installing or upgrading still writes nothing until the owner
deliberately turns it off — which is logged at WARNING when it happens.

**Levers wired.** `switch.pompa_piscina` (on/off),
`number.pompa_piscina_manual_percentage_power` (speed),
`select.pompa_piscina_pump_mode` (forced to `Manual`, without which the
percentage means nothing — STORY §2), `switch.clorinatore` (enable). The PdC is
**not** wired: `actuator.PDC_ACTUATION_IMPLEMENTED` is False and the climate
levers are not constructed at all, so "leaving the PdC untouched" is structural
rather than a promise. Also new: a push notification on a *hardware* block (PdC
fault / pump problem — §7.5) and on a latched lever. Routine blocks such as
"pump not in marcia" never notify.

**The DST fix landed first**, as the v0.1.0 review required. `PoolState` now
carries two clocks: `now` (naive local) for windows and time-of-day, `mono`
(UTC) for every duration, with all of `Memory` stamped in the UTC frame. The
March transition otherwise makes a 10-minute-old stamp read as 70 minutes and
hands the compressor a restart MIN_OFF should have refused.

**How a write happens** is documented in `CLAUDE.md` → *How a write happens*:
idempotent, re-assert exactly once then latch and stop fighting, never judge an
unreadable device, and a hydraulic order inside each tick. **182 tests.**

### Pre-tag adversarial review — three real defects found and fixed

1. **A fresh lever re-commanded a pool that was already correct.** "Is this a
   new intent?" was evaluated before "does the device already agree?", so the
   very first tick after every restart, reload or dry-run flip would have sent
   `switch.turn_on` to a running pump and `switch.turn_on` to an enabled
   chlorinator — relay clicks and logbook entries for nothing, on exactly the
   event that happens most often. Found by
   `test_holding_the_same_agreement_never_writes_again`. The agreement check now
   comes first.
2. **The test that proved the dry run was dry could not have failed.**
   `async_mock_service` *replaces* a domain's service handler, and `switch`,
   `number` and `select` all register their own when the integration forwards
   its platforms — silently overwriting the mock. Every v0.1.0 "no service call
   is ever made" assertion was therefore passing because nothing was listening.
   Replaced with a bus listener on `EVENT_CALL_SERVICE`, which observes the real
   call and cannot be overwritten; the same tests now genuinely constrain the
   default configuration. This is the finding to remember: the v0.1.0 release
   note's central safety claim was, as tested, vacuous.
3. **`maintenance` would have switched the pool off.** Rung 1 of the ladder
   returns `pump_on=False, chlorine_on=False`, which reads as "turn everything
   off" to an actuator and as "freeze" to §4. The owner flips maintenance to
   work on the pool; stopping the pump under them is the opposite of the ask.
   `Decision.actuate=False` now means hands off every lever.

Three more guards were added in the same pass, none of them from a defect but
all from asking what happens when a command has *not* landed yet:

- the pump is never stopped while `binary_sensor.pool_pdc_acceso` reads on
  (v0.2.0 does not drive the PdC at all, so it may well be running on its own
  thermostat when our window closes);
- the speed is never dropped below 80 % while `switch.clorinatore` still reads
  on (§6, cell flow-switch minimum unknown);
- every write is bounded by `WRITE_TIMEOUT_S`, so one hung cloud call cannot
  hold the engine's lock and make the supervisor deaf.

Both hydraulic guards *defer* rather than cancel, and both fail towards more
flow.

Also checked and found correct: the latch clears on a new intent and on the
device agreeing again; a read gap neither writes nor counts against a device nor
resets the settle window; the stop order really does cut the cell before the
pump; `unload` still releases nothing.

### Before turning the dry run off — HA-side, NOT done from here (still open)

Retiring a live automation while the pool has no other chlorination control
would leave the cell unmanaged, so this waits for the owner:

1. Retire `automation.pool_chlorinator_daily_3h_run` and
   `automation.pool_chlorinator_follows_pool_in_use`. Left enabled they and the
   supervisor take turns on `switch.clorinatore`, and the supervisor will read
   the disagreement as a manual override and latch the lever — correctly, and
   uselessly.
2. Keep every `pool_allerta_*` and the low-circulation safety cutoff. They are
   independent watchdogs and never fight the supervisor.
3. Confirm `automation.pool_test_cop_notturno` is disabled (§6).
4. Run the 24 h dry comparison below first.

### What to watch in the first live hour

1. **One command, not sixty.** `grep WRITE` in the log: a handful of lines a
   day, each with its reason. A lever being commanded every 60 s means the
   device is not adopting it and the latch is not working.
2. **`sensor.pool_supervisor_reason` → `latched`.** Empty is correct. A lever
   in it is the supervisor saying "something else is driving this"; the two
   chlorinator automations are the likely culprit if step 1 above was skipped.
3. **The pump never stops under a running PdC.** `Holding off on the pump` in
   the log is the guard working, not a fault.
4. **`select.pompa_piscina_pump_mode` reads `Manual`** — if it does not, the
   speed writes are cosmetic.

---

## v0.1.0 — repo scaffold + dry-run supervisor (2026-09-17) — superseded; see LIVE STATUS

First release. Scaffolded from the `villa-hvac` skeleton (config-flow hub, one
engine tick, pure `supervisor/` modules, settings as the integration's own
restoring entities, pytest + freeze_time, STORY/CLAUDE.md discipline). Nothing
is shared with `villa-hvac` at runtime.

**What shipped.** The full pure control law of STORY §5 (pump follows demand,
PdC 4-state machine, chlorine-to-target, §5.5 priority ladder, COP model) plus
the whole HA surface: a config flow with an entity picker per §2 input, 17
`number.*` settings, 10 `time.*` windows, `select.pool_mode`, four switches, and
the diagnostic sensors. **131 tests**, green against the exact deploy target
(HA 2026.8.3 via `pytest-homeassistant-custom-component==0.13.357`).

**What did NOT ship: any ability to write.** There is no
`hass.services.async_call` anywhere in the component. `switch.pool_dry_run` (ON
by default) declares that rather than enforcing it; turning it off logs a
warning and still writes nothing. Six parametrised tests assert that no
`switch`/`climate`/`number`/`select` service is ever called under conditions
that would want all of them.

**Interpretation the owner should sanity-check.** §8 step 1 says "no actuation"
but also "log intended writes with reasons, run 24 h dry, compare with reality"
— which needs the controller to already be deciding. So the *law* is in v0.1.0
and only the *writes* are deferred. v0.2.0/v0.3.0 become "wire the writes for
these levers + retire the old automations" rather than "write the law". If the
owner wanted the law itself deferred, v0.1.0 is bigger than intended — but the
24 h dry run would then have had nothing to compare.

### Pre-tag adversarial review — four real defects found and fixed

1. **`filtration_only` left a phantom run in Memory.** The mode was enforced by
   overwriting the *answer* after `pdc_step` had already advanced Memory into
   GRID: the decision reported BLOCKED while memory said GRID, so the next tick
   would have resumed a run that was never announced, with MIN_ON and the
   restart re-derivation both keyed off a state the owner never saw. Fixed by
   putting the mode in the machine's own blocked set. Regression tests:
   `TestAdversarialRegressions::test_filtration_only_*`.
2. **A cover closed for exactly 0.0 h read as `unknown`** (falsy-vs-None in
   `CoverClosedForSensor`). Cosmetic, but it would have made the sensor look
   broken at the moment the cover shut.
3. **The PdC short-cycled all night.** `grid_conditions` is consulted both to
   START a run and to CONTINUE one, and it used the start threshold
   (`water < min_temp`) for both — so the run ended the moment the water touched
   27.0, drifted back below within MIN_OFF, and restarted. A simulated September
   night gave **~14 compressor starts instead of 2**, which is precisely the
   damage MIN_ON/MIN_OFF exist to prevent, and it would have been invisible in
   the dry run except as noise in the log. Found by replaying a full day through
   the law offline before tagging — not by any test, because every test asserted
   a single transition rather than a day's worth. `grid_conditions` now takes
   `running` and selects the correct side of the hysteresis band; two regression
   tests, one of which counts starts across a whole night.
4. **Every entity id was wrong.** Under `has_entity_name` the id prefix comes
   from the *device* name, so the entities landed as
   `switch.villa_pool_dry_run` / `sensor.villa_pool_supervisor_reason` while
   STORY §4 specifies `switch.pool_dry_run` / `sensor.pool_supervisor_reason` —
   the ids the owner's dashboards and automations will reference. Device renamed
   to `Pool`, entity names chosen to slugify onto the §4 ids, and all 36 ids
   pinned by a test.

Also checked and found correct: no service call reachable anywhere; the reason
line fits a 255-char HA state (96 chars worst case); `unavailable` never coerced
to `False`/`0` on any input; unload releases nothing.

### Known issues carried forward (none blocking a dry run)

- **DST.** The engine's timers use naive local datetimes. Across the October
  transition an hour repeats, so a `MIN_ON`/`MIN_OFF` comparison can go negative
  for that hour once a year. Harmless while dry, but **fix before v0.2.0
  actuates** — move the timers to UTC and keep local time only for window
  comparisons.
- **The SOLAR target has no hysteresis** (§5.2 as written). A probe dithering
  on 28.0 gives ~4 starts over 5 h, bounded by MIN_OFF. A band would fix it but
  would also forgo free solar top-up between 27.5 and 28.0 — owner's call.
- **`chlorine_hours_today` unavailable reads as 0.0**, i.e. "no progress", which
  errs towards more chlorination. Consider holding the last known value instead.
- Three STORY gaps are recorded in `CLAUDE.md` → *Known spec gaps*: §7.4's
  premise can't fire with the §3 windows; "free hours" is unreachable with the
  default windows; antifreeze is frozen by `manual`/`closed`.

### Deploy + the 24 h dry run

HACS custom repository → install v0.1.0 → restart → add the integration →
confirm the pickers → assign the *pool* area. Then leave it alone for 24 h.

First turn on INFO logging for the engine, or there is nothing to compare:

```yaml
logger:
  logs:
    custom_components.villa_pool: info
```

**What to verify, in rough priority order.**

1. **Nothing moved.** In the logbook for `switch.pompa_piscina`,
   `switch.clorinatore` and `climate.pool_pdc_piscina`, every state change over
   the 24 h must be attributable to an existing automation or to a human.
   `villa_pool` must appear nowhere. This is the release's core claim.
2. **The PdC never "changes state" across a cloud-polling gap.** Watch
   `sensor.pool_pdc_state`: its `write_allowed` attribute goes `false` whenever
   `climate.pool_pdc_piscina` is `unavailable`, and the state itself must NOT
   move during those gaps. This is the single most important behaviour to
   confirm, because it is the one that would cause double compressor starts
   once v0.2.0/v0.3.0 actuate.
3. **The night grid run is ONE run, not a sawtooth.** From 23:00, if the water
   is below 27.0 and the band is F3, `sensor.pool_pdc_state` should read `grid`
   continuously until the water reaches 27.5 — then `off`. Counting more than
   2-3 grid episodes in a night means the hysteresis regressed (see the v0.1.0
   review: that bug was real and is now tested).
4. **No grid intent in F2 ever** — 07:00-08:00 and 19:00-23:00 on weekdays,
   07:00-23:00 on Saturday. The reason line should say `band F2` if anything
   else would have wanted it.
5. **Pump intent tracks the window and the confirmation.** `would_pump_on`
   true across 08:00-20:00; `pump_confirmed` in the attributes turns true ~60 s
   after `binary_sensor.pool_pompa_in_marcia` goes on, not immediately.
6. **`binary_sensor.pool_solar_ok` does not flap.** ON only after 10 min above
   3000 W, OFF promptly below 2500 W. If it toggles more than a handful of times
   on a partly-cloudy day, the thresholds need revisiting before v0.3.0.
7. **Chlorine accounting.** `sensor.pool_chlorine_hours_missing` counts down as
   `sensor.salt_chlorinator_runtime_today` rises, and `would_chlorine_on` goes
   false at 8 h. It WILL disagree with
   `automation.pool_chlorinator_daily_3h_run` (12:00, 6 h) — that is expected,
   and that automation is what v0.2.0 retires.
8. **`sensor.pool_volume_today`** climbs ~8 m³/h while the pump runs at 80 %,
   its `turnovers` attribute ≈ volume/90, and it resets at midnight.
9. **`sensor.pool_cop_stimato`** ~2.8 in ~18 °C night air, ~3.5-3.8 in warm
   afternoon air. Diagnostic only — just check it is not absurd.
10. **Restart HA once** mid-run and confirm the settings come back and
    `sensor.pool_pdc_state` is re-derived rather than reset to `off`.
11. **Log volume.** A handful of `DRY-RUN` lines per day, each with a reason.
    Dozens means something is flapping — that is a finding, not noise.

### Kickstart prompt (v0.1.0 -> v0.2.0) — DONE, this is what v0.2.0 did

> Read `CLAUDE.md` then `STORY_POOL_CONTROLLER.md`. v0.1.0 ran 24 h dry —
> the log comparison is in this file under "Dry-run result". Now do STORY §8
> step 2: wire actuation for the **pump and chlorinator only** (§5.1, §5.3, the
> §5.5 ladder) behind `switch.pool_dry_run`, leaving the PdC untouched. Before
> writing any actuation, move the engine's timers from naive local time to UTC
> (the DST issue above). Add the idempotent write path — never fight a manual
> override, re-assert before concluding "manual", log every write with its
> reason — then retire `automation.pool_chlorinator_daily_3h_run` and
> `automation.pool_chlorinator_follows_pool_in_use`. Keep the `pool_allerta_*`
> automations: they are the owner's independent watchdogs. Ship as v0.2.0, one
> PR, tagged, with a pre-tag adversarial review recorded here.

### Dry-run result

_(still owed. The integration went live on 2026-09-17 at 20:04 without a written
24 h dry comparison — see LIVE STATUS at the top of this file for what was
verified instead, which is a snapshot rather than a day.)_

### Still open for the owner

- **The cover sensor entity id.** `binary_sensor.pool_telo_chiuso` is a
  placeholder and the picker is deliberately empty. Once the sensor is installed
  (20-21/9), set it in the options flow and wire
  `automation.pool_allerta_telo_aperto_chiudi_casa` (§1). Every cover rule is
  inert until then — including the 24 h chlorine cut-off.
- **§5.4's daytime GRID top-up** is still PROPOSED. Not implemented;
  `switch.pool_grid_day_topup` does not exist.
- Confirm `automation.pool_test_cop_notturno` is disabled before v0.2.0.
