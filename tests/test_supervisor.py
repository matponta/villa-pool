"""Unit tests for the pure modules, below the acceptance level.

`test_acceptance.py` pins the owner's ten criteria. This file pins the edges
those criteria happen not to touch — midnight-crossing windows, tri-state
readings, hysteresis boundaries, MIN_OFF — which is where a control law of this
shape actually breaks.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.villa_pool.const import (
    BAND_F1,
    BAND_F2,
    BAND_F3,
    COP_REF,
    DEFAULT_ANTIFREEZE_OFF_C,
    DEFAULT_ANTIFREEZE_SPEED,
    DEFAULT_MIN_TEMP,
    MODE_AUTO,
    MODE_CLOSED,
    MODE_FILTRATION_ONLY,
    MODE_MANUAL,
    MODE_WINTER,
    PDC_BLOCKED,
    PDC_GRID,
    PDC_OFF,
    PDC_SOLAR,
)
from custom_components.villa_pool.supervisor import (
    Memory,
    chlorine_decision,
    cop_estimate,
    decide,
    in_slot,
    in_window,
    is_confirmed,
    pump_plan,
    restore_memory,
    solar_step,
    thermal_cost,
    window_length,
)
from tests.helpers import at, config, memory, state

MIN = timedelta(minutes=1)


# --- windows -----------------------------------------------------------------

class TestWindows:
    def test_normal_window(self):
        assert in_window(time(12, 0), time(8, 0), time(20, 0)) is True
        assert in_window(time(7, 59), time(8, 0), time(20, 0)) is False

    def test_end_is_exclusive(self):
        assert in_window(time(20, 0), time(8, 0), time(20, 0)) is False
        assert in_window(time(8, 0), time(8, 0), time(20, 0)) is True

    @pytest.mark.parametrize("t,expected", [
        (time(23, 30), True), (time(0, 0), True), (time(6, 59), True),
        (time(7, 0), False), (time(12, 0), False), (time(22, 59), False),
    ])
    def test_midnight_crossing(self, t, expected):
        """The PdC grid window, 23:00 -> 07:00."""
        assert in_window(t, time(23, 0), time(7, 0)) is expected

    def test_equal_bounds_is_an_empty_window_not_a_full_day(self):
        """An owner clearing a window means 'off', and guessing 'always on' is
        the dangerous way to be wrong."""
        assert in_window(time(12, 0), time(9, 0), time(9, 0)) is False

    def test_window_length(self):
        assert window_length(time(8, 0), time(20, 0)) == timedelta(hours=12)
        assert window_length(time(23, 0), time(7, 0)) == timedelta(hours=8)

    def test_in_slot(self):
        assert in_slot(at(hh=12, mm=30), time(12, 0), 2.0) is True
        assert in_slot(at(hh=13, mm=59), time(12, 0), 2.0) is True
        assert in_slot(at(hh=14, mm=1), time(12, 0), 2.0) is False

    def test_zero_length_slot_never_opens(self):
        assert in_slot(at(hh=12, mm=0), time(12, 0), 0.0) is False


# --- solar dwell + hysteresis -------------------------------------------------

class TestSolarDwell:
    def test_dwell_must_elapse_before_ok(self):
        now = at()
        mem = Memory()
        for i in range(11):
            mem = solar_step(mem, now + i * MIN, 3000.0, 3000.0, 2500.0)
            if i < 10:
                assert mem.solar_ok is False, f"minute {i}"
        assert mem.solar_ok is True          # the 10th minute

    def test_hysteresis_holds_between_the_thresholds(self):
        now = at()
        mem = Memory()
        for i in range(11):
            mem = solar_step(mem, now + i * MIN, 3000.0, 3000.0, 2500.0)
        assert mem.solar_ok is True
        # 2600 is below the on-threshold but above the off-threshold.
        mem = solar_step(mem, now + 11 * MIN, 2600.0, 3000.0, 2500.0)
        assert mem.solar_ok is True
        mem = solar_step(mem, now + 12 * MIN, 2400.0, 3000.0, 2500.0)
        assert mem.solar_ok is False

    def test_falls_immediately_no_down_dwell_here(self):
        """The patience on the way down belongs to the PdC machine, not here."""
        now = at()
        mem = Memory()
        for i in range(11):
            mem = solar_step(mem, now + i * MIN, 3000.0, 3000.0, 2500.0)
        mem = solar_step(mem, now + 11 * MIN, 0.0, 3000.0, 2500.0)
        assert mem.solar_ok is False

    def test_dwell_restarts_after_an_interruption(self):
        now = at()
        mem = Memory()
        for i in range(8):
            mem = solar_step(mem, now + i * MIN, 3000.0, 3000.0, 2500.0)
        mem = solar_step(mem, now + 8 * MIN, 0.0, 3000.0, 2500.0)      # dip
        for i in range(9, 17):
            mem = solar_step(mem, now + i * MIN, 3000.0, 3000.0, 2500.0)
        assert mem.solar_ok is False        # only 8 min since the dip
        mem = solar_step(mem, now + 19 * MIN, 3000.0, 3000.0, 2500.0)
        assert mem.solar_ok is True

    def test_unknown_headroom_is_never_ok(self):
        now = at()
        mem = Memory()
        for i in range(15):
            mem = solar_step(mem, now + i * MIN, None, 3000.0, 2500.0)
        assert mem.solar_ok is False


# --- pump confirmation --------------------------------------------------------

class TestPumpConfirmation:
    def test_needs_the_full_60_seconds(self):
        now = at()
        mem = Memory(pump_running_since=now)
        assert is_confirmed(mem, now) is False
        assert is_confirmed(mem, now + timedelta(seconds=59)) is False
        assert is_confirmed(mem, now + timedelta(seconds=60)) is True

    def test_unknown_does_not_reset_the_anchor(self):
        """A flickering tuya-local read must not drop the chlorinator."""
        from custom_components.villa_pool.supervisor import confirm_step

        now = at()
        mem = confirm_step(Memory(), now, True)
        mem2 = confirm_step(mem, now + MIN, None)
        assert mem2.pump_running_since == mem.pump_running_since

    def test_an_explicit_stop_does_reset_it(self):
        from custom_components.villa_pool.supervisor import confirm_step

        now = at()
        mem = confirm_step(Memory(), now, True)
        mem2 = confirm_step(mem, now + MIN, False)
        assert mem2.pump_running_since is None


# --- pump demand --------------------------------------------------------------

class TestPumpPlan:
    def test_no_demand_means_off(self):
        on, speed, reqs = pump_plan(
            cfg=config(), window_open=False, winter_slot=False,
            pdc_wants_flow=False, chlorine_wants=False, pool_in_use=False,
            antifreeze=False, catchup=False, postrun=False,
        )
        assert (on, speed, reqs) == (False, None, ())

    def test_speed_is_the_max_of_the_requesters(self):
        _, speed, _ = pump_plan(
            cfg=config(), window_open=True, winter_slot=False,
            pdc_wants_flow=False, chlorine_wants=False, pool_in_use=False,
            antifreeze=True, catchup=False, postrun=False,
        )
        assert speed == 80        # filtration 80 beats antifreeze 30

    def test_antifreeze_alone_runs_slow(self):
        _, speed, reqs = pump_plan(
            cfg=config(), window_open=False, winter_slot=False,
            pdc_wants_flow=False, chlorine_wants=False, pool_in_use=False,
            antifreeze=True, catchup=False, postrun=False,
        )
        assert speed == 30
        assert reqs == ("antifreeze",)


# --- the PdC machine ----------------------------------------------------------

class TestPdcMachine:
    def test_min_off_blocks_an_immediate_restart(self):
        now = at(2026, 9, 16, 23, 0)
        st = state(now, water_temp=25.0, band=BAND_F3)
        mem = replace(memory(now), pdc_last_stop=now - timedelta(minutes=5))
        dec, _ = decide(st, mem)
        assert dec.pdc_state == PDC_OFF
        assert "MIN_OFF" in dec.detail["pdc_reason"]

    def test_min_off_allows_a_restart_once_elapsed(self):
        now = at(2026, 9, 16, 23, 0)
        st = state(now, water_temp=25.0, band=BAND_F3)
        mem = replace(memory(now), pdc_last_stop=now - timedelta(minutes=16))
        dec, _ = decide(st, mem)
        assert dec.pdc_state == PDC_GRID

    @pytest.mark.parametrize("band,expected", [
        # §3 forbids F2 only — F1 and F3 are near-identical in price, so both
        # are allowed. (On the real ARERA calendar 23:00 is F3 on every day of
        # the week; F1 is included here to pin that the veto is F2-only.)
        (BAND_F1, PDC_GRID),
        (BAND_F2, PDC_OFF),
        (BAND_F3, PDC_GRID),
    ])
    def test_band_gating(self, band, expected):
        now = at(2026, 9, 16, 23, 0)
        st = state(now, water_temp=25.0, band=band)
        dec, _ = decide(st, memory(now))
        assert dec.pdc_state == expected

    def test_grid_heating_switch_off_refuses_grid(self):
        now = at(2026, 9, 16, 23, 0)
        st = state(now, water_temp=25.0, band=BAND_F3, grid_heating=False)
        dec, _ = decide(st, memory(now))
        assert dec.pdc_state == PDC_OFF
        assert "grid heating off" in dec.reason

    def test_pdc_fault_blocks(self):
        now = at(2026, 9, 16, 23, 0)
        st = state(now, water_temp=25.0, band=BAND_F3, pdc_fault=True)
        dec, _ = decide(st, memory(now))
        assert dec.pdc_state == PDC_BLOCKED

    def test_unconfirmed_pump_blocks(self):
        now = at(2026, 9, 16, 23, 0)
        st = state(now, water_temp=25.0, band=BAND_F3)
        dec, _ = decide(st, Memory())      # no confirmation yet
        assert dec.pdc_state == PDC_BLOCKED
        assert "pump not in marcia" in dec.reason

    def test_the_pump_is_still_asked_for_while_the_pdc_waits(self):
        """Start order: pump ON -> confirm -> PdC. The pump must be requested
        even though the PdC is blocked on that very confirmation."""
        now = at(2026, 9, 16, 23, 0)
        st = state(now, water_temp=25.0, band=BAND_F3, pump_running=False)
        dec, _ = decide(st, Memory())
        assert dec.pump_on is True
        assert "pdc" in dec.requesters

    def test_unknown_water_temp_never_starts_heating(self):
        now = at(2026, 9, 16, 23, 0)
        st = state(now, water_temp=None, band=BAND_F3)
        dec, _ = decide(st, memory(now))
        assert dec.pdc_state == PDC_OFF


# --- modes --------------------------------------------------------------------

class TestModes:
    @pytest.mark.parametrize("mode", [MODE_MANUAL, MODE_CLOSED])
    def test_frozen_modes_drive_nothing(self, mode):
        now = at(2026, 9, 16, 12, 0)
        st = state(now, mode=mode, pool_in_use=True, water_temp=20.0)
        dec, _ = decide(st, memory(now))
        assert dec.pump_on is False
        assert dec.chlorine_on is False
        assert dec.pdc_write is False

    def test_maintenance_freezes_everything(self):
        now = at(2026, 9, 16, 12, 0)
        st = state(now, maintenance=True, pool_in_use=True)
        dec, _ = decide(st, memory(now))
        assert dec.pump_on is False
        assert dec.chlorine_on is False
        assert "maintenance" in dec.reason

    def test_filtration_only_runs_the_pump_but_never_the_pdc(self):
        now = at(2026, 9, 16, 12, 0)
        st = state(now, mode=MODE_FILTRATION_ONLY, water_temp=20.0,
                   headroom_w=5000.0)
        dec, _ = decide(st, memory(now))
        assert dec.pump_on is True
        assert dec.pdc_state == PDC_BLOCKED


# --- chlorine guardrails ------------------------------------------------------

class TestChlorineGuardrails:
    def test_never_enabled_below_80_percent(self):
        """STORY §6: the cell's flow-switch minimum is unknown."""
        now = at(2026, 9, 16, 12, 0)
        st = state(now)
        on, reason = chlorine_decision(
            st, pump_confirmed=True, pump_speed=50, pdc_state=PDC_OFF,
            antifreeze=False,
        )
        assert on is False
        assert "below chlorine minimum" in reason

    def test_cover_halves_the_target(self):
        now = at(2026, 9, 16, 12, 0)
        st = state(now, cover_closed=True, cover_closed_for_h=3.0,
                   chlorine_hours_today=4.5)
        dec, _ = decide(st, memory(now))
        assert dec.chlorine_on is False      # target is 8 * 0.5 = 4

    def test_free_hours_run_chlorine_outside_its_window(self):
        """§5.3: take the production while the PdC already pays for the pump.

        NOTE: with the DEFAULT windows this branch is unreachable — the solar
        window (10-18) lies wholly inside the chlorine window (09-21), so the
        PdC can never be on sun while chlorine is shut. It only bites if the
        owner narrows the chlorine window, as here.
        """
        now = at(2026, 9, 16, 11, 0)         # in the solar window...
        st = state(now, headroom_w=5000.0, water_temp=26.0,
                   chlorine_start=time(14, 0))   # ...but chlorine opens at 14:00
        mem = memory(now)
        for i in range(12):
            dec, mem = decide(st.with_now(now + i * MIN), mem)
        assert dec.pdc_state == PDC_SOLAR
        assert dec.chlorine_on is True
        assert "free hours" in dec.detail["chlorine_reason"]


