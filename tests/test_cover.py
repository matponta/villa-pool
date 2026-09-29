"""The cover ramp (STORY §5.3, owner amendment 2026-09-29).

Two halves, tested separately:

* the ramp itself — a straight line from `target_chlorine_hours` down to
  `cover_min_chlorine_hours` over `cover_ramp_days`, which has to stay
  proportional when the owner moves either end;
* "closed for N days" — measured from the last time the cover was seen OPEN in
  the recorder, not from `last_changed`, which a restart and every
  `unavailable` blip reset. Seen live: the helper read `on` from 22/9, and
  `last_changed` said 25/9 15:49 — the restart.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.villa_pool.const import CONF_COVER_CLOSED
from custom_components.villa_pool.supervisor import (
    CoverTracker,
    cover_target_hours,
    decide,
    target_hours,
)

from .helpers import at, memory, state
from .test_engine import setup_pool, tick

UTC = timezone.utc
T0 = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
H = timedelta(hours=1)
COVER = "binary_sensor.pool_telo_chiuso"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Override conftest's: the recorder's database URL has to be resolved
    BEFORE `hass` is built, and `enable_custom_integrations` builds it."""
    yield


def covered(day: int | None, **kw):
    return state(at(2026, 9, 16, 12, 0), cover_closed=True,
                 cover_closed_days=day, **kw)


# --- the ramp ----------------------------------------------------------------


class TestTheRamp:
    @pytest.mark.parametrize(("day", "hours"), [(0, 8.0), (1, 6.0), (2, 4.0),
                                                 (3, 2.0), (4, 2.0), (30, 2.0)])
    def test_default_steps_are_8_6_4_2(self, day, hours):
        assert cover_target_hours(covered(day)) == pytest.approx(hours)

    def test_a_different_start_stays_proportional(self):
        steps = [cover_target_hours(covered(d, target_chlorine_hours=6.0))
                 for d in range(4)]
        assert steps == pytest.approx([6.0, 14 / 3, 10 / 3, 2.0])

    def test_a_different_floor_stays_proportional(self):
        steps = [cover_target_hours(covered(d, cover_min_chlorine_hours=3.5))
                 for d in range(4)]
        assert steps == pytest.approx([8.0, 6.5, 5.0, 3.5])

    def test_a_shorter_ramp(self):
        steps = [cover_target_hours(covered(d, cover_ramp_days=2))
                 for d in range(4)]
        assert steps == pytest.approx([8.0, 5.0, 2.0, 2.0])

    def test_a_zero_day_ramp_drops_to_the_floor_at_once(self):
        assert cover_target_hours(covered(0, cover_ramp_days=0)) == 2.0

    def test_a_floor_above_the_target_never_raises_it(self):
        """A cover can lower the demand, never raise it."""
        st = covered(5, target_chlorine_hours=1.5)
        assert cover_target_hours(st) == 1.5

    def test_open_or_unknown_is_the_full_target(self):
        for closed in (False, None):
            st = state(at(), cover_closed=closed, cover_closed_days=5)
            assert cover_target_hours(st) is None
            assert target_hours(st) == 8.0

    def test_closed_with_no_day_count_is_day_0(self):
        """Errs towards MORE chlorine."""
        assert cover_target_hours(covered(None)) == 8.0

    def test_winter_keeps_its_own_dose(self):
        st = covered(5, mode="winter")
        assert target_hours(st) == 2.0      # winter_chlorine_hours, not the floor


