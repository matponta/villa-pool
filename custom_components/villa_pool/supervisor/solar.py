"""Solar headroom: hysteresis + entry dwell.

STORY §3 fixes the thresholds (3000 W on / 2500 W off) and §4 asks for a
`binary_sensor.pool_solar_ok` carrying "hysteresis + 10 min dwell".

The asymmetry is deliberate and is what acceptance criterion §7.3 pins:

* **Rising** edge is slow — headroom must hold above the threshold for
  `SOLAR_ON_DWELL_S` before `solar_ok` goes True. A 5 min pulse is not a
  reason to start a compressor; a 10 min one is.
* **Falling** edge is immediate — `solar_ok` drops the moment the (hysteretic)
  headroom fails. The *patience* on the way down lives in the PdC state machine
  instead, as "not solar_ok for 15 min", so a 12 min cloud cannot stop a run.

Putting the down-dwell in the state machine rather than here is what keeps
those two numbers independent: the sensor answers "is there sun now?", the
machine answers "has it been gone long enough to matter?".
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from ..const import SOLAR_ON_DWELL_S
from .model import Memory


def raw_headroom_ok(headroom_w: float | None, on_w: float, off_w: float,
                    *, currently_ok: bool) -> bool:
    """The hysteretic threshold test. Unknown headroom is never 'ok'."""
    if headroom_w is None:
        return False
    return headroom_w >= (off_w if currently_ok else on_w)


def solar_step(mem: Memory, now: datetime, headroom_w: float | None,
               on_w: float, off_w: float) -> Memory:
    """Advance the solar latches one tick and return the updated Memory."""
    raw_ok = raw_headroom_ok(headroom_w, on_w, off_w, currently_ok=mem.solar_ok)

    if raw_ok:
        raw_since = mem.solar_raw_since or now
        dwell_met = (now - raw_since) >= timedelta(seconds=SOLAR_ON_DWELL_S)
        solar_ok = mem.solar_ok or dwell_met
    else:
        raw_since = None
        solar_ok = False

    if solar_ok:
        lost_since = None
    else:
        lost_since = mem.solar_lost_since or now

    return replace(
        mem,
        solar_ok=solar_ok,
        solar_raw_since=raw_since,
        solar_lost_since=lost_since,
    )


def solar_lost_for(mem: Memory, now: datetime) -> timedelta:
    """How long `solar_ok` has been False (zero while it is True)."""
    if mem.solar_ok or mem.solar_lost_since is None:
        return timedelta(0)
    return now - mem.solar_lost_since
