"""The ORP/pH chlorine trim (STORY §5.3, §9) — pure tests.

The numbers in the headline cases are the real ones, measured on the owner's
pool on 2026-09-21 against their own reference tool, for the same reason the
COP tests replay the measured night: a test built on a fiction proves the code
agrees with the fiction.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from custom_components.villa_pool.const import (
    MODE_CLOSED,
    MODE_WINTER,
    WATER_FLUSH_S,
)
from custom_components.villa_pool.supervisor import (
    Memory,
    decide,
    orp_trim,
    read_quality,
    target_hours,
)

from .helpers import at, config, state


def fresh_mem(now, seconds: float = 600) -> Memory:
    """A Memory whose pump has been in marcia for `seconds`."""
    return Memory(pump_running_since=now - timedelta(seconds=seconds))


def chem(now=None, **kw):
    """A pool in the chlorine window with the probe reading and control on."""
    base = dict(
        orp_control=True,
        water_ph=7.2,
        water_orp=700.0,
        water_ec=8.9,
        chlorine_hours_today=0.0,
        pump_running=True,
    )
    base.update(kw)
    return state(now or at(d=21, hh=18, mm=0), **base)


# --- freshness: the skimmer is not the pool ---------------------------------


class TestFreshness:
    """The tester sits in the skimmer. With the pump off that is a stagnant
    pocket, and on 2026-09-21 it read EC 7.01 mS/cm against 8.906 flushed —
    21 % low — with the owner's reference tool putting the pool at 8.61."""

    def test_pump_not_running_is_never_fresh(self):
        now = at(d=21, hh=18)
        q = read_quality(chem(now), Memory(pump_running_since=None))
        assert q.fresh is False
        assert q.stale_reason == "pump not in marcia"

    def test_still_flushing_is_not_fresh(self):
        now = at(d=21, hh=18)
        q = read_quality(chem(now), fresh_mem(now, WATER_FLUSH_S - 1))
        assert q.fresh is False
        assert "flushing" in q.stale_reason

    def test_fresh_once_the_flush_window_has_elapsed(self):
        now = at(d=21, hh=18)
        q = read_quality(chem(now), fresh_mem(now, WATER_FLUSH_S))
        assert q.fresh is True
        assert q.stale_reason == ""

    def test_values_are_carried_even_when_stale(self):
        """The diagnostic surface still shows what the probe says; only the
        trim is required to ignore it."""
        now = at(d=21, hh=18)
        q = read_quality(chem(now), Memory(pump_running_since=None))
        assert (q.ph, q.orp, q.ec) == (7.2, 700.0, 8.9)


# --- when the trim is allowed to do anything at all --------------------------


class TestTrimGating:
    def test_control_off_is_always_zero(self):
        now = at(d=21, hh=18)
        st = chem(now, orp_control=False, water_orp=500.0)
        trim = orp_trim(st, read_quality(st, fresh_mem(now)))
        assert trim.hours == 0.0
        assert trim.reason == "ORP control off"

    def test_stale_reading_is_zero_and_says_why(self):
        now = at(d=21, hh=18)
        st = chem(now, water_orp=500.0)
        trim = orp_trim(st, read_quality(st, Memory(pump_running_since=None)))
        assert trim.hours == 0.0
        assert "pump not in marcia" in trim.reason

    def test_winter_keeps_its_fixed_maintenance_dose(self):
        now = at(m=1, d=15, hh=13)
        st = chem(now, mode=MODE_WINTER, water_orp=500.0)
        trim = orp_trim(st, read_quality(st, fresh_mem(now)))
        assert trim.hours == 0.0

    def test_unreadable_orp_is_zero(self):
        now = at(d=21, hh=18)
        st = chem(now, water_orp=None)
        trim = orp_trim(st, read_quality(st, fresh_mem(now)))
        assert trim.hours == 0.0
        assert trim.reason == "ORP unreadable"


# --- the arithmetic, and its deliberate asymmetry ----------------------------