class TestTheCellIsNeverCutByTheCover:
    def test_the_floor_runs_the_cell(self):
        now = at(2026, 9, 16, 12, 0)
        dec, _ = decide(covered(10, chlorine_hours_today=0.5), memory(now))
        assert dec.chlorine_on is True
        assert "cover floor: target 2.0 h" in dec.reason

    def test_a_zero_floor_is_the_owners_choice(self):
        now = at(2026, 9, 16, 12, 0)
        st = covered(10, cover_min_chlorine_hours=0.0)
        dec, _ = decide(st, memory(now))
        assert dec.chlorine_on is False

    def test_the_target_does_not_move_during_the_day(self):
        """Calendar days, so a whole day is counted against one target: the
        ramp cannot switch the cell off at 15:00 because the cover happened
        to close at 15:00 yesterday."""
        now = at(2026, 9, 16, 9, 0)
        targets = set()
        for minutes in range(0, 12 * 60, 30):
            st = covered(1).with_now(now + timedelta(minutes=minutes))
            targets.add(decide(st, memory(st.now))[0].detail["chlorine_target_h"])
        assert targets == {6.0}


# --- closed for how long -----------------------------------------------------


class TestTheTracker:
    def track(self, history, now):
        t = CoverTracker(grace_s=3600)
        t.seed(history, now)
        return t

    def test_measures_from_the_end_of_the_last_opening(self):
        t = self.track([("off", T0), ("on", T0 + H)], T0 + 50 * H)
        assert t.last_open == T0 + H
        assert t.closed(T0 + 50 * H) is True
        assert t.closed_for_h(T0 + 50 * H) == pytest.approx(49.0)

    def test_a_restart_is_not_an_opening(self):
        """What 25/9 15:49 did to `last_changed`."""
        history = [("off", T0), ("on", T0 + H),
                   ("unavailable", T0 + 72 * H), ("on", T0 + 72 * H + 30 * 1e-6 * H)]
        t = self.track(history, T0 + 80 * H)
        assert t.last_open == T0 + H

    def test_an_unavailable_blip_is_not_an_opening(self):
        history = [("off", T0), ("on", T0 + H), ("unavailable", T0 + 5 * H),
                   ("on", T0 + 5.1 * H)]
        t = self.track(history, T0 + 10 * H)
        assert t.last_open == T0 + H
        assert t.bound is False

    def test_no_opening_in_the_history_is_a_lower_bound(self):
        t = self.track([("on", T0), ("unavailable", T0 + H), ("on", T0 + 2 * H)],
                       T0 + 100 * H)
        assert t.last_open == T0
        assert t.bound is True

    def test_open_right_now(self):
        t = self.track([("on", T0), ("off", T0 + H)], T0 + 5 * H)
        assert t.last_open == T0 + 5 * H
        assert t.closed(T0 + 5 * H) is False
        assert t.closed_for_h(T0 + 5 * H) is None

    def test_live_opening_resets_and_closing_counts_from_it(self):
        t = self.track([("on", T0)], T0 + 50 * H)
        t.observe("off", T0 + 51 * H)
        t.observe("on", T0 + 52 * H)
        assert t.last_open == T0 + 51 * H
        assert t.bound is False

    def test_a_gap_holds_the_last_reading_inside_the_grace(self):
        t = self.track([("off", T0), ("on", T0 + H)], T0 + 10 * H)
        t.observe("unavailable", T0 + 10 * H + timedelta(minutes=30))
        assert t.closed(T0 + 10 * H + timedelta(minutes=30)) is True

    def test_a_dead_sensor_goes_unknown_after_the_grace(self):
        """A pool nobody can see must not stay on its floor for ever."""
        t = self.track([("off", T0), ("on", T0 + H)], T0 + 10 * H)
        for minutes in range(0, 181, 60):
            t.observe("unavailable", T0 + 10 * H + timedelta(minutes=minutes))
        assert t.closed(T0 + 13 * H) is None
        assert t.closed_for_h(T0 + 13 * H) is None

    def test_a_gap_at_the_end_of_the_history_starts_the_grace_there(self):
        t = self.track([("off", T0), ("on", T0 + H), ("unavailable", T0 + 5 * H)],
                       T0 + 10 * H)
        assert t.closed(T0 + 5.5 * H) is True
        assert t.closed(T0 + 10 * H) is None

    def test_no_history_at_all_starts_from_the_first_live_reading(self):
        t = CoverTracker(grace_s=3600)
        t.observe("on", T0)
        assert t.last_open == T0
        assert t.bound is True