# --- COP model (diagnostic) ---------------------------------------------------

class TestCopModel:
    def test_the_measured_point_reproduces(self):
        assert cop_estimate(18.0) == COP_REF

    @pytest.mark.parametrize("air,approx", [(10.0, 2.0), (18.0, 2.8), (28.0, 3.8)])
    def test_indicative_values_from_the_story(self, air, approx):
        assert cop_estimate(air) == pytest.approx(approx, abs=0.05)

    def test_clamped_to_something_physical(self):
        assert cop_estimate(-40.0) == 1.5
        assert cop_estimate(90.0) == 5.5

    def test_unknown_air_gives_no_estimate(self):
        assert cop_estimate(None) is None

    def test_thermal_cost_needs_a_known_band(self):
        prices = {"F1": 0.164, "F2": 0.193, "F3": 0.166}
        assert thermal_cost(None, prices, 18.0) is None
        assert thermal_cost("F3", prices, 18.0) == pytest.approx(0.0589, abs=1e-3)

    def test_solar_share_lowers_the_cost(self):
        prices = {"F1": 0.164, "F2": 0.193, "F3": 0.166}
        full_grid = thermal_cost("F1", prices, 18.0, headroom_w=0, pdc_power_w=3100)
        half_solar = thermal_cost(
            "F1", prices, 18.0, headroom_w=1550, pdc_power_w=3100
        )
        assert half_solar == pytest.approx(full_grid / 2, abs=1e-3)


