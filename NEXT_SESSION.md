# Next session — kickstart prompts

## v0.1.0 — repo scaffold + dry-run supervisor (2026-09-17) — NOT YET DEPLOYED

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
- **`chlorine_hours_today` unavailable reads as 0.0**, i.e. "no progress", which
  errs towards more chlorination. Consider holding the last known value instead.
- Three STORY gaps are recorded in `CLAUDE.md` → *Known spec gaps*: §7.4's
  premise can't fire with the §3 windows; "free hours" is unreachable with the
  default windows; antifreeze is frozen by `manual`/`closed`.

### Deploy + the 24 h dry run

HACS custom repository → install v0.1.0 → restart → add the integration →
confirm the pickers → assign the *pool* area. Then leave it alone for 24 h.

**What to verify** — see the list handed to the owner with this release; the
short version is: the reason line always matches what the pool is actually
doing, the PdC intent never flickers across a cloud-polling gap, and the pump
intent tracks `pool_pompa_in_marcia` with the expected 60 s confirmation.

### Kickstart prompt for the next session

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

_(to be filled in after the 24 h run)_

### Still open for the owner

- **The cover sensor entity id.** `binary_sensor.pool_telo_chiuso` is a
  placeholder and the picker is deliberately empty. Once the sensor is installed
  (20-21/9), set it in the options flow and wire
  `automation.pool_allerta_telo_aperto_chiudi_casa` (§1). Every cover rule is
  inert until then — including the 24 h chlorine cut-off.
- **§5.4's daytime GRID top-up** is still PROPOSED. Not implemented;
  `switch.pool_grid_day_topup` does not exist.
- Confirm `automation.pool_test_cop_notturno` is disabled before v0.2.0.
