"""The ten acceptance criteria of STORY §7, one test class each.

Written BEFORE the control law (owner's instruction) — these are the contract
`supervisor/` has to satisfy, not a description of what it happens to do.

Each class names the criterion verbatim in its docstring so a failure points
straight back at the STORY. Criteria that are about HA plumbing rather than the
law (§7.5 notification, §7.9 no-write, §7.10 restart) are pinned here at the
pure level and again end-to-end in `test_engine.py`.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import time, timedelta

import pytest

from custom_components.villa_pool.const import (
    BAND_F2,
    BAND_F3,
    DEFAULT_ANTIFREEZE_SPEED,
    DEFAULT_FILTRATION_SPEED,
    DEFAULT_MIN_TEMP,
    MODE_WINTER,
    PDC_BLOCKED,
    PDC_GRID,
    PDC_OFF,
    PDC_SOLAR,
)
from custom_components.villa_pool.supervisor import decide
from tests.helpers import at, memory, state

MIN = timedelta(minutes=1)


def run(st, mem):
    """One tick."""
    return decide(st, mem)


def run_for(st, mem, minutes, step=1, **changes):
    """Replay `minutes` of ticks, applying `changes` to the state each tick.

    Returns the final (decision, memory). The law is a pure function of
    (state, memory), so a sequence of ticks is just a fold.
    """
    dec = None
    for i in range(0, minutes, step):
        st_i = st.with_now(st.now + i * MIN)
        if changes:
            st_i = replace(st_i, **changes)
        dec, mem = decide(st_i, mem)
    return dec, mem


# --- §7.1 --------------------------------------------------------------------

class TestCriterion01SnapshotOf16September:
    """1. With the 16/9 17:25 snapshot (water 25.6, headroom 0 W, 17:25, F1 on a
    weekday, pool_in_use on) the PdC is OFF (not in a window / no solar), the
    pump is ON at 80 % (pool_in_use), chlorine enabled."""

    def snapshot(self):
        """The criterion exactly as the owner wrote it.

        `grid_day_topup=False` is what the pool was when §7 was written — the
        daytime top-up did not exist. It is still a supported configuration (the
        switch), so this keeps testing the criterion rather than quietly
        retiring it; `test_the_daytime_top_up_changes_this_criterion` below pins
        what the 2026-09-17 amendment does to the same snapshot.
        """
        now = at(2026, 9, 16, 17, 25)      # Wednesday
        return state(now, water_temp=25.6, headroom_w=0.0, band="F1",
                     pool_in_use=True, grid_day_topup=False), memory(now)

    def test_pdc_is_off(self):
        st, mem = self.snapshot()
        dec, _ = run(st, mem)
        assert dec.pdc_state == PDC_OFF

    def test_pump_on_at_filtration_speed(self):
        st, mem = self.snapshot()
        dec, _ = run(st, mem)
        assert dec.pump_on is True
        assert dec.pump_speed == DEFAULT_FILTRATION_SPEED

    def test_pool_in_use_is_the_reason_the_pump_runs(self):
        st, mem = self.snapshot()
        dec, _ = run(st, mem)
        assert "pool_in_use" in dec.requesters

    def test_chlorine_enabled(self):
        st, mem = self.snapshot()
        dec, _ = run(st, mem)
        assert dec.chlorine_on is True

    def test_reason_is_one_readable_line(self):
        st, mem = self.snapshot()
        dec, _ = run(st, mem)
        assert dec.reason
        assert "\n" not in dec.reason

    def test_the_daytime_top_up_changes_this_criterion(self):
        """§5.4 amendment, owner-confirmed 2026-09-17.

        17:25 on a weekday is F1 and inside the solar window, and the water is
        1.4 K below the guaranteed minimum. With the top-up on, "the PdC is OFF"
        is no longer the right answer — heating now costs 30-45 % less per
        thermal kWh than waiting for 23:00, which is the whole point of the
        amendment. This is a deliberate change to an acceptance criterion, not
        a regression.
        """
        now = at(2026, 9, 16, 17, 25)
        st = state(now, water_temp=25.6, headroom_w=0.0, band="F1",
                   pool_in_use=True, grid_day_topup=True)
        dec, _ = run(st, memory(now))
        assert dec.pdc_state == PDC_GRID
        assert dec.pdc_setpoint == DEFAULT_MIN_TEMP
        assert "daytime top-up" in dec.reason


# --- §7.2 --------------------------------------------------------------------

class TestCriterion02GridNightRun:
    """2. Same day 23:00 (F3, grid window, grid_heating on): PdC -> GRID,
    setpoint 27; at 27.5 -> OFF; pump post-run 5 min then follows other
    demand."""

    def snapshot(self, water=25.6):
        now = at(2026, 9, 16, 23, 0)
        return state(now, water_temp=water, band=BAND_F3, headroom_w=0.0,
                     grid_heating=True), memory(now)

    def test_enters_grid(self):
        st, mem = self.snapshot()
        dec, _ = run(st, mem)
        assert dec.pdc_state == PDC_GRID

    def test_grid_setpoint_is_min_temp(self):
        st, mem = self.snapshot()
        dec, _ = run(st, mem)
        assert dec.pdc_setpoint == DEFAULT_MIN_TEMP

    def test_pump_runs_for_the_pdc(self):
        st, mem = self.snapshot()
        dec, _ = run(st, mem)
        assert dec.pump_on is True
        assert "pdc" in dec.requesters

    def test_stops_at_min_temp_plus_hysteresis(self):
        st, mem = self.snapshot()
        _, mem = run(st, mem)
        # An hour of heating later the water has reached 27.5.
        later = st.now + timedelta(hours=1)
        dec, _ = run(state(later, water_temp=27.5, band=BAND_F3,
                           grid_heating=True), mem)
        assert dec.pdc_state == PDC_OFF

    def test_pump_keeps_running_for_the_post_run_then_stops(self):
        st, mem = self.snapshot()
        _, mem = run(st, mem)
        later = st.now + timedelta(hours=1)
        hot = state(later, water_temp=27.5, band=BAND_F3, grid_heating=True)
        dec, mem = run(hot, mem)
        # Immediately after the PdC stops the pump is held on post-run.
        assert dec.pump_on is True
        assert "postrun" in dec.requesters
        # Four minutes in, still held.
        dec, mem = run(hot.with_now(later + 4 * MIN), mem)
        assert dec.pump_on is True
        # Past five minutes nothing else wants it at 00:05, so it stops.
        dec, _ = run(hot.with_now(later + 6 * MIN), mem)
        assert dec.pump_on is False


# --- §7.3 --------------------------------------------------------------------

class TestCriterion03SolarDwellAndMinOn:
    """3. A 3 kW headroom pulse of 5 min does not start the PdC; 10 min does; a
    12 min cloud does not stop it before MIN_ON."""

    def sunny(self, headroom=3000.0):
        """The dwell criterion, isolated from the grid path.

        The water is below the guaranteed minimum here, so with the 2026-09-17
        daytime top-up the machine would start on GRID whatever the sun is
        doing — and this criterion is about the SOLAR entry dwell, not about
        whether something else also wants to heat. Turning the top-up off is
        what keeps the test answering the question it was written to ask;
        `TestDaytimeGridTopUp` covers the other one.
        """
        now = at(2026, 9, 16, 12, 0)       # inside the 10-18 solar window
        return state(now, water_temp=26.0, headroom_w=headroom, band="F1",
                     grid_day_topup=False), memory(now)

    def test_five_minute_pulse_does_not_start_the_pdc(self):
        st, mem = self.sunny()
        dec, _ = run_for(st, mem, minutes=5)
        assert dec.pdc_state == PDC_OFF

    def test_ten_minute_pulse_starts_the_pdc(self):
        st, mem = self.sunny()
        dec, _ = run_for(st, mem, minutes=11)
        assert dec.pdc_state == PDC_SOLAR

    def test_solar_setpoint_is_the_solar_target(self):
        st, mem = self.sunny()
        dec, _ = run_for(st, mem, minutes=11)
        assert dec.pdc_setpoint == st.config.solar_target_temp

    def test_twelve_minute_cloud_does_not_stop_it(self):
        st, mem = self.sunny()
        _, mem = run_for(st, mem, minutes=11)
        cloudy = replace(st.with_now(st.now + 11 * MIN), headroom_w=0.0)
        dec, _ = run_for(cloudy, mem, minutes=12)
        assert dec.pdc_state == PDC_SOLAR


# --- §7.4 --------------------------------------------------------------------

class TestCriterion04GridNeverInF2:
    """4. Saturday 19:30 (F2): GRID is refused even inside the grid window;
    `reason` says `band F2`.

    NOTE (owner): with the §3 default windows 19:30 is NOT inside the grid
    window (23:00-07:00), so the criterion as written cannot fire. The windows
    are owner-editable `time.*` entities, so the end-to-end case below widens
    the grid window to 19:00 to put 19:30 genuinely inside it — that is the only
    way to exercise "refused *even inside* the window". The band veto itself is
    pinned independently.

    NOTE 2 (amendment 2026-09-18): the criterion is now about the NIGHT grid
    window specifically. §5.4's daytime top-up is exempt from the band, so
    "GRID is never in F2" is no longer true of the pool as a whole — 19:30 is
    outside the solar window, which is what keeps every case below honest.
    """

    def test_f2_is_refused_inside_a_grid_window_that_covers_1930(self):
        now = at(2026, 9, 19, 19, 30)      # Saturday -> F2 on the ARERA calendar
        st = state(now, water_temp=25.0, band=BAND_F2, grid_heating=True,
                   headroom_w=0.0, pdc_grid_start=time(19, 0))
        dec, _ = run(st, memory(now))
        assert dec.pdc_state == PDC_OFF

    def test_the_reason_names_the_band(self):
        now = at(2026, 9, 19, 19, 30)
        st = state(now, water_temp=25.0, band=BAND_F2, grid_heating=True,
                   headroom_w=0.0, pdc_grid_start=time(19, 0))
        dec, _ = run(st, memory(now))
        assert "band F2" in dec.reason

    def test_the_same_moment_in_f3_would_have_heated(self):
        """Control: only the band differs."""
        now = at(2026, 9, 19, 19, 30)
        st = state(now, water_temp=25.0, band=BAND_F3, grid_heating=True,
                   headroom_w=0.0, pdc_grid_start=time(19, 0))
        dec, _ = run(st, memory(now))
        assert dec.pdc_state == PDC_GRID


# --- §7.5 --------------------------------------------------------------------

class TestCriterion05PumpFaultBlocksThePdC:
    """5. Pump reported `problem` while PdC in SOLAR -> PdC `off` within one
    tick, chlorine off, notification, state BLOCKED with reason."""

    def in_solar(self):
        now = at(2026, 9, 16, 12, 0)
        st = state(now, water_temp=26.0, headroom_w=3000.0)
        dec, mem = run_for(st, memory(now), minutes=11)
        assert dec.pdc_state == PDC_SOLAR
        return st.with_now(st.now + 11 * MIN), mem

    def faulted(self):
        st, mem = self.in_solar()
        broken = replace(st, pump_problem=True)
        return run(broken, mem)

    def test_pdc_goes_blocked_in_one_tick(self):
        dec, _ = self.faulted()
        assert dec.pdc_state == PDC_BLOCKED

    def test_chlorine_is_cut(self):
        dec, _ = self.faulted()
        assert dec.chlorine_on is False

    def test_reason_names_the_pump_fault(self):
        dec, _ = self.faulted()
        assert dec.blocked_reason
        assert "pump" in dec.reason.lower()


# --- §7.6 --------------------------------------------------------------------

class TestCriterion06CoverClosed25Hours:
    """6. Cover closed 25 h -> chlorine off even with pool_in_use; reopen ->
    resumes."""

    def test_chlorine_off_after_25_h_closed_even_when_in_use(self):
        now = at(2026, 9, 16, 12, 0)
        st = state(now, cover_closed=True, cover_closed_for_h=25.0,
                   pool_in_use=True)
        dec, _ = run(st, memory(now))
        assert dec.chlorine_on is False

    def test_reason_names_the_cover(self):
        now = at(2026, 9, 16, 12, 0)
        st = state(now, cover_closed=True, cover_closed_for_h=25.0,
                   pool_in_use=True)
        dec, _ = run(st, memory(now))
        assert "cover" in dec.reason.lower()

    def test_reopening_resumes_chlorine(self):
        now = at(2026, 9, 16, 12, 0)
        st = state(now, cover_closed=False, cover_closed_for_h=0.0,
                   pool_in_use=True)
        dec, _ = run(st, memory(now))
        assert dec.chlorine_on is True

    def test_23_hours_closed_is_still_fine(self):
        now = at(2026, 9, 16, 12, 0)
        st = state(now, cover_closed=True, cover_closed_for_h=23.0)
        dec, _ = run(st, memory(now))
        assert dec.chlorine_on is True

    def test_absent_cover_sensor_never_cuts_chlorine(self):
        """The sensor is not installed yet (STORY §1) — unknown is not closed."""
        now = at(2026, 9, 16, 12, 0)
        st = state(now, cover_closed=None, cover_closed_for_h=None)
        dec, _ = run(st, memory(now))
        assert dec.chlorine_on is True


# --- §7.7 --------------------------------------------------------------------

class TestCriterion07WinterAntifreeze:
    """7. Winter mode, outdoor -1 °C at 03:00 -> pump ON at antifreeze_speed,
    chlorine off; outdoor +2.5 -> pump off (unless winter 12:00 slot)."""

    def test_pump_runs_at_antifreeze_speed(self):
        now = at(2026, 12, 15, 3, 0)
        st = state(now, mode=MODE_WINTER, outdoor_temp=-1.0, water_temp=8.0)
        dec, _ = run(st, memory(now))
        assert dec.pump_on is True
        assert dec.pump_speed == DEFAULT_ANTIFREEZE_SPEED

    def test_chlorine_is_off_during_antifreeze(self):
        now = at(2026, 12, 15, 3, 0)
        st = state(now, mode=MODE_WINTER, outdoor_temp=-1.0, water_temp=8.0)
        dec, _ = run(st, memory(now))
        assert dec.chlorine_on is False

    def test_release_at_plus_two_point_five_stops_the_pump(self):
        now = at(2026, 12, 15, 3, 0)
        cold = state(now, mode=MODE_WINTER, outdoor_temp=-1.0, water_temp=8.0)
        _, mem = run(cold, memory(now))
        warm = state(now + 30 * MIN, mode=MODE_WINTER, outdoor_temp=2.5,
                     water_temp=8.0)
        dec, _ = run(warm, mem)
        assert dec.pump_on is False

    def test_antifreeze_holds_between_zero_and_the_release(self):
        """Hysteresis: once engaged it stays until +2, not at +0.1."""
        now = at(2026, 12, 15, 3, 0)
        cold = state(now, mode=MODE_WINTER, outdoor_temp=-1.0, water_temp=8.0)
        _, mem = run(cold, memory(now))
        lukewarm = state(now + 30 * MIN, mode=MODE_WINTER, outdoor_temp=1.0,
                         water_temp=8.0)
        dec, _ = run(lukewarm, mem)
        assert dec.pump_on is True

    def test_winter_noon_slot_still_runs_the_pump_at_filtration_speed(self):
        now = at(2026, 12, 15, 12, 30)
        st = state(now, mode=MODE_WINTER, outdoor_temp=5.0, water_temp=8.0)
        dec, _ = run(st, memory(now))
        assert dec.pump_on is True
        assert dec.pump_speed == DEFAULT_FILTRATION_SPEED

    def test_winter_noon_slot_ends_after_winter_hours(self):
        now = at(2026, 12, 15, 14, 30)     # 12:00 + 2 h
        st = state(now, mode=MODE_WINTER, outdoor_temp=5.0, water_temp=8.0)
        dec, _ = run(st, memory(now))
        assert dec.pump_on is False

    def test_pdc_is_blocked_in_winter(self):
        now = at(2026, 12, 15, 12, 30)
        st = state(now, mode=MODE_WINTER, outdoor_temp=5.0, water_temp=8.0)
        dec, _ = run(st, memory(now))
        assert dec.pdc_state == PDC_BLOCKED


# --- §7.8 --------------------------------------------------------------------

class TestCriterion08ChlorineTargetCatchUp:
    """8. `hours_today` 5 at 19:00 with target 8 -> pump + chlorine stay on; at
    8 h -> off."""

    def test_below_target_keeps_pump_and_chlorine_on(self):
        now = at(2026, 9, 16, 19, 0)
        st = state(now, chlorine_hours_today=5.0)
        dec, _ = run(st, memory(now))
        assert dec.chlorine_on is True
        assert dec.pump_on is True

    def test_at_target_both_go_off(self):
        now = at(2026, 9, 16, 19, 0)
        st = state(now, chlorine_hours_today=8.0)
        dec, _ = run(st, memory(now))
        assert dec.chlorine_on is False

    def test_catch_up_extends_the_pump_past_its_window(self):
        """After the 19:00 deadline, missing hours keep pump+chlorine running
        past the 20:00 pump window (STORY §5.3)."""
        now = at(2026, 9, 16, 20, 30)
        st = state(now, chlorine_hours_today=5.0)
        dec, _ = run(st, memory(now))
        assert dec.pump_on is True
        assert dec.chlorine_on is True
        assert "catchup" in dec.requesters

    def test_catch_up_outlives_the_chlorine_window_until_midnight(self):
        """STORY §5.3: catch-up keeps pump + chlorine on "until target or
        midnight" — so it deliberately runs past the 21:00 window end."""
        now = at(2026, 9, 16, 21, 30)
        st = state(now, chlorine_hours_today=5.0)
        dec, _ = run(st, memory(now))
        assert dec.chlorine_on is True

    def test_catch_up_is_over_after_midnight(self):
        """Past midnight the deadline is in the future again and the daily
        counter has reset: no catch-up, and 00:30 is outside the window."""
        now = at(2026, 9, 17, 0, 30)
        st = state(now, chlorine_hours_today=0.0)
        dec, _ = run(st, memory(now))
        assert dec.chlorine_on is False

    def test_target_control_off_ignores_the_target(self):
        """The escape hatch: switch.pool_chlorine_target_control off means
        chlorine follows the WINDOW only — so being at target no longer stops
        it."""
        now = at(2026, 9, 16, 19, 0)
        st = state(now, chlorine_hours_today=8.0, chlorine_target_control=False)
        dec, _ = run(st, memory(now))
        assert dec.chlorine_on is True

    def test_target_control_off_has_no_catch_up(self):
        """...and equally, no target means nothing to catch up on: the window
        end is the end."""
        now = at(2026, 9, 16, 21, 30)
        st = state(now, chlorine_hours_today=5.0, chlorine_target_control=False)
        dec, _ = run(st, memory(now))
        assert dec.chlorine_on is False