# --- regressions from the pre-tag adversarial review -------------------------

class TestAdversarialRegressions:
    def test_filtration_only_leaves_no_phantom_run_in_memory(self):
        """The reported state and the remembered state must agree.

        The first cut blocked the PdC by overwriting the ANSWER after the state
        machine had already advanced Memory into GRID — so the decision said
        BLOCKED while memory said GRID, and the next tick would resume a run
        that was never announced.
        """
        now = at(2026, 9, 16, 23, 0)
        st = state(now, mode=MODE_FILTRATION_ONLY, water_temp=25.0,
                   band=BAND_F3)
        dec, mem = decide(st, memory(now))
        assert dec.pdc_state == PDC_BLOCKED
        assert mem.pdc_state == PDC_BLOCKED

    def test_filtration_only_stays_blocked_across_ticks(self):
        now = at(2026, 9, 16, 23, 0)
        st = state(now, mode=MODE_FILTRATION_ONLY, water_temp=25.0,
                   band=BAND_F3)
        mem = memory(now)
        for i in range(5):
            dec, mem = decide(st.with_now(now + i * MIN), mem)
            assert dec.pdc_state == PDC_BLOCKED
            assert mem.pdc_state == PDC_BLOCKED

    def test_the_reason_line_fits_in_a_ha_state(self):
        """HA truncates a state at 255 characters; the worst case is every
        requester active at once."""
        now = at(2026, 12, 15, 12, 30)
        st = state(now, mode="winter", outdoor_temp=-1.0, water_temp=8.0,
                   pool_in_use=True, chlorine_hours_today=0.0)
        dec, _ = decide(st, memory(now))
        assert len(dec.reason) <= 255

    def test_a_grid_run_does_not_stop_at_min_temp(self):
        """The hysteresis band must survive the run.

        `grid_conditions` is consulted both to START and to CONTINUE. Using the
        start threshold (water < min_temp) for the continue test ended the run
        the moment the water touched 27.0 — it then drifted back below within
        MIN_OFF and restarted, giving ~14 compressor starts overnight instead of
        2. Found by simulating a full September day before the v0.1.0 tag.
        """
        now = at(2026, 9, 16, 23, 0)
        st = state(now, water_temp=25.6, band=BAND_F3, grid_heating=True)
        dec, mem = decide(st, memory(now))
        assert dec.pdc_state == PDC_GRID
        # Water climbs past min_temp but not yet past the hysteresis: keep going.
        for water in (26.9, 27.0, 27.1, 27.4):
            dec, mem = decide(
                replace(st.with_now(now + 30 * MIN), water_temp=water), mem
            )
            assert dec.pdc_state == PDC_GRID, f"stopped early at {water}"
        # And only at min_temp + 0.5 does it stop.
        dec, mem = decide(
            replace(st.with_now(now + 60 * MIN), water_temp=27.5), mem
        )
        assert dec.pdc_state == PDC_OFF

    def test_a_night_of_grid_heating_is_not_a_short_cycle(self):
        """End to end: one night, counting compressor starts."""
        now = at(2026, 9, 16, 23, 0)
        mem = memory(now)
        water, starts, prev = 25.6, 0, PDC_OFF
        for i in range(0, 8 * 60, 5):          # 23:00 -> 07:00
            st = state(now + i * MIN, water_temp=round(water, 1), band=BAND_F3,
                       grid_heating=True, headroom_w=0.0)
            dec, mem = decide(st, mem)
            if dec.pdc_state == PDC_GRID and prev != PDC_GRID:
                starts += 1
            prev = dec.pdc_state
            water += 0.08 if dec.pdc_state == PDC_GRID else -0.015
        assert starts <= 3, f"{starts} compressor starts in one night"


