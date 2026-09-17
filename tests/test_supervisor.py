"""Unit tests for the pure modules, below the acceptance level.

`test_acceptance.py` pins the owner's ten criteria. This file pins the edges
those criteria happen not to touch — midnight-crossing windows, tri-state
readings, hysteresis boundaries, MIN_OFF — which is where a control law of this
shape actually breaks.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import time, timedelta

import pytest

from custom_components.villa_pool.const import (
    BAND_F1,
    BAND_F2,
    BAND_F3,
    COP_REF,
    MODE_CLOSED,
    MODE_FILTRATION_ONLY,
    MODE_MANUAL,
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
