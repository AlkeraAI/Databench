"""The heartbeat contract between a workspace machine's daemon and the cloud.

Two numbers, in one place, because they are the two halves of one agreement:
the daemon stamps its liveness every :data:`HEARTBEAT_INTERVAL_SECONDS`, and the
cloud judges the machine unreachable once
:data:`MISSED_HEARTBEATS_BEFORE_UNREACHABLE` of those stamps have failed to
arrive. Spelled apart, the two halves drift — a daemon beating every 20 s
against a window written for three 15 s beats is a healthy box declared dead
after two missed beats — and the drift is invisible from either side: the box is
fine, the banner says it is not.

This module imports only the standard library, so both halves can read it: the settings defaults
(``compute_heartbeat_interval_seconds`` / ``compute_heartbeat_ready_seconds``,
which a deployment may still override together) and the daemon's own default
interval.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

HEARTBEAT_INTERVAL_SECONDS = 15
"""How often the daemon on a workspace machine stamps its liveness.

Fifteen seconds: frequent enough that the window below is several beats wide,
cheap enough that every box in a fleet can keep it up.
"""

MISSED_HEARTBEATS_BEFORE_UNREACHABLE = 4
"""How many stamps may go missing before the machine is judged unreachable.

Four, a full minute of silence. The banner is not a verdict on the turn — a
turn may run for hours — so the cost of waiting longer is only that a reader
whose box really has died learns it a little later, while the cost of calling
it early is a banner on a healthy box that was busy inside one long step,
through a deploy, or behind a load-balancer blip. A minute clears all three.
"""

READY_WINDOW_SECONDS = HEARTBEAT_INTERVAL_SECONDS * MISSED_HEARTBEATS_BEFORE_UNREACHABLE
"""How recently a machine must have been heard from to count as ``ready``."""

DRAIN_CEILING_SECONDS = 6 * 60 * 60
"""How long a box told to stop waits for the turns it still holds.

The other agreement between the two halves, and here for the same reason: the
box waits by this number and an operator plans a deploy by it, so the two must
be one number. It is NOT a limit on how long an answer may take — a turn may
legitimately run for hours or days, and nothing here shortens one. It exists so
a deploy can be finished: without a ceiling a single wedged chat holds the box
for ever and the operator's only remaining move is the kill that strands every
other chat on it. Six hours is past any ordinary turn, so the ordinary deploy
costs nobody their answer; what is still running at the ceiling is handed back
the way an idle chat is — folder pushed, lease released — and resumes on the
next box rather than being lost.
"""

ENV_DRAIN_CEILING_SECONDS = "ALKERA_CLOUD_DRAIN_CEILING_SECONDS"
"""Where a node's environment states its drain ceiling: the daemon waits by it,
and the node's service unit sizes its stop timeout from the same value."""

CHAT_IDLE_MINUTES = 24 * 60
"""How long a chat with nothing running and nobody using it stays awake on its
box before the box puts it to sleep.

The box keeps a chat awake until it needs the room (a new chat with every slot
taken, or memory under pressure, sleeps the least recently used idle chat
first); this is the backstop under that. Twenty-four hours, so a chat left at
the end of a working day is still warm the next morning. Nothing meters or
bills a chat's awake time, so the window costs a pool box its memory and
nothing else. Here because two halves agree on
it: the platform serves it to its boxes, and a box started without it (a
developer's own machine) falls back to the same number."""

CHAT_IDLE_PRODUCTION_FLOOR_MINUTES = 60
"""The shortest idle window a production deployment may serve its boxes. A
window of minutes is a test setting: shipped to production it would put every
reader's chat to sleep between two questions, and each wake is a cold start."""

ENV_CHAT_IDLE_MINUTES = "ALKERA_CLOUD_CHAT_IDLE_MINUTES"
"""Where a node's environment states the idle window. A name of its own rather
than the older ``ALKERA_CLOUD_MIRROR_IDLE_MINUTES`` on purpose: a box still on a
build from before the sleep policy ignores it and keeps its own short window,
which is the only window that is safe on a box with no memory pressure valve.
A current box reads this first and the older name after it."""

CHAT_MEMORY_PRESSURE_PERCENT = 85
"""How full the chats' memory may get, as a percentage of its limit, before the
box sleeps its least recently used idle chats to give memory back. Read on the
working set (resident memory less reclaimable file cache)."""

ENV_CHAT_MEMORY_PRESSURE_PERCENT = "ALKERA_CLOUD_MEMORY_PRESSURE_PERCENT"
"""Where a node's environment states the memory pressure line."""

STOP_EXIT_SECONDS = 120
"""How long past its drain ceiling a stopping daemon may take to END its process.

The stop after the ceiling puts every chat it still holds to sleep, each on a
budget of its own and all at once, then closes its socket — about a minute at
worst. What is left after that is not work but a process that cannot exit: a
worker thread parked on a drive that never answers, which the interpreter waits
for at exit. Past this margin the daemon dumps every thread's stack to its log
and ends itself, so its supervisor starts the next one and the chats it could
not hand back are re-placed rather than served by nobody.
"""

UNIT_STOP_SLACK_SECONDS = 60
"""How much longer than the daemon's own hard stop the node's service manager
waits before it kills the process group. The daemon ends itself first, with the
thread dump that is the one clue a hang leaves; the kill is only the backstop
for a daemon whose timer never ran."""


RESTART_DRAIN_CEILING_SECONDS = 30
"""How long a supervised restart in place lets in-flight work run before the
process exits. Nothing is handed back, so this bounds only the gap the restart
costs; a turn still running when it ends is restarted by the next process."""


def drain_ceiling_seconds(env: Mapping[str, str]) -> float:
    """The drain ceiling ``env`` names (:data:`ENV_DRAIN_CEILING_SECONDS`), else
    :data:`DRAIN_CEILING_SECONDS`.

    ``0`` is meaningful and is kept: an operator who wants a box to hand its
    chats back at once (a deploy that cannot wait) says so with a zero.
    Anything that is not a finite number at or above zero reads as the
    default, so a typo in a deploy's environment cannot turn a drain into a
    cut."""
    raw = env.get(ENV_DRAIN_CEILING_SECONDS)
    if raw is None or not raw.strip():
        return float(DRAIN_CEILING_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DRAIN_CEILING_SECONDS)
    if not math.isfinite(value) or value < 0:
        return float(DRAIN_CEILING_SECONDS)
    return value


def unit_stop_timeout_seconds(drain_ceiling_seconds: int) -> int:
    """The node's service-unit stop timeout for a daemon draining by
    ``drain_ceiling_seconds``: finite, and past the daemon's own hard stop."""
    return int(drain_ceiling_seconds) + STOP_EXIT_SECONDS + UNIT_STOP_SLACK_SECONDS


__all__ = [
    "CHAT_IDLE_MINUTES",
    "CHAT_IDLE_PRODUCTION_FLOOR_MINUTES",
    "CHAT_MEMORY_PRESSURE_PERCENT",
    "DRAIN_CEILING_SECONDS",
    "ENV_CHAT_IDLE_MINUTES",
    "ENV_CHAT_MEMORY_PRESSURE_PERCENT",
    "ENV_DRAIN_CEILING_SECONDS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "MISSED_HEARTBEATS_BEFORE_UNREACHABLE",
    "READY_WINDOW_SECONDS",
    "RESTART_DRAIN_CEILING_SECONDS",
    "STOP_EXIT_SECONDS",
    "UNIT_STOP_SLACK_SECONDS",
    "drain_ceiling_seconds",
    "unit_stop_timeout_seconds",
]
