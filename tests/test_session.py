"""The §5.4 heating-session log.

This exists so the COP model can eventually be fitted against measurements
instead of a guess, which means its one real obligation is to refuse to invent
numbers. A wrong point is worse than no point: it would be fitted.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.villa_pool.const import PDC_GRID, PDC_SOLAR
from custom_components.villa_pool.supervisor import session_step, thermal_kwh

T0 = datetime(2026, 9, 16, 23, 0)


def run(minutes, *, water_from=26.2, water_to=26.7, energy_from=100.0,
        energy_to=118.6, air=18.0, mode=PDC_GRID, night=True, step=10):
    """Open a session, hold it for `minutes`, close it. Returns the result."""
    session, _ = session_step(
        None, now=T0, running=True, was_running=False, mode=mode,
        water=water_from, energy=energy_from, air=air, cover_closed=True,
        in_night_window=night if not isinstance(night, list) else night[0],
    )
    ticks = max(minutes // step, 1)
    for i in range(1, ticks):
        in_night = night[i] if isinstance(night, list) else night
        session, _ = session_step(
            session, now=T0 + timedelta(minutes=i * step), running=True,
            was_running=True, mode=mode, water=None, energy=None, air=air,
            cover_closed=True, in_night_window=in_night,
        )
    session, result = session_step(
        session, now=T0 + timedelta(minutes=minutes), running=False,
        was_running=True, mode=mode, water=water_to, energy=energy_to,
        air=air, cover_closed=True, in_night_window=True,
    )
    assert session is None
    return result


class TestTheMeasuredNightReproduces:
    """The single point the whole COP model rests on (STORY §1): the night of
    16->17/9, water 26.2 -> 26.7, ~18.6 kWh on phase A, COP 2.82."""

    def test_thermal_energy_matches_the_story(self):
        assert thermal_kwh(0.5) == pytest.approx(52.3, abs=0.3)

    def test_the_cop_comes_back_at_2_8(self):
        result = run(360)
        assert result.cop == pytest.approx(2.82, abs=0.02)

    def test_and_carries_what_the_fit_needs(self):
        result = run(360)
        assert result.air_mean == 18.0
        assert result.delta_t == 0.5
        assert result.energy_kwh == pytest.approx(18.6, abs=0.01)
        assert result.clean is True


class TestItRefusesToInventANumber:
    """Every branch here publishes the session and withholds the COP."""

    def test_a_run_shorter_than_the_floor(self):
        result = run(10)
        assert result.cop is None
        assert "too short" in result.note
        assert result.minutes == 10.0        # still logged

    def test_a_rise_inside_the_probes_resolution(self):
        """0.1 °C on a 0.1 °C probe is quantisation, not a measurement."""
        result = run(120, water_to=26.3)
        assert result.cop is None
        assert "below resolution" in result.note

    def test_a_missing_energy_reading(self):
        result = run(120, energy_to=None)
        assert result.cop is None
        assert "missing" in result.note

    def test_a_missing_water_reading(self):
        result = run(120, water_to=None)
        assert result.cop is None
        assert "missing" in result.note

    def test_an_energy_counter_that_did_not_move(self):
        result = run(120, energy_to=100.0)
        assert result.cop is None
        assert "no energy" in result.note

    def test_water_going_backwards(self):
        """Losing heat is not a negative COP, it is not a measurement."""
        result = run(120, water_to=25.9)
        assert result.cop is None


class TestOnlyNightSessionsCalibrate:
    """§5.4: daytime sessions are contaminated by solar gain on the pool."""

    def test_a_night_run_is_clean(self):
        assert run(360).clean is True

    def test_a_daytime_run_is_not(self):
        result = run(360, night=False, mode=PDC_SOLAR)
        assert result.clean is False
        assert "upper bound" in result.note

    def test_one_daylight_tick_is_enough_to_disqualify_it(self):
        """A run that started at 06:00 and ended after sunrise had the sun
        helping for part of it, and the compressor would take the credit."""
        pattern = [True] * 30 + [False] * 6
        result = run(360, night=pattern)
        assert result.clean is False

    def test_a_daytime_run_still_reports_its_cop(self):
        """Withheld would be worse: it is a real upper bound, and labelled."""
        result = run(360, night=False)
        assert result.cop is not None


class TestBracketing:
    def test_a_run_already_going_at_startup_is_not_adopted(self):
        """Its start reading was never taken, so any COP from it is fiction."""
        session, result = session_step(
            None, now=T0, running=True, was_running=True, mode=PDC_GRID,
            water=26.0, energy=100.0, air=18.0, cover_closed=True,
            in_night_window=True,
        )
        assert session is None
        assert result is None

    def test_a_polling_gap_does_not_chop_the_run_in_two(self):
        """The bracket follows the supervisor's state, which holds through a
        gap — `pool_pdc_acceso` would flicker and produce several sessions."""
        session, _ = session_step(
            None, now=T0, running=True, was_running=False, mode=PDC_GRID,
            water=26.0, energy=100.0, air=18.0, cover_closed=True,
            in_night_window=True,
        )
        started = session.started
        for i in range(1, 10):
            session, result = session_step(
                session, now=T0 + timedelta(minutes=i * 10), running=True,
                was_running=True, mode=PDC_GRID, water=None, energy=None,
                air=None, cover_closed=True, in_night_window=True,
            )
            assert result is None
        assert session.started == started

    def test_nothing_happens_while_nothing_runs(self):
        session, result = session_step(
            None, now=T0, running=False, was_running=False, mode="off",
            water=26.0, energy=100.0, air=18.0, cover_closed=None,
            in_night_window=True,
        )
        assert session is None and result is None

    def test_the_mode_recorded_is_where_the_run_ended_up(self):
        """Grid that the sun took over from is one run, not two."""
        session, _ = session_step(
            None, now=T0, running=True, was_running=False, mode=PDC_GRID,
            water=26.0, energy=100.0, air=18.0, cover_closed=True,
            in_night_window=True,
        )
        session, _ = session_step(
            session, now=T0 + timedelta(minutes=30), running=True,
            was_running=True, mode=PDC_SOLAR, water=None, energy=None,
            air=20.0, cover_closed=True, in_night_window=True,
        )
        _, result = session_step(
            session, now=T0 + timedelta(minutes=60), running=False,
            was_running=True, mode=PDC_SOLAR, water=26.5, energy=105.0,
            air=20.0, cover_closed=True, in_night_window=True,
        )
        assert result.mode == PDC_SOLAR

    def test_air_is_a_mean_over_the_run_not_the_last_reading(self):
        session, _ = session_step(
            None, now=T0, running=True, was_running=False, mode=PDC_GRID,
            water=26.0, energy=100.0, air=10.0, cover_closed=True,
            in_night_window=True,
        )
        session, _ = session_step(
            session, now=T0 + timedelta(minutes=30), running=True,
            was_running=True, mode=PDC_GRID, water=None, energy=None,
            air=20.0, cover_closed=True, in_night_window=True,
        )
        _, result = session_step(
            session, now=T0 + timedelta(minutes=60), running=False,
            was_running=True, mode=PDC_GRID, water=26.5, energy=105.0,
            air=30.0, cover_closed=True, in_night_window=True,
        )
        assert result.air_mean == 15.0        # (10 + 20) / 2, ends excluded