# --- the engine, against a real recorder --------------------------------------


async def _record_a_closure_then_a_restart(hass: HomeAssistant, freezer) -> None:
    """Open on 22/9 10:00, shut at 10:01, then a restart on 25/9 that rewrites
    `last_changed` — exactly the live history."""
    await hass.config.async_set_time_zone("Europe/Rome")
    freezer.move_to("2026-09-22 08:00:00+00:00")        # 10:00 local
    hass.states.async_set(COVER, "off")
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(COVER, "on")
    await async_wait_recording_done(hass)
    freezer.move_to("2026-09-25 13:49:00+00:00")
    hass.states.async_set(COVER, "unavailable")
    freezer.tick(timedelta(seconds=5))
    hass.states.async_set(COVER, "on")
    await async_wait_recording_done(hass)
    freezer.move_to("2026-09-29 11:00:00+00:00")        # 13:00 local


async def test_days_come_from_the_last_opening_not_last_changed(
    recorder_mock, hass: HomeAssistant, freezer
) -> None:
    await _record_a_closure_then_a_restart(hass, freezer)
    entry = await setup_pool(hass, data={CONF_COVER_CLOSED: COVER})
    await tick(hass, freezer=freezer)
    # The history read runs on the recorder's executor, which
    # `async_block_till_done` does not wait for.
    await async_wait_recording_done(hass)
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine
    st = engine.last_state
    assert st.cover_closed is True
    assert st.cover_closed_days == 7                  # 22/9 -> 29/9, not 25/9
    assert engine.decision.detail["chlorine_target_h"] == 2.0
    attrs = hass.states.get("sensor.pool_cover_closed_for").attributes
    assert attrs["lower_bound"] is False
    assert attrs["last_seen_open"].startswith("2026-09-22T08:01")


async def test_without_the_recorder_it_falls_back_towards_more_chlorine(
    hass: HomeAssistant, freezer
) -> None:
    """No history: the live `last_changed` is the only evidence, and it is a
    lower bound — so fewer days, more chlorine, never the other way round."""
    await hass.config.async_set_time_zone("Europe/Rome")
    freezer.move_to("2026-09-29 11:00:00+00:00")
    hass.states.async_set(COVER, "on")
    entry = await setup_pool(hass, data={CONF_COVER_CLOSED: COVER})
    await tick(hass, freezer=freezer)
    st = entry.runtime_data.engine.last_state
    assert st.cover_closed is True
    assert st.cover_closed_days == 0
    assert entry.runtime_data.engine.decision.detail["chlorine_target_h"] == 8.0


async def test_an_opening_seen_live_resets_the_ramp(
    hass: HomeAssistant, freezer
) -> None:
    await hass.config.async_set_time_zone("Europe/Rome")
    freezer.move_to("2026-09-29 11:00:00+00:00")
    hass.states.async_set(COVER, "on")
    entry = await setup_pool(hass, data={CONF_COVER_CLOSED: COVER})
    engine = entry.runtime_data.engine
    await tick(hass, freezer=freezer)             # seeded: nothing but "on"
    engine.cover.last_open = dt_util.utcnow() - timedelta(days=5)
    engine.cover.bound = False
    await tick(hass, freezer=freezer)
    assert engine.last_state.cover_closed_days == 5
    hass.states.async_set(COVER, "off")
    await tick(hass, freezer=freezer)
    assert engine.last_state.cover_closed is False
    hass.states.async_set(COVER, "on")
    await tick(hass, freezer=freezer)
    assert engine.last_state.cover_closed_days == 0


async def test_no_cover_configured_leaves_every_rule_inert(
    hass: HomeAssistant,
) -> None:
    entry = await setup_pool(hass)
    await tick(hass)
    st = entry.runtime_data.engine.last_state
    assert st.cover_closed is None
    assert st.cover_closed_days is None

