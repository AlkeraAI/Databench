"""The supervisor's dealings with its workers' systemd units, bounded.

Every ``systemctl`` call here returns within its timeout: a stop is asked for
without blocking (``--no-block``) and its end is polled for, because a unit
stops by draining its worker, which may take hours, and a supervisor blocked
on that beats for nobody. A command that does not answer in time is reported
(:data:`~alkera_core.compute.box_logs.UNIT_COMMAND_TIMED_OUT`) and treated as failed,
never raised.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

from alkera_core.compute.box_logs import UNIT_COMMAND_TIMED_OUT
from alkera_core.process import SpawnSpec
from alkera_core.process import run as run_child

from alkera_cli.supervisor.org_events import emit

#: How long one ``systemctl`` call may take.
COMMAND_TIMEOUT_SECONDS: Final = 30.0
#: The status :func:`run` answers for a command that timed out.
TIMED_OUT: Final = 124
#: How long a leftover unit is given to stop on its own before it is killed,
#: and how long a killed one is given to go.
UNIT_STOP_GRACE_SECONDS: Final = 30.0
UNIT_KILL_GRACE_SECONDS: Final = 10.0
#: The states of a unit that no longer holds its name.
GONE_STATES: Final = frozenset({"inactive", "failed"})
_UNIT: Final = re.compile(r"^alkera-org-(\d+)\.service$")

Runner = Callable[[Sequence[str]], int]


def run(argv: Sequence[str]) -> int:
    """``argv``'s exit status, or :data:`TIMED_OUT`."""
    try:
        return run_child(_captured(argv), timeout=COMMAND_TIMEOUT_SECONDS).returncode
    except subprocess.TimeoutExpired:
        emit(UNIT_COMMAND_TIMED_OUT, level="warning", command=" ".join(argv[:3]))
        return TIMED_OUT


def _captured(argv: Sequence[str]) -> SpawnSpec:
    return SpawnSpec(argv=list(argv), env=os.environ, stdout="pipe", stderr="pipe")


def active_org_slots() -> set[int]:
    """The slots whose worker unit is loaded and active now."""
    try:
        listing = ["systemctl", "list-units", "--plain", "--no-legend", "--state=active"]
        out = run_child(
            _captured([*listing, "alkera-org-*"]), timeout=COMMAND_TIMEOUT_SECONDS
        ).stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        emit(UNIT_COMMAND_TIMED_OUT, level="warning", command="systemctl list-units")
        return set()
    return parse_unit_slots(out)


def unit_state(unit: str) -> str:
    """The unit's ``ActiveState`` (``inactive`` for one that is gone), or
    ``unknown`` when systemd does not answer in time."""
    return show_property(unit, "ActiveState") or "unknown"


def show_property(unit: str, name: str) -> str:
    """One property of ``unit`` as ``systemctl show`` prints it, or ``""``
    when systemd does not answer in time."""
    try:
        shown = run_child(
            _captured(["systemctl", "show", f"--property={name}", "--value", unit]),
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        emit(UNIT_COMMAND_TIMED_OUT, level="warning", command="systemctl show")
        return ""
    return shown.stdout.decode("utf-8", errors="replace").strip()


def parse_unit_slots(listing: str) -> set[int]:
    """The slot numbers in a ``systemctl list-units --plain`` listing."""
    found: set[int] = set()
    for line in listing.splitlines():
        match = _UNIT.match(line.split(maxsplit=1)[0]) if line.strip() else None
        if match:
            found.add(int(match.group(1)))
    return found


async def clear_unit(
    unit: str,
    *,
    runner: Callable[[Sequence[str]], Awaitable[int]],
    state: Callable[[str], Awaitable[str]],
    sleep: Callable[[float], Awaitable[None]],
) -> bool:
    """Make sure ``unit`` is gone before its slot's next worker takes its
    name: asked to stop, killed if it is still there after a grace, and its
    failed state reset. Whether it is gone."""

    async def gone_within(seconds: float) -> bool:
        for _ in range(max(1, int(seconds))):
            # A unit still deactivating (its worker draining) holds the name.
            if await state(unit) in GONE_STATES:
                return True
            await sleep(1.0)
        return False

    await runner(("systemctl", "stop", "--no-block", unit))
    gone = await gone_within(UNIT_STOP_GRACE_SECONDS)
    if not gone:
        await runner(("systemctl", "kill", "--signal=KILL", unit))
        gone = await gone_within(UNIT_KILL_GRACE_SECONDS)
    await runner(("systemctl", "reset-failed", unit))
    return gone


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "GONE_STATES",
    "TIMED_OUT",
    "UNIT_KILL_GRACE_SECONDS",
    "UNIT_STOP_GRACE_SECONDS",
    "Runner",
    "active_org_slots",
    "clear_unit",
    "parse_unit_slots",
    "run",
    "show_property",
    "unit_state",
]