# --- §7.9 --------------------------------------------------------------------

class TestCriterion09PdCUnavailableIsNotAStateChange:
    """9. PdC entities `unavailable` for 4 min -> no state change, no write."""

    def in_grid(self):
        now = at(2026, 9, 16, 23, 0)
        st = state(now, water_temp=25.6, band=BAND_F3, grid_heating=True)
        dec, mem = run(st, memory(now))
        assert dec.pdc_state == PDC_GRID
        return st, mem

    def test_state_is_unchanged_through_the_gap(self):
        st, mem = self.in_grid()
        gone = replace(st, pdc_available=False, pdc_running=None)
        dec, mem = run_for(gone, mem, minutes=4)
        assert dec.pdc_state == PDC_GRID

    def test_no_write_is_issued_during_the_gap(self):
        st, mem = self.in_grid()
        gone = replace(st, pdc_available=False, pdc_running=None)
        dec, _ = run_for(gone, mem, minutes=4)
        assert dec.pdc_write is False

    def test_unavailable_is_never_read_as_off(self):
        """The failure this rule exists to prevent: a polling gap looking like a
        stop, and the law 'restarting' a machine that never stopped."""
        st, mem = self.in_grid()
        gone = replace(st, pdc_available=False, pdc_running=None)
        _, mem2 = run_for(gone, mem, minutes=4)
        assert mem2.pdc_state == PDC_GRID
        assert mem2.pdc_since == mem.pdc_since       # the run was not restarted