class TestTrimArithmetic:
    def _trim(self, now, **kw):
        st = chem(now, **kw)
        return orp_trim(st, read_quality(st, fresh_mem(now)))

    def test_at_target_changes_nothing(self):
        assert self._trim(at(d=21, hh=18), water_orp=700.0).hours == 0.0

    def test_full_scale_below_target_gives_the_whole_extension(self):
        # 100 mV under target == ORP_TRIM_FULL_SCALE_MV -> orp_max_extra_hours.
        assert self._trim(at(d=21, hh=18), water_orp=600.0).hours == 2.0

    def test_half_scale_gives_half(self):
        assert self._trim(at(d=21, hh=18), water_orp=650.0).hours == 1.0

    def test_extension_is_clamped(self):
        """A probe reading absurdly low cannot run the cell away: the bound is
        in hours, which is the unit the owner already caps."""
        assert self._trim(at(d=21, hh=18), water_orp=0.0).hours == 2.0

    def test_orp_above_target_trims_nothing(self):
        """Extension only. A cut would oscillate against its own input — it
        stops the cell, which stops the pump, which makes the reading stale,
        which removes the cut. See `orp_trim`'s docstring."""
        trim = self._trim(at(d=21, hh=18), water_orp=900.0)
        assert trim.hours == 0.0
        assert "over target" in trim.reason

    def test_a_sensor_stuck_high_leaves_the_plain_hours_target(self):
        now = at(d=21, hh=18)
        st = chem(now, water_orp=5000.0, target_chlorine_hours=6.0)
        trim = orp_trim(st, read_quality(st, fresh_mem(now)))
        assert target_hours(st, trim.hours) == 6.0


# --- the pH ceiling: the rung that stops the loop winding up -----------------


class TestPhCeiling:
    """Chlorine's active form is HOCl and its share collapses as pH rises, so
    above the ceiling more cell hours is the wrong answer to a low ORP — the
    right answer is acid, which the supervisor cannot dose."""

    def _trim(self, now, **kw):
        st = chem(now, **kw)
        return orp_trim(st, read_quality(st, fresh_mem(now)))

    def test_high_ph_suspends_the_extension_and_asks_for_acid(self):
        trim = self._trim(at(d=21, hh=18), water_orp=600.0, water_ph=7.9)
        assert trim.hours == 0.0
        assert trim.suspended is True
        assert "add acid" in trim.reason
        assert "7.9" in trim.reason

    def test_unknown_ph_also_suspends_the_extension(self):
        """The ceiling exists to prevent windup; an unreadable pH removes the
        protection, so it removes the extension with it."""
        trim = self._trim(at(d=21, hh=18), water_orp=600.0, water_ph=None)
        assert trim.hours == 0.0
        assert trim.suspended is True

    def test_exactly_at_the_ceiling_still_extends(self):
        trim = self._trim(at(d=21, hh=18), water_orp=600.0, water_ph=7.7)
        assert trim.hours == 2.0
        assert trim.suspended is False

    def test_high_ph_with_a_healthy_orp_is_not_a_suspension(self):
        """`suspended` means "the water wants chlorine and we are refusing".
        High pH with ORP already at target is not that — there is nothing to
        refuse, and flagging it would cry wolf on the reason line."""
        trim = self._trim(at(d=21, hh=18), water_orp=900.0, water_ph=8.2)
        assert trim.hours == 0.0
        assert trim.suspended is False


# --- how the trim reaches the target -----------------------------------------


class TestTargetHours:
    def test_trim_is_added_after_the_cover_factor(self):
        st = chem(at(d=21, hh=18), cover_closed=True,
                  target_chlorine_hours=8.0)
        # 8.0 * 0.5 cover factor, then +1.0 — not (8.0 + 1.0) * 0.5. The cover
        # scales what the pool LOSES; the trim is what the water measured.
        assert target_hours(st, 1.0) == 5.0

    def test_target_never_goes_negative(self):
        """`orp_trim` cannot return a negative today, but `target_hours` takes
        the number on trust from its caller and guards it anyway."""
        st = chem(at(d=21, hh=18), target_chlorine_hours=0.5)
        assert target_hours(st, -5.0) == 0.0

    def test_winter_target_ignores_the_trim_entirely(self):
        st = chem(at(m=1, d=15, hh=13), mode=MODE_WINTER)
        assert target_hours(st, 2.0) == target_hours(st, 0.0)


