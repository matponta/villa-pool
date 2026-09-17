"""Idempotent writing — the pure half (STORY §8 step 2).

Deciding *what* the pool should be doing is `law.decide()`. Deciding *whether
to send a command about it* is a separate question, and this module answers it
for one lever at a time. It is pure, like everything else in this package, so
the answer can be unit-tested without a running Home Assistant.

Three rules, all of them learned rather than invented:

* **Idempotent.** A command is sent only when the device disagrees with us. A
  supervisor that re-sends `switch.turn_on` every 60 s to a switch that is
  already on fills the logbook with noise, and on a cloud-backed device it
  spends the API budget it will need when something is actually wrong.

* **Re-assert before concluding "manual".** A device that has not followed a
  command has not necessarily been overridden — it may simply not have got
  there yet (the tuya-local switch round-trips through a local bridge, the
  aquatemp fork through a cloud). So a disagreement inside the settle window is
  patience, not evidence; and the first disagreement *after* it earns exactly
  one more command before we draw any conclusion.

* **Then stop fighting.** After the re-assert fails the lever is latched: the
  supervisor keeps deciding and keeps reporting, but stops writing to that one
  lever. A controller that loses an argument with a human twice a minute for a
  week is worse than one that says "you have this" and gets out of the way.
  The latch clears by itself the moment our intent changes (a new decision
  deserves a fresh command) or the device comes back to where we wanted it.

The latch is deliberately NOT called "manual" in the state it records, because
the two causes are indistinguishable from here: a human at the pool house and
a device that has stopped accepting commands produce exactly the same evidence.
The owner-facing message says so.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

# What `plan()` decided to do about one lever this tick.
WRITE: str = "write"          # send the command now
HOLD: str = "hold"            # nothing to do — we agree, or we have no opinion
SETTLING: str = "settling"    # commanded recently; give the device time
BLIND: str = "blind"          # cannot read the device: judge nothing, write nothing
LATCHED: str = "latched"      # someone or something else owns this lever


@dataclass(frozen=True)
class Lever:
    """What the supervisor knows about one controllable thing.

    Lives in the HA layer (one per lever, rebuilt empty on restart) rather than
    in the control `Memory`: an empty lever after a restart is exactly right,
    because the first tick re-compares against the real device and a pool that
    already matches the intent is not written to at all.
    """

    desired: object | None = None
    wrote_at: datetime | None = None
    # Consecutive commands of THIS desired value that the device did not adopt.
    writes: int = 0
    latched: bool = False
    latched_since: datetime | None = None
    confirmed_at: datetime | None = None


@dataclass(frozen=True)
class Plan:
    """One lever's verdict for one tick."""

    action: str
    value: object | None = None
    reason: str = ""

    @property
    def writes(self) -> bool:
        return self.action == WRITE


def same(a: object | None, b: object | None, tolerance: float | None = None) -> bool:
    """Do these two readings mean the same thing?

    `tolerance` is for the levers read back as floats — a setpoint written as
    27.0 can come back as 27 or 26.999, and treating that as a disagreement
    would re-command the machine forever.
    """
    if a is None or b is None:
        return False
    if tolerance is not None:
        try:
            return abs(float(a) - float(b)) <= tolerance
        except (TypeError, ValueError):
            return False
    return a == b


def plan(
    lever: Lever,
    *,
    desired: object | None,
    actual: object | None,
    mono: datetime,
    settle_s: int,
    attempts: int = 2,
    tolerance: float | None = None,
) -> tuple[Plan, Lever]:
    """Should we command this lever right now? Returns (plan, next lever).

    `mono` is the UTC anchor (`PoolState.mono`) — everything here is a
    duration, so it must not be local time (see `model`).

    `attempts` is how many commands one intent is worth before the lever is
    latched. The default of 2 is the STORY's "re-assert before concluding
    manual": one command, and if that does not take, one more.
    """
    # No opinion this tick — e.g. the pump speed while the pump is meant to be
    # off. Forget the lever so the next real intent counts as a fresh one.
    if desired is None:
        return Plan(HOLD, None, "no intent"), Lever()

    # Cannot read the device. This is the `unavailable` rule of §5.2 applied to
    # every lever: a read gap is not evidence of anything, so we neither write
    # into it nor hold it against the device afterwards.
    if actual is None:
        return Plan(BLIND, desired, "device unreadable — holding"), lever

    # The device already agrees, so there is nothing to send — and this is
    # checked BEFORE "is this a new intent", which is the difference between a
    # restart that writes nothing and a restart that re-commands a pool already
    # doing exactly what it should. Any latch is over too: the argument ended
    # by itself.
    if same(actual, desired, tolerance):
        return (
            Plan(HOLD, desired, "already there"),
            Lever(desired=desired, writes=0, confirmed_at=mono),
        )

    # A new intent earns a command immediately. This is what makes the safety
    # writes prompt: when the pump faults and the PdC must go `off` "within one
    # tick" (§7.5), `off` is a new intent and is not rate-limited by the settle
    # window, which protects a *repeat* of a command rather than a change of
    # mind.
    if not same(desired, lever.desired, tolerance):
        return (
            Plan(WRITE, desired, "new intent"),
            Lever(desired=desired, wrote_at=mono, writes=1),
        )

    if lever.latched:
        return Plan(LATCHED, desired, "not ours to drive"), lever

    if lever.wrote_at is not None and (mono - lever.wrote_at) < timedelta(
        seconds=settle_s
    ):
        return Plan(SETTLING, desired, "commanded, waiting"), lever

    if lever.writes >= attempts:
        return (
            Plan(LATCHED, desired,
                 f"commanded {lever.writes}x and it is still {actual!r} — "
                 "manual override, or the device is not accepting commands"),
            replace(lever, latched=True, latched_since=mono),
        )

    return (
        Plan(WRITE, desired, f"re-asserting (attempt {lever.writes + 1})"),
        replace(lever, wrote_at=mono, writes=lever.writes + 1),
    )