# --- §7.10 -------------------------------------------------------------------

class TestCriterion10RestartDuringGrid:
    """10. Restart HA at 01:00 while GRID active -> after restart the state is
    GRID again (or OFF if water >= 27.5), never a spurious extra start."""

    def test_state_is_re_derived_as_grid(self):
        """After a restart the latches are empty; the law must re-derive GRID
        from the live machine + water temp, not assume OFF (STORY §6)."""
        from custom_components.villa_pool.supervisor import restore_memory

        now = at(2026, 9, 17, 1, 0)
        mem = restore_memory(mono=now, pdc_running=True, pump_running=True,
                             water_temp=26.0,
                             min_temp=DEFAULT_MIN_TEMP, grid_ok=True,
                             solar_ok=False)
        assert mem.pdc_state == PDC_GRID

    def test_re_derived_state_is_off_once_the_water_is_hot_enough(self):
        from custom_components.villa_pool.supervisor import restore_memory

        now = at(2026, 9, 17, 1, 0)
        mem = restore_memory(mono=now, pdc_running=False, pump_running=True,
                             water_temp=27.6,
                             min_temp=DEFAULT_MIN_TEMP, grid_ok=True,
                             solar_ok=False)
        assert mem.pdc_state == PDC_OFF

    def test_restart_does_not_produce_a_second_start(self):
        """A re-derived GRID must carry a `pdc_since` in the past, so MIN_ON is
        already satisfied and the next tick is a continuation, not a start."""
        from custom_components.villa_pool.supervisor import restore_memory

        now = at(2026, 9, 17, 1, 0)
        mem = restore_memory(mono=now, pdc_running=True, pump_running=True,
                             water_temp=26.0,
                             min_temp=DEFAULT_MIN_TEMP, grid_ok=True,
                             solar_ok=False)
        st = state(now, water_temp=26.0, band=BAND_F3, grid_heating=True)
        dec, mem2 = run(st, mem)
        assert dec.pdc_state == PDC_GRID
        assert mem2.pdc_since == mem.pdc_since     # same run, not a new one

    @pytest.mark.parametrize("running,water,expected", [
        (True, 26.0, PDC_GRID),
        (False, 26.0, PDC_OFF),
        (True, 27.6, PDC_OFF),      # hot enough: the run is over whatever the relay says
    ])
    def test_re_derivation_matrix(self, running, water, expected):
        from custom_components.villa_pool.supervisor import restore_memory

        now = at(2026, 9, 17, 1, 0)
        mem = restore_memory(mono=now, pdc_running=running, water_temp=water,
                             min_temp=DEFAULT_MIN_TEMP, grid_ok=True,
                             solar_ok=False)
        assert mem.pdc_state == expected