# --- daylight saving ---------------------------------------------------------

class TestDaylightSaving:
    """Every timer is measured against the UTC anchor, never local time.

    Europe/Rome springs forward on Sunday 2026-03-29: local 02:00 CET becomes
    03:00 CEST. Read in local time a stamp from 01:55 looks 70 minutes old at
    03:05 — when ten real minutes have passed. MIN_OFF is 15 minutes, so a
    local-time comparison hands the compressor a restart it has not earned, and
    the autumn transition does the mirror image: an hour where nothing can
    elapse at all. Once a year each, and invisible in a dry run.
    """

    # 01:55 CET and 03:05 CEST — ten real minutes apart, 70 local ones.
    BEFORE_LOCAL = datetime(2026, 3, 29, 1, 55)
    BEFORE_UTC = datetime(2026, 3, 29, 0, 55, tzinfo=UTC)
    AFTER_LOCAL = datetime(2026, 3, 29, 3, 5)
    AFTER_UTC = datetime(2026, 3, 29, 1, 5, tzinfo=UTC)

    def cold_night(self, now, utc_now=None):
        """Inside the grid window, water below minimum, F3: GRID is due — and
        the only thing standing between the pool and a restart is MIN_OFF."""
        return state(now, utc_now=utc_now, water_temp=25.0, band=BAND_F3,
                     headroom_w=0.0, grid_heating=True)

    def test_min_off_survives_the_spring_forward(self):
        mem = Memory(
            pdc_state=PDC_OFF,
            pdc_last_stop=self.BEFORE_UTC,
            pump_running_since=self.BEFORE_UTC - 10 * MIN,
        )
        dec, _ = decide(self.cold_night(self.AFTER_LOCAL, self.AFTER_UTC), mem)
        assert dec.pdc_state == PDC_OFF
        assert "MIN_OFF" in dec.detail["pdc_reason"]

    def test_the_same_stamps_in_local_time_would_have_restarted(self):
        """Proof the anchor is load-bearing rather than decorative: drop it and
        the identical situation authorises the start."""
        mem = Memory(
            pdc_state=PDC_OFF,
            pdc_last_stop=self.BEFORE_LOCAL,
            pump_running_since=self.BEFORE_LOCAL - 10 * MIN,
        )
        dec, _ = decide(self.cold_night(self.AFTER_LOCAL), mem)
        assert dec.pdc_state == PDC_GRID

    def test_the_autumn_fold_does_not_make_an_interval_negative(self):
        """25/10: local 02:50 CEST, then local 02:10 CET twenty real minutes
        later. In local time the pump confirmation is -40 minutes old."""
        started_utc = datetime(2026, 10, 25, 0, 50, tzinfo=UTC)   # local 02:50 CEST
        now_local = datetime(2026, 10, 25, 2, 10)                  # CET, second pass
        now_utc = datetime(2026, 10, 25, 1, 10, tzinfo=UTC)
        mem = Memory(pump_running_since=started_utc)
        assert is_confirmed(mem, now_utc) is True
        # Without the anchor the same 20 real minutes read as -40 and the pump
        # would be treated as never confirmed, blocking the PdC all hour.
        assert is_confirmed(
            Memory(pump_running_since=datetime(2026, 10, 25, 2, 50)), now_local
        ) is False

    def test_windows_still_read_in_local_time(self):
        """The anchor is for durations only. "23:00" means 23:00 in Italy, in
        July and in January alike — a window compared in UTC would drift by an
        hour twice a year, which is the bug in the other direction."""
        anchor = datetime(2026, 3, 29, 21, 30, tzinfo=UTC)   # = 23:30 CEST
        st = self.cold_night(at(2026, 3, 29, 23, 30), anchor)
        dec, _ = decide(st, memory(anchor))
        assert dec.pdc_state == PDC_GRID


