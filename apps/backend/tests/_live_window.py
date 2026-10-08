"""The allowance a real-time deadline in this suite is asserted with.

Most of what this suite claims can be pinned on a clock the test owns — a
budget fires because the test moved the clock, not because the machine was
slow. Some of it cannot: a frame has to travel a real socket into a real
served app before the box can act on it, and no injected clock makes that
arrive. A deadline on THAT measures the host's load as much as it measures the
plane, and the claim starts failing on a busy runner with nothing about the
product changed.

Rather than inventing a second knob, this reuses the ruling the Files perf
budgets already made: a published number is asserted times the run's
allowance — three times the number where the run is a shared gate, the number
itself where it is the quiet nightly one. The scale is ``FILES_PERF_SIZE``,
which ``apps/backend/tests/files/perf/_fixtures.py`` reads, the pr-gate's
Linux ``test`` job sets to ``smoke``, and the nightly Files workflow sets to
``nightly``.

``smoke`` allows three here rather than the fifty its perf row allows, because
fifty is about the perf FIXTURE — a latency measured over twenty thousand
nodes is not comparable to a budget published at a hundred thousand. A
liveness window has no fixture behind it. What ``smoke`` tells this suite is
only WHERE the run is happening, and there it says exactly what ``pr`` says: a
shared, noisy machine. The Windows shards set no scale at all and get the same
allowance through the default, which is what they want — thirty-two workers on
one NTFS box.

The claim itself never moves. The allowance only says how much of the host's
load an assertion absorbs before it calls the claim broken.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: The scale knob, shared with the Files perf budgets rather than duplicated.
SCALE_ENV = "FILES_PERF_SIZE"

#: What a run at this scale is allowed, and what to call it in a failure.
ALLOWANCES: dict[str, tuple[float, str]] = {
    "smoke": (3.0, "PR size"),
    "pr": (3.0, "PR size"),
    "nightly": (1.0, "nightly"),
}

#: The scale ``perf_sizes`` assumes when the knob is unset — a developer's
#: laptop and the Windows shards land here, and both are shared machines.
DEFAULT_SCALE = "pr"


@dataclass(frozen=True, slots=True)
class Window:
    """A window the claim states, and the deadline this run asserts it with."""

    #: The window the claim is about, before any allowance.
    spec: float
    #: What this run multiplies it by before asserting it.
    allowance: float
    #: What that allowance is called where a person reads the failure.
    label: str

    @property
    def seconds(self) -> float:
        """The deadline actually waited on."""
        return self.spec * self.allowance

    def __str__(self) -> str:
        """The deadline, the window it came from, and the allowance between.

        A failure naming only "15.0s" would send the next reader looking for a
        fifteen-second claim that exists nowhere.
        """
        return f"{self.seconds:.1f}s ({self.spec:.1f}s x {self.allowance:g}, {self.label})"


def live_window(seconds: float) -> Window:
    """``seconds`` as this run asserts it.

    An unknown scale raises rather than falling back to the loose allowance: a
    nightly run that silently asserted three times its window would report a
    green that proves nothing — the same reason ``perf_sizes`` refuses one.
    """
    scale = os.environ.get(SCALE_ENV, DEFAULT_SCALE)
    if scale not in ALLOWANCES:
        raise ValueError(f"{SCALE_ENV} must be one of {sorted(ALLOWANCES)}, got {scale!r}")
    allowance, label = ALLOWANCES[scale]
    return Window(spec=float(seconds), allowance=allowance, label=label)


__all__ = ["ALLOWANCES", "DEFAULT_SCALE", "SCALE_ENV", "Window", "live_window"]
