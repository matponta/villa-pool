"""The pure write planner (`supervisor/actuation.py`).

These are the rules that decide whether a command is sent at all. They are the
half of actuation that can be reasoned about without a pool, a bridge or a
cloud, so they are tested that way.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.villa_pool.supervisor import (
    BLIND,
    HOLD,
    LATCHED,
    SETTLING,
    WRITE,
    Lever,
    plan,
    same,
)

T0 = datetime(2026, 9, 17, 12, 0)
SETTLE = 120


def step(lever, desired, actual, at=T0, **kw):
    return plan(lever, desired=desired, actual=actual, mono=at,
                settle_s=SETTLE, **kw)


class TestIdempotence:
    """A command is sent only when the device disagrees."""

    def test_a_device_already_where_we_want_it_is_not_commanded(self):
        verdict, lever = step(Lever(), "on", "on")
        assert verdict.action == HOLD
        assert lever.writes == 0

    def test_a_disagreement_is_commanded(self):
        verdict, lever = step(Lever(), "on", "off")
        assert verdict.action == WRITE
        assert verdict.value == "on"
        assert lever.writes == 1

    def test_holding_the_same_agreement_never_writes_again(self):
        lever = Lever()
        for minute in range(60):
            verdict, lever = step(lever, "on", "on", T0 + timedelta(minutes=minute))
            assert verdict.action == HOLD

    def test_no_intent_clears_the_lever(self):
        """The pump speed while the pump is meant to be off: no opinion, and no
        stale intent left behind to make the next one look like a repeat."""
        _, lever = step(Lever(), 80, 30)
        verdict, lever = step(lever, None, 30)
        assert verdict.action == HOLD
        assert lever == Lever()


class TestSettling:
    """A device that has not answered yet has not refused."""

    def test_a_fresh_command_is_not_repeated_next_tick(self):
        verdict, lever = step(Lever(), "on", "off")
        assert verdict.action == WRITE
        verdict, lever = step(lever, "on", "off", T0 + timedelta(seconds=60))
        assert verdict.action == SETTLING

    def test_after_the_settle_window_it_is_re_asserted_once(self):
        _, lever = step(Lever(), "on", "off")
        verdict, lever = step(lever, "on", "off", T0 + timedelta(seconds=SETTLE + 1))
        assert verdict.action == WRITE
        assert lever.writes == 2

    def test_adopting_the_command_late_still_ends_the_argument(self):
        _, lever = step(Lever(), "on", "off")
        verdict, lever = step(lever, "on", "on", T0 + timedelta(seconds=90))
        assert verdict.action == HOLD
        assert lever.writes == 0


class TestLatching:
    """Re-assert once, then stop fighting (STORY §8 step 2)."""

    def run_until_latched(self, desired="on", actual="off"):
        lever = Lever()
        actions = []
        for minute in range(0, 30, 3):
            verdict, lever = step(lever, desired, actual, T0 + timedelta(minutes=minute))
            actions.append(verdict.action)
        return actions, lever

    def test_exactly_two_commands_are_sent_before_giving_up(self):
        actions, lever = self.run_until_latched()
        assert actions.count(WRITE) == 2
        assert lever.latched is True

    def test_once_latched_it_stays_quiet(self):
        actions, _ = self.run_until_latched()
        assert actions[-1] == LATCHED
        assert WRITE not in actions[3:]

    def test_the_reason_does_not_accuse_a_human(self):
        """A latched lever and an unresponsive device leave identical evidence,
        so the message must not pick one."""
        actions, lever = self.run_until_latched()
        verdict, _ = step(Lever(latched=True, desired="on"), "on", "off")
        assert verdict.action == LATCHED
        assert lever.latched

    def test_a_new_intent_clears_the_latch_and_commands_immediately(self):
        _, lever = self.run_until_latched()
        verdict, lever = step(lever, "off", "on", T0 + timedelta(hours=1))
        assert verdict.action == WRITE
        assert lever.latched is False

    def test_the_device_coming_back_on_its_own_clears_the_latch(self):
        _, lever = self.run_until_latched()
        verdict, lever = step(lever, "on", "on", T0 + timedelta(hours=1))
        assert verdict.action == HOLD
        assert lever.latched is False


class TestNewIntentIsNeverRateLimited:
    """§7.5 wants the PdC `off` "within one tick" when the pump faults — the
    settle window protects a REPEAT of a command, never a change of mind."""

    def test_a_changed_intent_writes_inside_the_settle_window(self):
        _, lever = step(Lever(), "heat", "off")
        verdict, lever = step(lever, "off", "heat", T0 + timedelta(seconds=5))
        assert verdict.action == WRITE
        assert verdict.value == "off"

    def test_and_resets_the_attempt_count(self):
        _, lever = step(Lever(), "heat", "off")
        _, lever = step(lever, "heat", "off", T0 + timedelta(seconds=SETTLE + 1))
        assert lever.writes == 2
        _, lever = step(lever, "off", "heat", T0 + timedelta(seconds=SETTLE + 2))
        assert lever.writes == 1


class TestUnreadableDevices:
    """An `unavailable` lever is not evidence of anything (STORY §5.2, §6)."""

    def test_no_write_while_the_device_cannot_be_read(self):
        verdict, _ = step(Lever(), "on", None)
        assert verdict.action == BLIND

    def test_a_read_gap_does_not_count_against_the_device(self):
        _, lever = step(Lever(), "on", "off")
        before = lever
        _, lever = step(lever, "on", None, T0 + timedelta(seconds=SETTLE + 1))
        assert lever == before

    def test_a_read_gap_does_not_reset_the_settle_window(self):
        _, lever = step(Lever(), "on", "off")
        _, lever = step(lever, "on", None, T0 + timedelta(seconds=30))
        verdict, _ = step(lever, "on", "off", T0 + timedelta(seconds=60))
        assert verdict.action == SETTLING


class TestTolerance:
    """A setpoint read back as 27 is the setpoint we wrote as 27.0."""

    @pytest.mark.parametrize("a,b,tol,expected", [
        (27.0, 27, 0.2, True),
        (27.0, 26.9, 0.2, True),
        (27.0, 27.5, 0.2, False),
        (80, 80.0, 0.5, True),
        ("Manual", "Manual", None, True),
        ("Manual", "AI Flow", None, False),
        (None, 27.0, 0.2, False),
    ])
    def test_same(self, a, b, tol, expected):
        assert same(a, b, tol) is expected

    def test_a_setpoint_within_tolerance_is_never_rewritten(self):
        verdict, _ = step(Lever(desired=27.0), 27.0, 26.95, tolerance=0.2)
        assert verdict.action == HOLD

    def test_a_setpoint_outside_tolerance_is(self):
        verdict, _ = step(Lever(), 27.0, 29.0, tolerance=0.2)
        assert verdict.action == WRITE