# --- winter and antifreeze (v0.4.0) ------------------------------------------

class TestAntifreezeOutranksTheFrozenModes:
    """Owner amendment 2026-09-17 to STORY §5.5.

    As written, rung 1 (`maintenance` / `manual` / `closed`) sat above
    antifreeze — so the supervisor stopped protecting the pipes in `closed`,
    which is the mode the pool spends the entire winter in, unattended. The
    cost of being wrong one way is a stopped pump for a few hours; the other
    way it is burst pipes.
    """

    def freezing(self, mode, **kw):
        now = at(2026, 12, 15, 3, 0)
        st = state(now, mode=mode, outdoor_temp=-1.0, water_temp=8.0, **kw)
        return decide(st, memory(now))

    @pytest.mark.parametrize("mode", [MODE_MANUAL, MODE_CLOSED])
    def test_the_pump_still_runs(self, mode):
        dec, _ = self.freezing(mode)
        assert dec.pump_on is True
        assert dec.pump_speed == DEFAULT_ANTIFREEZE_SPEED

    @pytest.mark.parametrize("mode", [MODE_MANUAL, MODE_CLOSED])
    def test_the_cell_is_cut(self, mode):
        """Not optional: §6 refuses to take the pump below 80 % with the
        chlorinator enabled, so antifreeze that did not cut it could not run at
        `antifreeze_speed` at all."""
        dec, _ = self.freezing(mode)
        assert dec.chlorine_on is False

    @pytest.mark.parametrize("mode", [MODE_MANUAL, MODE_CLOSED])
    def test_the_heat_pump_is_left_alone(self, mode):
        """The mode is still the owner's. Freeze protection asked for flow, not
        for the machine."""
        dec, _ = self.freezing(mode)
        assert dec.pdc_state == PDC_BLOCKED
        assert dec.pdc_write is False

    @pytest.mark.parametrize("mode", [MODE_MANUAL, MODE_CLOSED])
    def test_the_reason_says_it_is_overriding(self, mode):
        dec, _ = self.freezing(mode)
        assert "ANTIFREEZE overrides" in dec.reason
        assert dec.detail["antifreeze_override"] == f"mode {mode}"

    @pytest.mark.parametrize("mode", [MODE_MANUAL, MODE_CLOSED])
    def test_a_warm_day_in_the_same_mode_still_drives_nothing(self, mode):
        now = at(2026, 12, 15, 3, 0)
        st = state(now, mode=mode, outdoor_temp=12.0, water_temp=8.0)
        dec, _ = decide(st, memory(now))
        assert dec.pump_on is False
        assert dec.actuate is False

    def test_maintenance_still_freezes_antifreeze_too(self):
        """Deliberate, and the one case that did NOT change: maintenance means
        someone is physically at the pool, possibly with it drained or the
        valves shut, so starting a pump under them is a hazard rather than a
        protection. It also expires by itself after 4 h — `closed` lasts
        months."""
        dec, _ = self.freezing(MODE_AUTO, maintenance=True)
        assert dec.pump_on is False
        assert dec.actuate is False
        assert dec.detail["antifreeze"] is True     # engaged, and deliberately ignored

    def test_the_override_actuates_where_the_plain_freeze_does_not(self):
        cold, _ = self.freezing(MODE_CLOSED)
        assert cold.actuate is True