# --- the whole law ------------------------------------------------------------


class TestDegradesToTheHoursLaw:
    """The property that makes this safe to ship: with the switch off, or with
    no probe, `decide()` is byte-identical to v0.6.0. This is the analogue of
    `test_dry_run_calls_nothing`."""

    def _decide(self, **kw):
        now = at(d=21, hh=18)
        st = chem(now, **kw)
        return decide(st, fresh_mem(now))[0]

    def test_switch_off_matches_no_probe_at_all(self):
        with_probe = self._decide(orp_control=False, water_orp=500.0,
                                  water_ph=7.1)
        no_probe = self._decide(orp_control=False, water_orp=None,
                                water_ph=None, water_ec=None)
        assert with_probe.chlorine_on == no_probe.chlorine_on
        assert with_probe.pump_on == no_probe.pump_on
        assert with_probe.pump_speed == no_probe.pump_speed
        assert with_probe.reason == no_probe.reason

    def test_a_low_orp_with_the_switch_off_changes_nothing(self):
        off = self._decide(orp_control=False, water_orp=400.0,
                           chlorine_hours_today=99.0)
        assert off.chlorine_on is False
        assert "ORP" not in off.reason

    def test_stale_reading_with_the_switch_on_changes_nothing(self):
        """A read gap is not evidence — the rule the PdC has had since
        v0.1.0, applied to chemistry."""
        now = at(d=21, hh=18)
        st = chem(now, orp_control=True, water_orp=400.0,
                  chlorine_hours_today=99.0)
        stale = decide(st, fresh_mem(now, WATER_FLUSH_S - 1))[0]
        assert stale.chlorine_on is False


class TestTheGapSection9Recorded:
    """2026-09-21, the owner's real numbers: the cell ran 6.69 h against a
    6.0 h target, so `chlorine_hours_missing` was 0 and the supervisor believed
    the day was done — while the reference tool measured FAC 1.2 ppm, under the
    1.5-2 of §9. The hours proxy said done; the water said short."""

    def _state(self, now, **kw):
        base = dict(
            target_chlorine_hours=6.0,
            chlorine_hours_today=6.69,
            water_orp=619.0,      # what the tester read, flushed
            water_ph=7.2,         # what the reference read
        )
        base.update(kw)
        return chem(now, **base)

    def test_v060_stops_at_the_hours_target(self):
        now = at(d=21, hh=18)
        st = self._state(now, orp_control=False)
        d = decide(st, fresh_mem(now))[0]
        assert d.chlorine_on is False
        assert "target met" in d.detail["chlorine_reason"]

    def test_the_trim_reopens_it(self):
        now = at(d=21, hh=18)
        st = self._state(now, orp_control=True)
        d = decide(st, fresh_mem(now))[0]
        # 700 - 619 = 81 mV under -> 2.0 * 0.81 = 1.62 h -> target 7.62 h.
        assert d.detail["orp_trim_h"] == 1.62
        assert d.chlorine_on is True

    def test_the_reason_line_names_the_chemistry(self):
        now = at(d=21, hh=18)
        d = decide(self._state(now, orp_control=True), fresh_mem(now))[0]
        assert "619 mV" in d.reason
        assert "81 under target" in d.reason

    def test_high_ph_turns_it_into_a_request_for_acid(self):
        """Same low ORP, but pH over the ceiling: the supervisor refuses to
        burn cell hours it knows cannot fix the problem, and says so."""
        now = at(d=21, hh=18)
        st = self._state(now, orp_control=True, water_ph=7.9)
        d = decide(st, fresh_mem(now))[0]
        assert d.detail["orp_trim_h"] == 0.0
        assert d.detail["orp_suspended_by_ph"] is True
        assert d.chlorine_on is False
        assert "add acid" in d.reason


