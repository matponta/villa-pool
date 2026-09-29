"""How long has the cover been shut? (STORY §5.3, amendment 2026-09-29)

The chlorine ramp counts days since the cover was last seen OPEN. That is a
different question from "when did the helper last change", and the difference
is exactly what broke the old rule:

* **A restart resets `last_changed`.** Seen live: `binary_sensor.pool_telo_chiuso`
  read `on` continuously from 22/9, and its `last_changed` said 25/9 15:49 —
  the restart. Measured from that, a pool shut for a week is three days shut.
* **`unavailable` resets it too.** `on -> unavailable -> on` is two state
  changes and zero openings. A BLE sensor that drops one advertisement would
  otherwise restart the ramp at the full target.

So the tracker keeps two facts, both from evidence the sensor actually gave:

* `last_open` — the latest instant the cover was seen open. Seeded at startup
  from the recorder, then advanced live every tick the helper reads `off`.
  When the history holds no opening at all it is the OLDEST instant we have,
  marked `bound`: "closed at least since", which errs towards fewer days
  counted and therefore more chlorine.
* the last known reading, which survives an `unavailable` gap for
  `grace_s`. Past that the cover is unknown and every cover rule goes inert —
  the PdC's "a read gap is not evidence", applied the other way round: a dead
  sensor must not keep a pool nobody can see on its 2 h floor for ever.

Pure: no HA imports. The engine feeds it `(state, last_changed)` pairs from the
recorder and the live state each tick; everything is in UTC.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# The helper's own states. `on` = closed (it inverts the Shelly window sensor).
CLOSED = "on"
OPEN = "off"


@dataclass
class CoverTracker:
    """Last-seen-open and last-known reading for the cover helper."""

    grace_s: float
    last_open: datetime | None = None
    # True while `last_open` is only a lower bound on the closure (no opening
    # anywhere in the history we could see).
    bound: bool = False
    _known: bool | None = None
    _known_until: datetime | None = None

    def seed(self, history: list[tuple[str, datetime]], now: datetime) -> None:
        """Rebuild both facts from a chronological list of state changes.

        An `off` stays open until the NEXT state began, so the instant it was
        last seen open is that next state's timestamp — or `now`, if it is the
        latest state. A known reading is likewise valid until whatever came
        after it.
        """
        self.last_open = None
        self.bound = False
        self._known = None
        self._known_until = None
        for i, (value, _at) in enumerate(history):
            until = history[i + 1][1] if i + 1 < len(history) else now
            if value == OPEN:
                self.last_open = until
            if value in (OPEN, CLOSED):
                self._known = value == CLOSED
                self._known_until = until
        if self.last_open is None and history:
            self.last_open = history[0][1]
            self.bound = True

    def observe(self, value: str | None, now: datetime) -> None:
        """One live reading. Anything but `on`/`off` is a gap and changes nothing."""
        if value == OPEN:
            self.last_open = now
            self.bound = False
        elif value == CLOSED:
            if self.last_open is None:
                # First sight of a closed cover with no history at all: the
                # closure starts no later than now.
                self.last_open = now
                self.bound = True
        else:
            return
        self._known = value == CLOSED
        self._known_until = now

    def closed(self, now: datetime) -> bool | None:
        """True / False from the last reading inside the grace, else None."""
        if self._known is None or self._known_until is None:
            return None
        if (now - self._known_until).total_seconds() > self.grace_s:
            return None
        return self._known

    def closed_for_h(self, now: datetime) -> float | None:
        """Hours since the cover was last seen open, while it reads closed."""
        if self.closed(now) is not True or self.last_open is None:
            return None
        return max(0.0, (now - self.last_open).total_seconds() / 3600.0)
