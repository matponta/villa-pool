"""Daily windows. The ONE place midnight wrap-around is interpreted.

STORY §3: the windows are the same every day, so they are plain `time` settings
rather than schedules. The PdC grid window crosses midnight by default
(23:00 -> 07:00), which is the whole reason this module exists: every other
module asks `in_window(...)` and never compares times itself.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta


def in_window(now: time | datetime, start: time, end: time) -> bool:
    """Is `now` inside [start, end)?

    A window whose end is not after its start is read as crossing midnight, so
    23:00 -> 07:00 covers 23:30 and 02:00 but not 12:00. `start == end` is an
    EMPTY window, not a 24 h one: the owner clearing a window to disable it is
    far more likely than wanting a degenerate all-day window, and "always on"
    is the dangerous way to guess wrong.
    """
    t = now.time() if isinstance(now, datetime) else now
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def window_length(start: time, end: time) -> timedelta:
    """Duration of a (possibly midnight-crossing) window."""
    a = timedelta(hours=start.hour, minutes=start.minute, seconds=start.second)
    b = timedelta(hours=end.hour, minutes=end.minute, seconds=end.second)
    if start == end:
        return timedelta(0)
    return b - a if start < end else timedelta(days=1) - a + b


def since_start(now: datetime, start: time) -> timedelta:
    """How long ago today's `start` occurred (never negative).

    Used by the winter slot: "run `winter_hours` from 12:00". If `start` is
    still in the future today, the answer is a full day ago — i.e. outside any
    sane slot length — which keeps the slot closed until it opens.
    """
    anchor = now.replace(
        hour=start.hour, minute=start.minute, second=start.second, microsecond=0
    )
    if anchor > now:
        anchor -= timedelta(days=1)
    return now - anchor


def in_slot(now: datetime, start: time, hours: float) -> bool:
    """Is `now` inside the `hours`-long slot that opens at `start`?"""
    if hours <= 0:
        return False
    return since_start(now, start) < timedelta(hours=hours)