class TestInterlocksStillOutrank:
    """The trim is an amendment to §5.3's target, not a new authority. It can
    never talk its way past a rung of the §5.5 ladder."""

    def test_no_flow_no_cell_however_low_the_orp(self):
        now = at(d=21, hh=18)
        st = chem(now, water_orp=300.0, pump_running=False)
        d = decide(st, Memory(pump_running_since=None))[0]
        assert d.chlorine_on is False

    def test_closed_mode_still_freezes_everything(self):
        now = at(d=21, hh=18)
        st = chem(now, water_orp=300.0, mode=MODE_CLOSED)
        d = decide(st, fresh_mem(now))[0]
        assert d.chlorine_on is False
        assert d.actuate is False

    def test_maintenance_still_freezes_everything(self):
        now = at(d=21, hh=18)
        st = chem(now, water_orp=300.0, maintenance=True)
        d = decide(st, fresh_mem(now))[0]
        assert d.chlorine_on is False
        assert d.actuate is False

    def test_cover_closed_24h_still_cuts_the_cell(self):
        now = at(d=21, hh=18)
        st = chem(now, water_orp=300.0, cover_closed=True,
                  cover_closed_for_h=30.0)
        d = decide(st, fresh_mem(now))[0]
        assert d.chlorine_on is False

    def test_below_80_percent_the_cell_stays_off(self):
        """§6's guardrail: the cell's flow-switch minimum is still unknown."""
        now = at(d=21, hh=18)
        st = chem(now, water_orp=300.0,
                  config=replace(config(), filtration_speed=50))
        d = decide(st, fresh_mem(now))[0]
        assert d.chlorine_on is False


class TestStability:
    """The trim's input depends on the thing it controls: a low ORP extends
    the target, which runs the pump, which is what makes the reading fresh.
    That loop is only safe in one direction, and these are the tests that say
    so."""

    def test_the_trim_is_never_negative_for_any_reading(self):
        """The invariant the whole stability argument rests on. A trim that
        can only RAISE the target cannot start the cycle below."""
        now = at(d=21, hh=18)
        for orp in (0.0, 100.0, 500.0, 699.0, 700.0, 701.0, 900.0, 5000.0):
            st = chem(now, water_orp=orp)
            assert orp_trim(st, read_quality(st, fresh_mem(now))).hours >= 0.0

    def _run(self, ticks: int, *, pump_running: bool, **kw):
        """Replay `ticks` minutes, feeding the pump decision back in as the
        next tick's reality — which is what closes the loop."""
        start = at(d=21, hh=20, mm=30)
        mem = fresh_mem(start)
        seen = []
        for i in range(ticks):
            st = chem(start + timedelta(minutes=i), pump_running=pump_running,
                      **kw)
            decision, mem = decide(st, mem)
            seen.append(decision.pump_on)
            pump_running = decision.pump_on
        return seen

    def test_no_pump_cycling_at_the_target_boundary(self):
        """Regression for the limit cycle a negative trim would have caused.

        20:30 — pump window shut (08-20), chlorine window open (09-21), past
        the 19:00 deadline. 5.6 h done against a 6.0 h target with ORP ABOVE
        target: exactly where a cut would have taken the target to 5.5, called
        it met, stopped the pump, gone stale, restored the 6.0 target and
        started again, every ~4 minutes. Extension-only settles instead.
        """
        seen = self._run(20, pump_running=True, target_chlorine_hours=6.0,
                         chlorine_hours_today=5.6, water_orp=750.0)
        transitions = sum(1 for a, b in zip(seen, seen[1:]) if a != b)
        assert transitions == 0, seen

    def test_it_also_settles_when_starting_from_a_stopped_pump(self):
        """At most one transition: the pump starts (or does not) and stays
        there. Anything more is the cycle coming back."""
        seen = self._run(20, pump_running=False, target_chlorine_hours=6.0,
                         chlorine_hours_today=5.6, water_orp=750.0)
        transitions = sum(1 for a, b in zip(seen, seen[1:]) if a != b)
        assert transitions <= 1, seen

    def test_a_low_orp_holds_the_run_rather_than_chopping_it(self):
        """The extension keeps the pump on, which keeps the reading fresh,
        which keeps the extension. Self-consistent, by design."""
        seen = self._run(20, pump_running=True, target_chlorine_hours=6.0,
                         chlorine_hours_today=6.69, water_orp=619.0)
        assert all(seen), seen
