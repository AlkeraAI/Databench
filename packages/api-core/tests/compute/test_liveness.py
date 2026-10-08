"""The heartbeat contract: how long a box may go quiet before it is named.

The two halves are written apart — the daemon stamps on an interval, the cloud
judges against a window — and a deployment may move either. These pin what the
shipped pair means: a full minute of silence, several beats wide, and never
anything the turn state is allowed to read.
"""

from __future__ import annotations

from alkera_core.compute.liveness import (
    HEARTBEAT_INTERVAL_SECONDS,
    MISSED_HEARTBEATS_BEFORE_UNREACHABLE,
    READY_WINDOW_SECONDS,
)
from alkera_core.config import Settings


def test_a_box_is_named_unreachable_after_a_minute_of_silence() -> None:
    assert READY_WINDOW_SECONDS == 60


def test_the_window_is_several_beats_wide() -> None:
    """One lost beat — a restarted tunnel, a slow request — must never flicker
    the banner on a healthy box, and the window is derived so the two halves
    cannot drift apart."""
    assert READY_WINDOW_SECONDS == (
        HEARTBEAT_INTERVAL_SECONDS * MISSED_HEARTBEATS_BEFORE_UNREACHABLE
    )
    assert MISSED_HEARTBEATS_BEFORE_UNREACHABLE >= 2


def test_the_shipped_setting_is_that_window() -> None:
    """A deployment may override the pair together; with nothing set, the
    setting the cloud actually reads is the contract above."""
    assert Settings().compute_heartbeat_ready_seconds == READY_WINDOW_SECONDS