class TestAntifreezeSurvivesARestart:
    """The latch is history, and a restart does not have it.

    At +1 °C — inside the 0..+2 band — a fresh `Memory` cannot tell whether
    antifreeze was running. Re-deriving from the ENGAGE threshold answers "no"
    and stops the pump in the middle of a cold snap.
    """

    def restored(self, outdoor):
        now = at(2026, 12, 15, 3, 0)
        return restore_memory(
            mono=now, pdc_running=False, pump_running=True, water_temp=8.0,
            min_temp=DEFAULT_MIN_TEMP, band=BAND_F3, grid_heating=True,
            in_grid_window=True, solar_ok=False,
            outdoor_temp=outdoor, antifreeze_off_c=DEFAULT_ANTIFREEZE_OFF_C,
        )

    def test_a_restart_inside_the_band_keeps_the_pump_running(self):
        now = at(2026, 12, 15, 3, 0)
        mem = self.restored(1.0)
        dec, _ = decide(
            state(now, mode=MODE_WINTER, outdoor_temp=1.0, water_temp=8.0), mem
        )
        assert dec.pump_on is True
        assert dec.pump_speed == DEFAULT_ANTIFREEZE_SPEED

    def test_a_restart_above_the_release_threshold_does_not_engage(self):
        assert self.restored(5.0).antifreeze_active is False

    def test_an_unknown_outdoor_temperature_does_not_invent_a_freeze(self):
        """`antifreeze_step` then holds whatever this produced, so guessing ON
        here would latch a freeze that never happened until the probe came
        back."""
        assert self.restored(None).antifreeze_active is False

    @pytest.mark.parametrize("outdoor,expected", [
        (-5.0, True), (-0.1, True), (0.0, True), (1.9, True),
        (2.0, False), (10.0, False),
    ])
    def test_re_derivation_uses_the_release_threshold(self, outdoor, expected):
        assert self.restored(outdoor).antifreeze_active is expected


