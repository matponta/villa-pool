"""`decide()` — the one entry point, and the §5.5 priority ladder.

The whole control law is a pure function `(PoolState, Memory) -> (Decision,
Memory)`. Nothing here reads a clock, touches Home Assistant or mutates its
inputs, so a test is just a fold over a list of snapshots and the 24 h dry-run
can be replayed offline against the logs.

Priority ladder (STORY §5.5, first match wins) — this is the order the *reason*
is chosen in, and the order the interlocks are applied in:

    1 maintenance/manual  2 fault or pump not in marcia  3 cover closed > 24 h
    4 antifreeze  5 pool_in_use  6 SOLAR  7 GRID  8 targets/catch-up
    9 pump window
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from ..const import (
    MODE_CLOSED,
    MODE_FILTRATION_ONLY,
    MODE_MANUAL,
    MODE_WINTER,
    MIN_TEMP_HYSTERESIS,
    PDC_BLOCKED,
    PDC_BLOCKED_MODES,
    PDC_GRID,
    PDC_MIN_OFF_S,
    PDC_MIN_ON_S,
    PDC_OFF,
    PDC_SOLAR,
    POSTRUN_S,
    PUMP_CONFIRM_S,
)
from .chlorine import catchup_active, chlorine_decision, cover_cutoff, hours_missing
from .model import Decision, Memory, PoolState
from .pdc import RUNNING_STATES, grid_conditions, pdc_step, solar_conditions
from .pump import (
    antifreeze_step,
    confirm_step,
    is_confirmed,
    postrun_active,
    postrun_step,
    pump_plan,
)
from .solar import solar_step
from .windows import in_slot, in_window

# Modes in which the supervisor drives nothing at all (rung 1 of the ladder).
FROZEN_MODES = (MODE_MANUAL, MODE_CLOSED)


def decide(state: PoolState, mem: Memory) -> tuple[Decision, Memory]:
    """One supervisor tick. Returns what we intend and the advanced Memory."""
    cfg = state.config
    w = cfg.windows
    # Two clocks (see `model.PoolState`): `now` answers "where in the day are
    # we" and nothing else; `mono` (UTC) is what every timer is measured
    # against, so a DST fold cannot make an elapsed interval go negative.
    now = state.now
    mono = state.mono

    # --- advance the latches first ------------------------------------------
    mem = confirm_step(mem, mono, state.pump_running)
    mem = solar_step(mem, mono, state.headroom_w, cfg.solar_on_w, cfg.solar_off_w)
    mem = antifreeze_step(
        mem, state.outdoor_temp, cfg.antifreeze_on_c, cfg.antifreeze_off_c
    )
    pump_confirmed = is_confirmed(mem, mono)
    was_running = mem.pdc_state in RUNNING_STATES

    # --- rung 1: the supervisor is switched out of the loop entirely ---------
    if state.maintenance or state.mode in FROZEN_MODES:
        why = "maintenance" if state.maintenance else f"mode {state.mode}"
        mem = replace(mem, pdc_state=PDC_BLOCKED, pdc_since=mem.pdc_since or mono)

        # AMENDMENT 2026-09-17 (owner): antifreeze outranks `manual` and
        # `closed`. §5.5 as written put both above antifreeze, which meant the
        # supervisor stopped protecting the pipes in `closed` — the mode the
        # pool spends the whole winter in, unattended. The cost of being wrong
        # in one direction is a stopped pump for a few hours; in the other it
        # is burst pipes.
        #
        # `maintenance` deliberately still freezes everything, antifreeze
        # included: it means someone is physically at the pool, possibly with
        # it drained or the valves shut, and starting a pump under them is a
        # hazard rather than a protection. It also expires by itself after 4 h,
        # so the exposure is bounded — `closed` lasts months.
        if mem.antifreeze_active and not state.maintenance:
            mem = replace(mem, antifreeze_owns_pump=True)
            pump_on, pump_speed, requesters = pump_plan(
                cfg=cfg, window_open=False, winter_slot=False,
                pdc_wants_flow=False, chlorine_wants=False, pool_in_use=False,
                antifreeze=True, catchup=False, postrun=False,
            )
            return (
                Decision(
                    pump_on=pump_on,
                    pump_speed=pump_speed,
                    pdc_state=PDC_BLOCKED,
                    # The cell is cut as part of antifreeze (§3) — and it has
                    # to be, or the §6 guardrail would refuse to take the pump
                    # down to `antifreeze_speed` with it still enabled.
                    chlorine_on=False,
                    reason=(
                        f"ANTIFREEZE overrides {why}: pump ON {pump_speed}% "
                        f"(antifreeze); chlorine OFF; PdC left alone ({why})."
                    ),
                    requesters=requesters,
                    blocked_reason=why,
                    # The heat pump stays the owner's while the mode is theirs:
                    # it is blocked either way, and writing `off` to it would
                    # be more than freeze protection asked for.
                    pdc_write=False,
                    detail={"frozen": True, "antifreeze": True,
                            "antifreeze_override": why},
                ),
                mem,
            )

        # The freeze is over but the pump is one WE started. Stopping it is not
        # "driving the pool" — it is finishing what antifreeze began. The
        # actuator is idempotent, so this is one command and then silence; and
        # keeping it asserted rather than firing once means a command that does
        # not land is still re-asserted instead of leaving the pump running
        # until the owner next looks at the pool house.
        if mem.antifreeze_owns_pump and not state.maintenance:
            return (
                Decision(
                    pump_on=False,
                    pump_speed=None,
                    pdc_state=PDC_BLOCKED,
                    chlorine_on=False,
                    reason=(
                        f"Antifreeze released in {why}: stopping the pump the "
                        "supervisor started. Nothing else is driven."
                    ),
                    blocked_reason=why,
                    pdc_write=False,
                    detail={"frozen": True, "antifreeze": False,
                            "antifreeze_owns_pump": True},
                ),
                mem,
            )

        return (
            Decision(
                pump_on=False,
                pump_speed=None,
                pdc_state=PDC_BLOCKED,
                chlorine_on=False,
                reason=f"Frozen: {why} — supervisor is not driving anything.",
                blocked_reason=why,
                pdc_write=False,
                # "Freezes all actuation" (§4), which is hands-off, NOT
                # "switch everything off": the owner flipped maintenance to
                # work on the pool, and stopping the pump under them is the
                # opposite of what they asked for.
                actuate=False,
                detail={"frozen": True, "antifreeze": mem.antifreeze_active},
            ),
            mem,
        )

    # --- the PdC machine (rungs 2, 6, 7) -------------------------------------
    # filtration_only is a no-heating mode like the others; it belongs in the
    # machine's own blocked set, NOT as a post-hoc override of the answer (that
    # left Memory in SOLAR/GRID while the decision said BLOCKED).
    pdc_state, setpoint, pdc_reason, may_write, mem = pdc_step(
        state, mem, pump_confirmed=pump_confirmed,
        pdc_blocked_modes=(*PDC_BLOCKED_MODES, MODE_FILTRATION_ONLY),
    )
    mem = postrun_step(
        mem, mono,
        pdc_was_running=was_running,
        pdc_is_running=pdc_state in RUNNING_STATES,
        postrun_s=POSTRUN_S,
    )

    antifreeze = mem.antifreeze_active
    # Out of the frozen modes the ordinary law owns the pump again, so the
    # antifreeze hand-back is complete.
    mem = replace(mem, antifreeze_owns_pump=False)

    # --- chlorine (rungs 3, 4, 5, 8) -----------------------------------------
    # The pump follows demand, so the chlorinator's *demand* has to be known
    # before the pump is sized: ask what it would want with flow available,
    # then decide what it actually gets once the speed is known.
    chlorine_demand, _ = chlorine_decision(
        state, pump_confirmed=True, pump_speed=None,
        pdc_state=pdc_state, antifreeze=antifreeze,
    )

    # --- the pump (rung 9 + everything above that needs flow) ----------------
    if state.mode == MODE_WINTER:
        window_open = False
        winter_slot = in_slot(now, w.winter_start, cfg.winter_hours)
    else:
        window_open = in_window(now, w.pump_start, w.pump_end)
        winter_slot = False

    pdc_wants_flow = pdc_state in RUNNING_STATES or _pdc_would_start(
        state, mem, pump_confirmed
    )
    catchup = catchup_active(state) and state.mode != MODE_WINTER

    pump_on, pump_speed, requesters = pump_plan(
        cfg=cfg,
        window_open=window_open,
        winter_slot=winter_slot,
        pdc_wants_flow=pdc_wants_flow,
        chlorine_wants=chlorine_demand,
        pool_in_use=state.pool_in_use and state.mode != MODE_WINTER,
        antifreeze=antifreeze,
        catchup=catchup,
        postrun=postrun_active(mem, mono),
    )

    chlorine_on, chlorine_reason = chlorine_decision(
        state, pump_confirmed=pump_confirmed, pump_speed=pump_speed,
        pdc_state=pdc_state, antifreeze=antifreeze,
    )
    reason = _reason_line(
        state=state, mem=mem, pdc_state=pdc_state, pdc_reason=pdc_reason,
        pump_on=pump_on, pump_speed=pump_speed, requesters=requesters,
        chlorine_on=chlorine_on, chlorine_reason=chlorine_reason,
        antifreeze=antifreeze, pump_confirmed=pump_confirmed,
    )

    return (
        Decision(
            pump_on=pump_on,
            pump_speed=pump_speed,
            pdc_state=pdc_state,
            pdc_setpoint=setpoint,
            chlorine_on=chlorine_on,
            reason=reason,
            requesters=requesters,
            blocked_reason=(
                pdc_reason.removeprefix("PdC blocked: ")
                if pdc_state == PDC_BLOCKED else None
            ),
            pdc_write=may_write,
            detail={
                "pdc_reason": pdc_reason,
                "chlorine_reason": chlorine_reason,
                "solar_ok": mem.solar_ok,
                "pump_confirmed": pump_confirmed,
                "antifreeze": antifreeze,
                "chlorine_hours_missing": round(hours_missing(state), 2),
                "catchup": catchup,
                "cover_cutoff": cover_cutoff(state),
            },
        ),
        mem,
    )


def _pdc_would_start(state: PoolState, mem: Memory, pump_confirmed: bool) -> bool:
    """Would the PdC start if only the pump were already confirmed in marcia?

    This is what makes the start sequence work: the pump has to be asked for
    BEFORE the PdC can be allowed to run, or the hydraulic interlock and the
    pump's own demand-following would deadlock each other.
    """
    if pump_confirmed or not state.pdc_available:
        return False
    if state.maintenance or state.mode in (*PDC_BLOCKED_MODES, MODE_FILTRATION_ONLY):
        return False
    if state.pdc_fault or state.pump_problem:
        return False
    grid_ok, _ = grid_conditions(state)
    return solar_conditions(state, mem) or grid_ok


def _reason_line(*, state, mem, pdc_state, pdc_reason, pump_on, pump_speed,
                 requesters, chlorine_on, chlorine_reason, antifreeze,
                 pump_confirmed) -> str:
    """One line the owner reads first when something looks wrong (STORY §4).

    Deliberately flat and boring: actuator, what it is doing, why. No jargon
    the dashboard would have to decode.
    """
    pump_txt = (
        f"pump ON {pump_speed}% ({'+'.join(requesters)})" if pump_on
        else "pump OFF (no demand)"
    )
    if pdc_state == PDC_OFF:
        pdc_txt = f"PdC off — {pdc_reason}"
    elif pdc_state == PDC_BLOCKED:
        pdc_txt = pdc_reason if pdc_reason.startswith("PdC") else f"PdC blocked: {pdc_reason}"
    else:
        pdc_txt = f"PdC {pdc_state} — {pdc_reason}"
    cl_txt = f"chlorine {'ON' if chlorine_on else 'OFF'} — {chlorine_reason}"
    prefix = "ANTIFREEZE: " if antifreeze else ""
    return f"{prefix}{pump_txt}; {pdc_txt}; {cl_txt}"


def restore_memory(
    *,
    mono: datetime,
    pdc_running: bool | None,
    water_temp: float | None,
    min_temp: float,
    grid_ok: bool,
    solar_ok: bool,
    pump_running: bool | None = None,
    outdoor_temp: float | None = None,
    antifreeze_off_c: float = 0.0,
) -> Memory:
    """Re-derive the Memory after a HA restart (STORY §6, §7.10).

    "On HA restart: restore settings; re-derive the PdC state from
    `pool_pdc_acceso` + water temp, do not assume OFF."

    `mono` is the UTC anchor (`PoolState.mono`), because everything stamped
    here is read back as a duration.

    The timers come back already satisfied (`pdc_since` a full MIN_ON ago,
    `pdc_last_stop` a full MIN_OFF ago) so that a restart mid-run is a
    *continuation*: the next tick may legitimately stop the machine, but it can
    never read as a fresh start and put another cycle on the compressor.

    The pump confirmation is adopted too when the pump reads in marcia. The 60 s
    debounce exists to filter a *fresh transition* on a sensor with no
    `delay_on` of its own — it is not a reason to re-prove a steady state that
    demonstrably predates the restart. Without this the first minute after every
    restart reads as "pump not in marcia", which would block the PdC and write
    `off` -> `heat`: precisely the spurious extra start §7.10 forbids.

    **`grid_ok` is `grid_conditions(state, running=True)`'s own answer**, and
    this function no longer forms an opinion about windows, bands or the
    guaranteed minimum. It used to ask them itself, and the copies drifted
    twice in two days: first when §5.4's daytime top-up added a second
    authorising window (a restart mid top-up re-derived OFF while the machine
    was genuinely heating), then when the 2026-09-18 amendment made the band
    veto night-only.

    The invariant the parameter buys is the one that matters: **the supervisor
    adopts exactly what the law would authorise this tick**. Adopting anything
    else is worse than adopting nothing — the next tick refuses it and `_stop`
    fires, so the supervisor stops a run it never began and stamps a MIN_OFF the
    compressor has not earned. `running=True` is the correct side of §5.2's
    hysteresis for a machine that is already heating.

    **Antifreeze is re-derived against the RELEASE threshold**, not the engage
    one. The latch is history we cannot recover: at +1 °C, inside the 0..+2
    band, a restart cannot tell whether antifreeze was running. Re-deriving
    from `antifreeze_on_c` would answer "no" and stop the pump in the middle of
    a cold snap; re-deriving from `antifreeze_off_c` answers "yes" and costs
    ~22 W until the air passes +2. That is the same asymmetry `antifreeze_step`
    already applies to an unknown temperature, for the same reason: when the
    question is whether the pipes are freezing, fail towards keeping the water
    moving.
    """
    if pump_running is None:
        pump_running = bool(pdc_running)
    hot_enough = water_temp is not None and water_temp >= min_temp + MIN_TEMP_HYSTERESIS

    if hot_enough or not pdc_running:
        pdc_state = PDC_OFF
    elif solar_ok:
        pdc_state = PDC_SOLAR
    elif grid_ok:
        pdc_state = PDC_GRID
    else:
        # Running, but nothing we recognise authorises it — most likely the
        # owner's own one-shot or a manual start. Treat it as OFF so the
        # supervisor does not adopt (and then "stop") a run it never began.
        pdc_state = PDC_OFF

    return Memory(
        pdc_state=pdc_state,
        pdc_since=mono - timedelta(seconds=PDC_MIN_ON_S),
        pdc_last_stop=mono - timedelta(seconds=PDC_MIN_OFF_S),
        solar_ok=solar_ok,
        solar_raw_since=mono - timedelta(hours=1) if solar_ok else None,
        solar_lost_since=None if solar_ok else mono,
        pump_running_since=(
            mono - timedelta(seconds=PUMP_CONFIRM_S) if pump_running else None
        ),
        antifreeze_active=(
            outdoor_temp is not None and outdoor_temp < antifreeze_off_c
        ),
    )
