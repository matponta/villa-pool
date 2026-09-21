"""Water chemistry: whether a reading is real, and what it may change (§5.3, §9).

Pure, like everything else in `supervisor/`: no HA imports, no clock reads.

Two jobs, kept apart on purpose:

* `read_quality` answers whether this tick's pH/ORP/EC mean anything at all.
  The tester sits in the SKIMMER, and with the pump off that is a small pocket
  behind the weir flap holding surface water — it does not read like the pool.
* `orp_trim` turns an ORP deficit into hours of extra chlorine, clamped.

**The trim moves the hours TARGET; it never switches the cell.** That is the
whole design, and three things fall out of it:

1. Hysteresis is free. Bang-bang on ORP would chatter the relay around the
   setpoint; trimming an hours target leaves the existing integrator in the
   loop, so the signal is smoothed by construction. §9 gap 4 is what that
   mistake looks like when it is made against the SOLAR temperature target.
2. A stale or missing reading is not a special case. `orp_trim` returns 0.0 and
   §5.3 runs exactly as it did in v0.6.0 — the same rule the PdC has had since
   v0.1.0: *a read gap is not evidence*.
3. A failed probe cannot run away. The bound is in hours, which is the unit the
   owner already caps and already understands.

ORP never gates the cell on or off, and it cannot: with no flow there is no
reading, and with no reading there would be no decision to run the pump that
would produce one. The hours law is the cold start, always.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..const import (
    MODE_WINTER,
    ORP_TRIM_FULL_SCALE_MV,
    WATER_FLUSH_S,
)
from .model import Memory, PoolState


@dataclass(frozen=True)
class WaterQuality:
    """This tick's chemistry, and whether the law is allowed to believe it.

    The values are carried even when `fresh` is False: the diagnostic surface
    should still show what the probe says, and only `orp_trim` is required to
    ignore it. Nothing else in the package reads these.
    """

    fresh: bool
    ph: float | None = None
    orp: float | None = None
    ec: float | None = None
    # Why the reading does not count, when it does not. Empty when fresh.
    stale_reason: str = ""
    # Seconds of confirmed marcia behind this reading; None when the pump is
    # not running at all.
    flushed_for_s: float | None = None


@dataclass(frozen=True)
class OrpTrim:
    """The chemistry correction to today's chlorine-hours target.

    `suspended` is the one case the owner has to act on: the water wants more
    chlorine and the supervisor is deliberately refusing to make it, because
    pH is the binding constraint and acid is the fix. It is carried as a flag
    rather than inferred from `reason` so the reason line and the tests can
    both ask the question directly.
    """

    hours: float = 0.0
    reason: str = "ORP control off"
    suspended: bool = False


def flushed_for_s(mem: Memory, now: datetime) -> float | None:
    """Seconds since the pump was last seen in marcia, or None if it is not.

    Measured against `pump_running_since`, which the 60 s pump confirmation
    already owns — so this window INCLUDES that 60 s rather than starting
    after it.
    """
    if mem.pump_running_since is None:
        return None
    return (now - mem.pump_running_since).total_seconds()


def read_quality(state: PoolState, mem: Memory) -> WaterQuality:
    """Is this tick's chemistry the pool's, or the skimmer pocket's?

    Measured 2026-09-21, and confirmed against the owner's own reference tool:
    with the pump off the skimmer read EC 7.01 mS/cm against 8.906 flushed —
    **21 % low** — while the reference put the pool at 8.61. A stagnant reading
    is not a noisy version of the right answer, it is a different body of
    water.

    The settling that day: pump on 20:41:16, in marcia 20:41:26, still stagnant
    at 20:41:39, ramping by 20:42:00, plateau 20:43:00 and flat for 14 min
    after. `WATER_FLUSH_S` is that with margin.
    """
    ph, orp, ec = state.water_ph, state.water_orp, state.water_ec
    elapsed = flushed_for_s(mem, state.mono)

    if elapsed is None:
        return WaterQuality(False, ph, orp, ec, "pump not in marcia")
    if elapsed < WATER_FLUSH_S:
        return WaterQuality(
            False, ph, orp, ec,
            f"skimmer flushing ({elapsed:.0f}/{WATER_FLUSH_S} s)",
            elapsed,
        )
    return WaterQuality(True, ph, orp, ec, "", elapsed)


def orp_trim(state: PoolState, quality: WaterQuality) -> OrpTrim:
    """Hours to add to (or take off) today's chlorine target.

    Proportional to the ORP deficit and **extension only** — clamped to
    [0, `orp_max_extra_hours`]. An ORP above target trims nothing.

    Partly asymmetry of cost (ORP wrongly low costs cell hours; ORP wrongly
    high costs a green pool), but mostly stability: a cut oscillates, because
    the trim's input depends on the thing it controls. Cutting the target can
    stop the cell, which stops the pump, which makes the reading stale, which
    removes the cut, which restarts everything — a ~4 min pump cycle. See
    `DEFAULT_ORP_MAX_EXTRA_HOURS` in `const.py` for the worked case. Extension
    has no such loop: raising the target keeps the pump running and the
    reading fresh, and a stale reading just falls back to the plain hours law,
    which settles to "off" and stays there.

    That is also the direction the pool actually needs. On 2026-09-21 the cell
    ran 6.69 h against a 6.0 h target — `chlorine_hours_missing` 0, the target
    "met" — and the owner's reference measured FAC 1.2 ppm, under the 1.5-2 of
    §9. The hours proxy said done; the water said short.

    **The pH ceiling gates the extension, and an unknown pH gates it too.**
    Chlorine's active form is HOCl and its share collapses as pH rises (~75 %
    at 7.0, ~50 % at 7.5, ~22 % at 8.0), while a salt cell *raises* pH as a
    byproduct. So above the ceiling the answer is acid, not cell hours: without
    this rung the loop would extend, see no improvement, extend again and hit
    its cap every day, winding up against a constraint it has no authority
    over. The supervisor cannot dose acid, so it names the problem instead.

    """
    cfg = state.config

    if not state.orp_control:
        return OrpTrim(0.0, "ORP control off")
    # Winter is a fixed maintenance dose in a closed pool, not a target to
    # chase — and nobody is swimming in it.
    if state.mode == MODE_WINTER:
        return OrpTrim(0.0, "winter: fixed maintenance dose")
    if not quality.fresh:
        return OrpTrim(0.0, f"ORP not counted — {quality.stale_reason}")
    if quality.orp is None:
        return OrpTrim(0.0, "ORP unreadable")

    deficit = cfg.orp_target - quality.orp
    raw = cfg.orp_max_extra_hours * deficit / ORP_TRIM_FULL_SCALE_MV

    if raw > 0:
        if quality.ph is None:
            return OrpTrim(
                0.0,
                f"ORP {quality.orp:.0f} mV low but pH unreadable — "
                "extension suspended",
                suspended=True,
            )
        if quality.ph > cfg.ph_ceiling:
            return OrpTrim(
                0.0,
                f"ORP {quality.orp:.0f} mV low but pH {quality.ph:.2f} over "
                f"{cfg.ph_ceiling:.1f} — extension suspended, add acid",
                suspended=True,
            )

    trim = round(max(0.0, min(cfg.orp_max_extra_hours, raw)), 2)

    if trim > 0:
        return OrpTrim(
            trim,
            f"ORP {quality.orp:.0f} mV, {deficit:.0f} under target "
            f"(+{trim:.1f} h)",
        )
    if deficit < 0:
        return OrpTrim(
            0.0, f"ORP {quality.orp:.0f} mV, {-deficit:.0f} over target"
        )
    return OrpTrim(0.0, f"ORP {quality.orp:.0f} mV at target")
