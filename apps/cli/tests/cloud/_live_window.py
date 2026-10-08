"""The allowance a real-time liveness deadline in this suite is asserted with.

The live claims here are real-time claims: a file written on the box reaches
the member inside the window a person would call live. On a shared runner —
sixteen xdist workers over one Postgres and one disk — that window measures the
machine's load as much as it measures the plane, and the claim starts failing
on a slow evening's runner with nothing about the plane changed.

The Files performance budgets already ruled on exactly this, and this module reuses
their ruling rather than inventing a second knob: a published number is
asserted times the run's allowance — three times the number where the run is a
shared gate, the number itself where it is the quiet nightly one. The scale is
``FILES_PERF_SIZE``, read by ``apps/backend/tests/files/perf/_fixtures.py``,
which the pr-gate sets to ``smoke`` and the nightly Files workflow to
``nightly``.

``smoke`` allows three times here rather than the fifty its perf row allows,
because fifty is about the perf FIXTURE: a latency measured over twenty
thousand nodes is not comparable to a budget published at a hundred thousand,
so the wall-clock half of those rows deliberately asserts nothing. A liveness
window has no fixture behind it. What ``smoke`` tells this suite is only where
the run is happening, and there it says exactly what ``pr`` says — a shared,
noisy machine.

The claim itself never moves: five seconds is still what separates "live" from
"saved at a checkpoint when the turn ends". The allowance only says how much of
the host's load an assertion absorbs before it calls that distinction broken.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: The scale knob, shared with the Files performance budgets rather than duplicated.
SCALE_ENV = "FILES_PERF_SIZE"

#: What a run at this scale is allowed, and what to call it in a failure. ``pr``
#: is the scale the perf fixtures fall back to when nothing sets the knob, and
#: its published multiplier is the three a shared machine wants — so an
#: environment that sets nothing (a developer's laptop, and the Windows shard,
#: which sets no scale at all) gets the shared-machine allowance too.
ALLOWANCES: dict[str, tuple[float, str]] = {
    "smoke": (3.0, "PR size"),
    "pr": (3.0, "PR size"),
    "nightly": (1.0, "nightly"),
}

#: The scale ``_fixtures.perf_sizes`` assumes when the knob is unset.
DEFAULT_SCALE = "pr"


@dataclass(frozen=True, slots=True)
class Window:
    """A window the spec states, and the deadline this run asserts it with."""

    #: The window the claim is about, before any allowance.
    spec: float
    #: What the run multiplies it by before asserting it.
    allowance: float
    #: What that allowance is called where a person reads the failure.
    label: str

    @property
    def seconds(self) -> float:
        """The deadline actually waited on."""
        return self.spec * self.allowance

    def __str__(self) -> str:
        """The deadline, the window it came from and the allowance between them.

        A failure that named only "15.0s" would send the next reader looking for
        a fifteen-second claim that does not exist anywhere.
        """
        return f"{self.seconds:.1f}s ({self.spec:.1f}s x {self.allowance:g}, {self.label})"


def live_window(seconds: float) -> Window:
    """``seconds`` as this run asserts it.

    An unknown scale fails rather than falling back to the loose allowance: a
    nightly run that silently asserted three times its window would report a
    green that proves nothing — the same reason ``perf_sizes`` refuses one.
    """
    scale = os.environ.get(SCALE_ENV, DEFAULT_SCALE)
    if scale not in ALLOWANCES:
        raise ValueError(f"{SCALE_ENV} must be one of {sorted(ALLOWANCES)}, got {scale!r}")
    allowance, label = ALLOWANCES[scale]
    return Window(spec=float(seconds), allowance=allowance, label=label)


__all__ = ["ALLOWANCES", "DEFAULT_SCALE", "SCALE_ENV", "Window", "live_window"]
