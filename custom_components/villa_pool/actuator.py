"""Turning a `Decision` into service calls — the HA half of actuation.

The pure half is `supervisor/actuation.py`, which answers "should this lever be
commanded right now" for one lever at a time. This module owns the parts that
need Home Assistant: which entity each lever is, what service moves it, what
order the levers are touched in, and the logging and notification around a
write.

**Ordering inside one tick is a hardware rule, not a style choice** (STORY
§5.1). The pool is one hydraulic system, so commands that remove flow must
follow the things that need it, and commands that add load must follow the
flow:

    stop:   PdC off -> chlorine off -> pump off
    start:  pump mode/speed -> pump on -> (next tick, once confirmed) PdC, chlorine

The start side is spread across ticks by the law itself — `pdc_step` and
`chlorine_decision` both refuse until `pool_pompa_in_marcia` has been true for
60 s — so this module never has to ask the PdC and the pump to start in the
same breath. What it does have to guarantee is that within a single tick the
pump is never switched off before the things drawing through it.

**Dry run does not come here to be disabled.** When `switch.pool_dry_run` is on
the engine calls `async_apply(..., live=False)`, which compares intent against
the real devices and logs the difference without touching the lever memory —
so the log says what a live supervisor would have sent, and the 24 h dry run's
central claim ("`villa_pool` appears nowhere in the logbook") stays literally
true, notifications included.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CHLORINE_MIN_PUMP_SPEED,
    CONF_CHLORINATOR_SWITCH,
    CONF_NOTIFY_TARGET,
    CONF_PDC_CLIMATE,
    CONF_PDC_RUNNING,
    CONF_PUMP_MODE,
    CONF_PUMP_SPEED,
    CONF_PUMP_SWITCH,
    NOTIFIABLE_BLOCKS,
    PDC_GRID,
    PDC_SOLAR,
    PUMP_MODE_MANUAL,
    PUMP_SPEED_TOLERANCE,
    SETPOINT_TOLERANCE,
    SETTLE_LOCAL_S,
    SETTLE_PDC_MODE_S,
    SETTLE_PDC_SETPOINT_S,
    WRITE_ATTEMPTS,
    WRITE_TIMEOUT_S,
)
from .supervisor import BLIND, LATCHED, WRITE, Decision, Lever, plan

_LOGGER = logging.getLogger(__name__)

LEVER_PDC_MODE = "pdc_mode"
LEVER_PDC_SETPOINT = "pdc_setpoint"
LEVER_CHLORINE = "chlorine"
LEVER_PUMP_MODE = "pump_mode"
LEVER_PUMP_SPEED = "pump_speed"
LEVER_PUMP = "pump"

# v0.2.0 wired the pump and the chlorinator; v0.3.0 adds the PdC (STORY §8 steps
# 2 and 3). Kept as a flag rather than as absent code because the pure planner
# and the hydraulic ordering are shared between them.
PDC_ACTUATION_IMPLEMENTED = True

ON = "on"
OFF = "off"
HVAC_HEAT = "heat"
HVAC_OFF = "off"


@dataclass(frozen=True)
class Target:
    """One lever, resolved against this tick's decision and the live device."""

    key: str
    entity_id: str | None
    desired: object | None
    actual: object | None
    settle_s: int = SETTLE_LOCAL_S
    tolerance: float | None = None
    label: str = ""