# --- §5.4 amendment, owner-confirmed 2026-09-17 ------------------------------

class TestDaytimeGridTopUp:
    """GRID inside the SOLAR window when the sun is not there.

    §5.4 proposed it and left it for the owner: the air is 8-10 K warmer by day,
    so the same thermal kWh costs an estimated 30-45 % less than at 23:00, and
    F1 and F3 are within a cent of each other. Confirmed 2026-09-17, default ON.

    The daily logic it produces: reach the guaranteed minimum by evening;
    if that did not happen, the night window is still there as a fallback.

    **AMENDED 2026-09-18 (owner): the top-up ignores the tariff band.** It ran
    in F1 only, which with the default windows meant "not on Saturdays" and
    nothing else. The night window keeps §3's F2 veto.
    """

    def midday(self, water=25.6, band="F1", topup=True, headroom=0.0, day=16):
        now = at(2026, 9, day, 12, 0)      # inside the 10-18 solar window
        return state(now, water_temp=water, headroom_w=headroom, band=band,
                     grid_day_topup=topup), memory(now)

    def test_it_heats_at_midday_with_no_sun(self):
        st, mem = self.midday()
        dec, _ = run(st, mem)
        assert dec.pdc_state == PDC_GRID
        assert dec.pdc_setpoint == DEFAULT_MIN_TEMP

    def test_the_reason_says_it_is_the_daytime_one(self):
        """The owner has to be able to tell a top-up from a night run at a
        glance, because they cost very different amounts."""
        st, mem = self.midday()
        dec, mem = run(st, mem)
        assert "daytime top-up" in dec.reason

    def test_and_keeps_saying_so_while_it_runs(self):
        """The entry reason scrolls past in one tick. What the owner actually
        reads, an hour later, is the holding one."""
        st, mem = self.midday()
        dec, mem = run(st, mem)
        dec, _ = run(st.with_now(st.now + 60 * MIN), mem)
        assert dec.pdc_state == PDC_GRID
        assert "daytime top-up" in dec.reason

    def test_the_switch_off_restores_the_night_only_behaviour(self):
        st, mem = self.midday(topup=False)
        dec, _ = run(st, mem)
        assert dec.pdc_state == PDC_OFF
        assert "outside grid window" in dec.reason

    def test_f2_no_longer_refuses_the_day_topup(self):
        """AMENDMENT 2026-09-18 (owner) — the top-up ignores the band.

        This test asserted the exact opposite through v0.5.0, and the inversion
        IS the change. Saturday is F2 from 07:00 to 23:00, so the solar window
        sits inside the expensive band all day: with the default windows the
        veto bought the pool one cold day a week and nothing else. F2 is ~17 %
        dearer per electrical kWh; daytime air buys ~24 % more heat per kWh.
        Waiting for 23:00 was never the cheaper answer, only the colder one.
        """
        st, mem = self.midday(band=BAND_F2, day=19)      # Saturday
        dec, _ = run(st, mem)
        assert dec.pdc_state == PDC_GRID
        assert dec.pdc_setpoint == DEFAULT_MIN_TEMP

    def test_an_f2_top_up_says_which_band_it_is_paying_for(self):
        """Exempt is not the same as hidden. This is the dearest electrical kWh
        the pool buys and the reason line is the only place it shows — on
        entry AND while it holds, for the same reason the daytime label itself
        is repeated."""
        st, mem = self.midday(band=BAND_F2, day=19)
        dec, mem = run(st, mem)
        assert "daytime top-up (band F2)" in dec.reason
        dec, _ = run(st.with_now(st.now + 60 * MIN), mem)
        assert dec.pdc_state == PDC_GRID
        assert "daytime top-up (band F2)" in dec.reason

    def test_the_night_window_still_refuses_f2(self):
        """The amendment is scoped to the daytime window. At 19:30 the air is
        not the daytime air, so the band is the only thing that varies and §3's
        veto stands — §7.4, which this must not have quietly retired."""
        now = at(2026, 9, 19, 19, 30)                    # Saturday → F2
        st = state(now, water_temp=25.0, band=BAND_F2, grid_heating=True,
                   headroom_w=0.0, pdc_grid_start=time(19, 0))
        dec, _ = run(st, memory(now))
        assert dec.pdc_state == PDC_OFF
        assert "band F2" in dec.reason

    def test_the_exemption_follows_the_day_window_not_the_label(self):
        """The windows are owner-editable and can be made to overlap. What
        lifts the veto is that the DAY window authorises — not which of the two
        `grid_window` happens to name first, which is still the night one."""
        now = at(2026, 9, 19, 16, 0)                     # Saturday, F2, no sun
        st = state(now, water_temp=25.6, headroom_w=0.0, band=BAND_F2,
                   pdc_grid_start=time(15, 0))           # night window covers 16:00
        dec, _ = run(st, memory(now))
        assert dec.pdc_state == PDC_GRID

    def test_free_sun_still_wins(self):
        """The top-up is a fallback for when SOLAR conditions fail, never a
        reason to burn grid while the sun is on the panels."""
        st, mem = self.midday(headroom=4000.0)
        dec, mem = run_for(st, mem, minutes=11)
        assert dec.pdc_state == PDC_SOLAR
        assert dec.pdc_setpoint == st.config.solar_target_temp

    def test_the_sun_takes_over_from_a_running_top_up(self):
        st, mem = self.midday()
        dec, mem = run(st, mem)
        assert dec.pdc_state == PDC_GRID
        sunny = replace(st, headroom_w=4000.0)
        dec, _ = run_for(sunny, mem, minutes=12)
        assert dec.pdc_state == PDC_SOLAR

    def test_it_stops_on_the_same_hysteresis_as_the_night_run(self):
        st, mem = self.midday()
        _, mem = run(st, mem)
        hot = replace(st.with_now(st.now + 60 * MIN), water_temp=27.5)
        dec, _ = run(hot, mem)
        assert dec.pdc_state == PDC_OFF

    def test_it_does_not_run_once_the_minimum_is_met(self):
        st, mem = self.midday(water=27.2)
        dec, _ = run(st, mem)
        assert dec.pdc_state == PDC_OFF

    def test_outside_both_windows_nothing_changes(self):
        """08:00 is inside the pump window but outside solar (10-18) and outside
        the night grid window — no top-up there."""
        now = at(2026, 9, 16, 8, 30)
        st = state(now, water_temp=25.6, headroom_w=0.0, band="F1")
        dec, _ = run(st, memory(now))
        assert dec.pdc_state == PDC_OFF

    def test_the_night_window_is_untouched(self):
        """The fallback §5.4 keeps: still GRID at 23:00 in F3."""
        now = at(2026, 9, 16, 23, 0)
        st = state(now, water_temp=25.6, headroom_w=0.0, band=BAND_F3)
        dec, _ = run(st, memory(now))
        assert dec.pdc_state == PDC_GRID
        assert "grid window" in dec.reason


