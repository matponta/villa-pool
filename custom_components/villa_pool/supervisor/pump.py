"""The pump follows demand (STORY §5.1).

`pump_wanted = any(window, pdc, chlorine, pool_in_use, antifreeze, catchup)` and
the speed is the `max()` of what the active requesters ask for — so antifreeze
(30 %) running alongside filtration (80 %) gives 80 %, and antifreeze alone
gives 30 %.

Sequencing is the other half of this module's job, and it is the part that
protects hardware:

* **start**: pump ON -> wait for `pool_pompa_in_marcia` (60 s) -> PdC / chlorine;
* **stop**: PdC OFF -> post-run 5 min -> chlorine OFF -> pump OFF.

`pool_pompa_in_marcia` carries no `delay_on`/`delay_off` of its own (the UI
template flow has no such field, STORY §1), so the 60 s confirmation is this
integration's own job and lives in `confirm_step`.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from ..const import (
    PUMP_CONFIRM_S,
    REQ_ANTIFREEZE,
    REQ_CATCHUP,
    REQ_CHLORINE,
    REQ_IN_USE,
    REQ_PDC,
    REQ_POSTRUN,
    REQ_WINDOW,
    REQ_WINTER,
)
from .model import Memory


def confirm_step(mem: Memory, now: datetime, pump_running: bool | None) -> Memory:
    """Track how long the pump has genuinely been in marcia.

    `None` (unknown) is NOT treated as stopped — it leaves the anchor alone, so
    a momentary unavailable reading on the tuya-local switch does not restart
    the 60 s confirmation and drop the chlorinator with it.
    """
    if pump_running is True:
        return replace(mem, pump_running_since=mem.pump_running_since or now)
    if pump_running is False:
        return replace(mem, pump_running_since=None)
    return mem


def is_confirmed(mem: Memory, now: datetime) -> bool:
    """Has the pump been in marcia for the full confirmation window?"""
    if mem.pump_running_since is None:
        return False
    return (now - mem.pump_running_since) >= timedelta(seconds=PUMP_CONFIRM_S)


def antifreeze_step(
    mem: Memory, outdoor_temp: float | None, on_c: float, off_c: float
) -> Memory:
    """Latch antifreeze with hysteresis: engage below `on_c`, release at `off_c`.

    An unknown outdoor temperature holds the current latch rather than
    releasing it — failing towards "keep the water moving" is the safe
    direction when the question is whether the pipes are freezing.
    """
    if outdoor_temp is None:
        return mem
    if mem.antifreeze_active:
        return replace(mem, antifreeze_active=outdoor_temp < off_c)
    return replace(mem, antifreeze_active=outdoor_temp < on_c)


def pump_plan(
    *,
    cfg,
    window_open: bool,
    winter_slot: bool,
    pdc_wants_flow: bool,
    chlorine_wants: bool,
    pool_in_use: bool,
    antifreeze: bool,
    catchup: bool,
    postrun: bool,
) -> tuple[bool, int | None, tuple[str, ...]]:
    """Collect the demands and return (on, speed, requesters).

    Requesters come back ordered by the speed they asked for, highest first, so
    the reason line names the demand that actually set the speed.
    """
    demands: list[tuple[str, int]] = []
    if window_open:
        demands.append((REQ_WINDOW, cfg.filtration_speed))
    if winter_slot:
        demands.append((REQ_WINTER, cfg.filtration_speed))
    if pdc_wants_flow:
        demands.append((REQ_PDC, cfg.pdc_speed))
    if chlorine_wants:
        demands.append((REQ_CHLORINE, cfg.filtration_speed))
    if pool_in_use:
        demands.append((REQ_IN_USE, cfg.filtration_speed))
    if antifreeze:
        demands.append((REQ_ANTIFREEZE, cfg.antifreeze_speed))
    if catchup:
        demands.append((REQ_CATCHUP, cfg.filtration_speed))
    if postrun:
        demands.append((REQ_POSTRUN, cfg.pdc_speed))

    if not demands:
        return False, None, ()

    demands.sort(key=lambda d: d[1], reverse=True)
    speed = max(speed for _, speed in demands)
    return True, speed, tuple(name for name, _ in demands)


def postrun_step(
    mem: Memory, now: datetime, *, pdc_was_running: bool, pdc_is_running: bool,
    postrun_s: int,
) -> Memory:
    """Arm the post-run on the falling edge of the PdC."""
    if pdc_was_running and not pdc_is_running:
        return replace(mem, postrun_until=now + timedelta(seconds=postrun_s))
    return mem


def postrun_active(mem: Memory, now: datetime) -> bool:
    return mem.postrun_until is not None and now < mem.postrun_until