class Actuator:
    """Owns the levers, their memories, and the writes."""

    def __init__(self, hass: HomeAssistant, entry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._levers: dict[str, Lever] = {}
        self._latched: set[str] = set()
        self._last_dry: tuple | None = None
        self._last_block: str | None = None
        self._last_antifreeze: bool | None = None
        self._deferred: set[str] = set()
        self.writes = 0
        self.last_write: str | None = None
        self.live_since: datetime | None = None

    # --- reads ---------------------------------------------------------------

    def _entity(self, conf_key: str) -> str | None:
        return self.coordinator.eid(conf_key)

    def _state(self, conf_key: str) -> str | None:
        """The device's own state, or None when it cannot be read.

        None is never coerced to a value: an unreadable lever is one the
        planner refuses to judge, exactly as the coordinator refuses to read an
        `unavailable` pump as stopped (STORY §6).
        """
        entity_id = self._entity(conf_key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        return state.state

    def _number(self, conf_key: str) -> float | None:
        raw = self._state(conf_key)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _attribute(self, conf_key: str, attribute: str) -> Any:
        entity_id = self._entity(conf_key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        return state.attributes.get(attribute)

    # --- the levers this tick ------------------------------------------------

    def _targets(self, decision: Decision) -> list[Target]:
        """Every lever we have an opinion about, in the order it may be moved.

        A lever we have no opinion about still appears, with `desired=None`, so
        its memory is cleared rather than left holding a stale intent.
        """
        pump_desired = ON if decision.pump_on else OFF
        # Speed and mode are only meaningful while we are asking for the pump.
        # Writing a percentage to a pump we want stopped is noise at best.
        speed_desired = decision.pump_speed if decision.pump_on else None
        mode_desired = PUMP_MODE_MANUAL if decision.pump_on else None
        chlorine_desired = ON if decision.chlorine_on else OFF
        chlorine_actual = self._state(CONF_CHLORINATOR_SWITCH)

        # Guard 1 (§6): never take the speed below the calibrated 80 % while the
        # cell is ACTUALLY enabled — the cell's flow-switch minimum is still
        # unknown. The ordering below already asks the chlorinator to stop
        # first, but an order is only a plan: if that command has not landed
        # yet, or the lever is latched, the relay is still closed and the speed
        # must wait. Deferring leaves the pump faster than asked, which is the
        # harmless direction.
        if (
            speed_desired is not None
            and speed_desired < CHLORINE_MIN_PUMP_SPEED
            and chlorine_actual == ON
        ):
            self._defer(
                "pump speed",
                f"{speed_desired}% is below the {CHLORINE_MIN_PUMP_SPEED}% "
                "chlorine minimum and the cell is still enabled",
            )
            speed_desired = None
        else:
            self._defer("pump speed", None)

        # Guard 2 (§5.1): never remove flow from a heat pump that is running.
        # In v0.2.0 the PdC is not ours to stop at all, so it may be running on
        # its own thermostat or on one of the owner's automations while our
        # pump window closes; from v0.3.0 the law stops it first, and this
        # still covers the minutes of cloud lag before it actually winds down.
        # Holding the pump on costs filtration hours. Getting it wrong costs a
        # compressor.
        if pump_desired == OFF and self._state(CONF_PDC_RUNNING) == ON:
            self._defer("pump", "the PdC still reads running — keeping flow")
            pump_desired = None
        else:
            self._defer("pump", None)

        stopping_pdc, starting_pdc = self._pdc_targets(decision)

        chlorine = Target(
            LEVER_CHLORINE,
            self._entity(CONF_CHLORINATOR_SWITCH),
            chlorine_desired,
            chlorine_actual,
            label="chlorinator",
        )
        pump_block = [
            Target(LEVER_PUMP_MODE, self._entity(CONF_PUMP_MODE), mode_desired,
                   self._state(CONF_PUMP_MODE), label="pump mode"),
            Target(LEVER_PUMP_SPEED, self._entity(CONF_PUMP_SPEED), speed_desired,
                   self._number(CONF_PUMP_SPEED),
                   tolerance=PUMP_SPEED_TOLERANCE, label="pump speed"),
            Target(LEVER_PUMP, self._entity(CONF_PUMP_SWITCH), pump_desired,
                   self._state(CONF_PUMP_SWITCH), label="pump"),
        ]

        # Everything that gives flow back comes before the pump; everything that
        # consumes it comes after.
        if decision.chlorine_on:
            return [*stopping_pdc, *pump_block, *starting_pdc, chlorine]
        return [*stopping_pdc, chlorine, *pump_block, *starting_pdc]

    def _pdc_targets(
        self, decision: Decision
    ) -> tuple[list[Target], list[Target]]:
        """The climate levers, split into (stop first, start last).

        Nothing at all in three cases: the release has not wired them, the
        cloud-polled entity is mid-gap (`pdc_write` false — a write into a
        polling gap is how a machine that never stopped gets started twice,
        §5.2/§7.9), or no climate entity is configured.

        HA writes only `hvac_mode` and the setpoint; the machine's own
        thermostat does the rest (§5.2). `blocked` and `off` both mean `off`
        here — the difference between them is a reason, not a command.
        """
        if not PDC_ACTUATION_IMPLEMENTED or not decision.pdc_write:
            return [], []
        entity_id = self._entity(CONF_PDC_CLIMATE)
        if not entity_id:
            return [], []
        heating = (
            decision.pdc_state in (PDC_SOLAR, PDC_GRID)
            and decision.pdc_setpoint is not None
        )
        mode = Target(
            LEVER_PDC_MODE, entity_id,
            HVAC_HEAT if heating else HVAC_OFF,
            self._state(CONF_PDC_CLIMATE),
            settle_s=SETTLE_PDC_MODE_S, label="PdC mode",
        )
        # The setpoint target is built even when we are not heating, with no
        # opinion, so its lever is CLEARED rather than left holding the last
        # run's value and attempt count. A lever carrying `writes=2` from
        # yesterday would latch itself on the first disagreement tonight.
        setpoint = Target(
            LEVER_PDC_SETPOINT, entity_id,
            decision.pdc_setpoint if heating else None,
            self._attribute(CONF_PDC_CLIMATE, "temperature"),
            settle_s=SETTLE_PDC_SETPOINT_S, tolerance=SETPOINT_TOLERANCE,
            label="PdC setpoint",
        )
        if heating:
            # Setpoint before `heat`, so the machine never starts against the
            # target of whatever ran last. If the controller refuses a target
            # while it is off, the setpoint lever corrects itself 10 min later
            # rather than riding the mode lever's full compressor grace.
            return [], [setpoint, mode]
        return [mode, setpoint], []

    # --- the tick ------------------------------------------------------------

    async def async_apply(
        self, decision: Decision, mono: datetime, *, live: bool
    ) -> None:
        """Move the pool towards `decision`, or say what that would take."""
        if not decision.actuate:
            # Rung 1 of the ladder: hands off entirely. Not "write everything
            # off" — see `Decision.actuate`.
            self._levers.clear()
            self._latched.clear()
            return

        targets = self._targets(decision)
        if not live:
            self._log_dry(targets, decision)
            return

        await self._notify_block(decision)
        await self._notify_antifreeze(decision)
        for target in targets:
            await self._apply_one(target, decision, mono)

    async def _apply_one(
        self, target: Target, decision: Decision, mono: datetime
    ) -> None:
        # A picker the owner cleared is a lever we do not have. Skipping before
        # the planner runs keeps its memory empty, so it cannot accumulate
        # attempts and then announce that it has given up on an entity that
        # never existed.
        if not target.entity_id:
            self._levers.pop(target.key, None)
            return
        lever = self._levers.get(target.key, Lever())
        verdict, lever = plan(
            lever,
            desired=target.desired,
            actual=target.actual,
            mono=mono,
            settle_s=target.settle_s,
            attempts=WRITE_ATTEMPTS,
            tolerance=target.tolerance,
        )
        self._levers[target.key] = lever

        if verdict.action == LATCHED and target.key not in self._latched:
            self._latched.add(target.key)
            _LOGGER.warning(
                "Not driving %s (%s) any more: %s. The supervisor keeps "
                "deciding and reporting; it will command this lever again as "
                "soon as its intent changes or the device agrees.",
                target.label, target.entity_id, verdict.reason,
            )
            await self._send_notification(
                "Pool: supervisor stopped driving the "
                f"{target.label}",
                f"{verdict.reason}. Supervisor reason: {decision.reason}",
                tag=f"pool_latched_{target.key}",
            )
        elif verdict.action != LATCHED:
            self._latched.discard(target.key)

        if verdict.action == BLIND:
            _LOGGER.debug(
                "%s (%s) is unreadable — no judgement, no write.",
                target.label, target.entity_id,
            )
        if verdict.action != WRITE:
            return

        domain, service, data = _service_for(
            target.key, target.entity_id, verdict.value
        )
        # Every write, with its reason (STORY §6). Writes are rare by design, so
        # this is a line per real command rather than a line per tick.
        _LOGGER.info(
            "WRITE %s.%s %s — %s (%s) — %s",
            domain, service, data, target.label, verdict.reason, decision.reason,
        )
        self.writes += 1
        self.last_write = f"{domain}.{service} {target.label}={verdict.value!r}"
        try:
            # Bounded: a cloud-backed lever that never answers must not hold the
            # engine's lock, or one hung call makes the supervisor deaf for
            # every tick after it.
            async with asyncio.timeout(WRITE_TIMEOUT_S):
                await self.hass.services.async_call(
                    domain, service, data, blocking=True
                )
        except Exception:  # noqa: BLE001 - one bad lever must not stop the tick
            # The write is still recorded above, so the settle window applies
            # and a device that keeps refusing ends up latched rather than
            # commanded every 60 s forever.
            _LOGGER.exception(
                "Failed to call %s.%s for %s", domain, service, target.entity_id
            )

    # --- dry run -------------------------------------------------------------

    def _log_dry(self, targets: list[Target], decision: Decision) -> None:
        """Say what a live supervisor would have sent — on change only.

        The lever memories are deliberately NOT advanced here. In a dry run no
        device ever adopts a command, so running the state machine would walk
        every lever into its latch and then report "manual override" about a
        pool nobody has touched.
        """
        diffs = tuple(
            (t.label, t.desired, t.actual)
            for t in targets
            if t.desired is not None and t.actual is not None
            and not _agrees(t)
        )
        if diffs == self._last_dry:
            return
        self._last_dry = diffs
        if not diffs:
            _LOGGER.info("DRY-RUN nothing to write — the pool already matches intent.")
            return
        for label, desired, actual in diffs:
            _LOGGER.info(
                "DRY-RUN would set %s: %r -> %r — %s",
                label, actual, desired, decision.reason,
            )

    # --- notifications -------------------------------------------------------

    async def _notify_block(self, decision: Decision) -> None:
        """Wake the owner when hardware says something is wrong (STORY §7.5).

        Only on the edge, and only for the two blocks that are the equipment
        complaining. The owner's own `automation.pool_allerta_*` watchdogs stay
        in place and may say the same thing from the other side; that
        duplication is deliberate — they are independent of this integration.
        """
        block = decision.blocked_reason
        if block == self._last_block:
            return
        self._last_block = block
        if block not in NOTIFIABLE_BLOCKS:
            return
        await self._send_notification(
            "Pool: supervisor blocked",
            f"{block}. {decision.reason}",
            tag=f"pool_blocked_{block.replace(' ', '_')}",
        )

    async def _notify_antifreeze(self, decision: Decision) -> None:
        """Both edges of the freeze latch (owner decision, 2026-09-17).

        Rare, actionable and worth knowing about at 03:00: the pool has started
        protecting itself, and the owner may want to check the cover, the
        skimmer or the pipework while it does. The release is sent too, so the
        event has a visible end rather than trailing off.
        """
        active = bool(decision.detail.get("antifreeze"))
        if active == self._last_antifreeze:
            return
        first_look, self._last_antifreeze = self._last_antifreeze is None, active
        if first_look and not active:
            # Starting up on a mild day is not an event.
            return
        if active:
            await self._send_notification(
                "Pool: antifreeze engaged",
                f"Freeze protection is running. {decision.reason}",
                tag="pool_antifreeze",
            )
        else:
            await self._send_notification(
                "Pool: antifreeze released",
                f"The air is back above the release threshold. {decision.reason}",
                tag="pool_antifreeze",
            )

    async def _send_notification(self, title: str, message: str, *, tag: str) -> None:
        merged = {**self.entry.data, **self.entry.options}
        target = merged.get(CONF_NOTIFY_TARGET)
        if not target or "." not in target:
            return
        domain, _, service = target.partition(".")
        if not self.hass.services.has_service(domain, service):
            # A renamed or removed notifier is worth one line, not a traceback
            # on every block for the rest of the day.
            self._defer(
                "notifications", f"{target} is not a service on this system"
            )
            return
        try:
            await self.hass.services.async_call(
                domain, service,
                {"title": title, "message": message, "data": {"tag": tag}},
                blocking=False,
            )
        except Exception:  # noqa: BLE001 - a failed push must not stop the tick
            _LOGGER.exception("Failed to notify via %s", target)

    def _defer(self, label: str, why: str | None) -> None:
        """Hold a lever back, saying so once — not every 60 s.

        `why=None` means the hold is over, and is silent: the interesting event
        is the pool being held, not the ordinary state of not being held.
        """
        if why is None:
            self._deferred.discard(label)
            return
        if label in self._deferred:
            return
        self._deferred.add(label)
        _LOGGER.info("Holding off on the %s: %s.", label, why)

    def reset(self) -> None:
        """Forget every lever.

        Called when the dry-run switch moves. While the supervisor was not
        writing, the pool may have been moved by anyone; carrying a latch or a
        remembered command across that boundary would let an argument from
        before the pause silence a lever that now needs driving.
        """
        self._levers.clear()
        self._latched.clear()
        self._deferred.clear()
        self._last_dry = None

    # --- reporting -----------------------------------------------------------

    @property
    def latched(self) -> list[str]:
        return sorted(self._latched)

    def diagnostics(self) -> dict:
        return {
            "writes": self.writes,
            "last_write": self.last_write,
            "latched": self.latched,
            # Levers the supervisor wants to move but is holding back for a
            # hydraulic reason. `pump` sitting here is how a stuck
            # `pool_pdc_acceso` would show itself: the guard fails towards
            # keeping flow and never times out, so it has to be VISIBLE rather
            # than merely safe.
            "holding": sorted(self._deferred),
        }


def _agrees(target: Target) -> bool:
    if target.tolerance is not None:
        try:
            return abs(float(target.actual) - float(target.desired)) <= target.tolerance
        except (TypeError, ValueError):
            return False
    return target.actual == target.desired


def _service_for(key: str, entity_id: str, value: object) -> tuple[str, str, dict]:
    """The one service call that moves this lever to `value`."""
    if key in (LEVER_PUMP, LEVER_CHLORINE):
        return (
            "switch",
            "turn_on" if value == ON else "turn_off",
            {"entity_id": entity_id},
        )
    if key == LEVER_PUMP_SPEED:
        return "number", "set_value", {"entity_id": entity_id, "value": float(value)}
    if key == LEVER_PUMP_MODE:
        return "select", "select_option", {"entity_id": entity_id, "option": value}
    if key == LEVER_PDC_MODE:
        return (
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": value},
        )
    if key == LEVER_PDC_SETPOINT:
        return (
            "climate",
            "set_temperature",
            {"entity_id": entity_id, "temperature": float(value)},
        )
    raise ValueError(f"no service mapped for lever {key}")