# --- §7.10 x §5.4 ------------------------------------------------------------

class TestRestartDuringADaytimeTopUp:
    """§7.10 applied to the window §5.4 added: a restart at 15:00 mid top-up.

    §7.10 is written about 01:00 because the night window was the only one a
    grid run could happen in when it was written. `restore_memory` was still
    being handed the night window alone, so a restart mid top-up re-derived OFF
    while the machine was genuinely heating, and the next tick entered GRID
    again as a fresh run — bounded at the relay (the actuator is idempotent and
    the PdC is already on `heat` at the same setpoint, so NOT an extra
    compressor cycle) but it reset `pdc_since` and bracketed a session that
    never happened.

    The fix is that `restore_memory` no longer holds an opinion: it is handed
    `grid_conditions`' own answer, so **it adopts exactly what the law would
    authorise this tick** — through the daytime window, and through the
    2026-09-18 band amendment, without knowing about either.
    """

    def restored(self, *, water=25.6, band="F1", topup=True, day=17, hh=15):
        """The state at `hh`:00 and the Memory a restart there comes back with.

        15:00 is inside the 10-18 solar window and outside the 23-07 night one;
        17/9/2026 is a Thursday, 19/9 a Saturday.
        """
        from custom_components.villa_pool.supervisor import (
            grid_conditions, restore_memory,
        )

        now = at(2026, 9, day, hh, 0)
        st = state(now, water_temp=water, headroom_w=0.0, band=band,
                   grid_day_topup=topup)
        return st, restore_memory(
            mono=now, pdc_running=True, pump_running=True, water_temp=water,
            min_temp=DEFAULT_MIN_TEMP,
            grid_ok=grid_conditions(st, running=True)[0],
            solar_ok=False,
        )

    def test_the_run_is_adopted(self):
        _, mem = self.restored()
        assert mem.pdc_state == PDC_GRID

    def test_the_next_tick_continues_it_rather_than_starting_it(self):
        """The discriminator: `pdc_since` survives, so this is one run and not
        two. A fresh entry would re-stamp it and bracket a new session."""
        st, mem = self.restored()
        dec, mem2 = run(st, mem)
        assert dec.pdc_state == PDC_GRID
        assert mem2.pdc_since == mem.pdc_since
        assert "heating on grid" in dec.reason      # holding, not entering

    def test_with_the_top_up_off_the_run_is_left_alone(self):
        """Nothing authorises a grid run at 15:00 with the switch off, so the
        machine is somebody else's — most likely the owner's own one-shot."""
        _, mem = self.restored(topup=False)
        assert mem.pdc_state == PDC_OFF

    def test_hot_enough_still_wins(self):
        """Whatever the relay says, a pool above min+0.5 has no run to adopt."""
        _, mem = self.restored(water=27.6)
        assert mem.pdc_state == PDC_OFF

    @pytest.mark.parametrize("hh,band,expected", [
        # The amendment of 2026-09-18: the daytime top-up ignores the band...
        (15, BAND_F2, PDC_GRID),
        # ...and the night window does not. Saturday either way.
        (1, BAND_F2, PDC_OFF),
        (15, "F1", PDC_GRID),
        (1, BAND_F3, PDC_GRID),
    ])
    def test_adoption_tracks_the_law_it_does_not_restate_it(
        self, hh, band, expected
    ):
        """The invariant worth pinning, now that `grid_conditions` is the only
        opinion: whatever the law would keep running, the restore adopts.

        Both halves of the band rule fall out of it without `restore_memory`
        knowing a band from a window — which is the point, because it is the
        hand-copy of exactly these two rules that drifted twice in two days.
        """
        _, mem = self.restored(hh=hh, band=band, day=19)
        assert mem.pdc_state == expected