class TestAntifreezeStopsWhatItStarted:
    """Having STARTED the pump in a frozen mode, the supervisor has to stop it.

    Without this the release reverts to "not driving anything" and the pump the
    supervisor turned on runs for the rest of the winter — in `closed`, the
    mode nobody looks at.
    """

    def episode(self, mode=MODE_CLOSED):
        now = at(2026, 12, 15, 3, 0)
        cold = state(now, mode=mode, outdoor_temp=-1.0, water_temp=8.0)
        dec, mem = decide(cold, memory(now))
        assert dec.pump_on is True
        warm = state(now + timedelta(hours=6), mode=mode, outdoor_temp=5.0,
                     water_temp=8.0, pump_running=True)
        return warm, mem

    def test_the_release_commands_the_pump_off(self):
        warm, mem = self.episode()
        dec, _ = decide(warm, mem)
        assert dec.pump_on is False
        assert dec.actuate is True
        assert "stopping the pump the supervisor started" in dec.reason

    def test_it_keeps_asserting_rather_than_firing_once(self):
        """A command that does not land must still be re-asserted; the actuator
        needs the intent to persist across ticks to do that."""
        warm, mem = self.episode()
        for _ in range(5):
            dec, mem = decide(warm, mem)
            assert dec.pump_on is False
            assert dec.actuate is True

    def test_nothing_else_is_driven_on_the_way_out(self):
        warm, mem = self.episode()
        dec, _ = decide(warm, mem)
        assert dec.chlorine_on is False
        assert dec.pdc_write is False

    def test_leaving_the_frozen_mode_hands_the_pump_back(self):
        warm, mem = self.episode()
        _, mem = decide(warm, mem)
        back = replace(warm, mode=MODE_AUTO)
        _, mem2 = decide(back, mem)
        assert mem2.antifreeze_owns_pump is False

    def test_a_frozen_mode_that_never_froze_still_drives_nothing(self):
        """The latch is what authorises the stop, not the mode."""
        now = at(2026, 12, 15, 3, 0)
        mild = state(now, mode=MODE_CLOSED, outdoor_temp=12.0, water_temp=8.0,
                     pump_running=True)
        dec, _ = decide(mild, memory(now))
        assert dec.actuate is False

    def test_maintenance_during_the_release_still_freezes(self):
        """Someone is at the pool; the stop waits for them to finish."""
        warm, mem = self.episode()
        dec, _ = decide(replace(warm, maintenance=True), mem)
        assert dec.actuate is False
