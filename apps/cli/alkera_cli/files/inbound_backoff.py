"""How long a node whose inbound download was refused waits to be asked again.

A link the drive keeps but the box will not materialize, or bytes another
holder has not sent yet, answer the download with the same refusal every time.
A holder that asked again on every inbound pass asked twice a second with a
warning each time, for as long as it held the folder. Each refusal now doubles
the wait, up to a ceiling; a newer entry for the node (the drive changed it
again) is asked for at once, and a download that lands forgets the streak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from alkera_cli.host.backoff import doubled

__all__ = ["INBOUND_RETRY_FIRST", "INBOUND_RETRY_MAX", "InboundBackoff"]

#: The first wait after a refusal, and the most any wait grows to.
INBOUND_RETRY_FIRST: Final = 2.0
INBOUND_RETRY_MAX: Final = 600.0


@dataclass(frozen=True, slots=True)
class _Streak:
    seq: int
    wait: float
    until: float


@dataclass
class InboundBackoff:
    """The refusal streak of every node a holder is owed, by node id."""

    _streaks: dict[str, _Streak] = field(default_factory=dict)

    def _current(self, node_id: str, seq: int) -> _Streak | None:
        streak = self._streaks.get(node_id)
        return streak if streak is not None and streak.seq == seq else None

    def waiting(self, node_id: str, seq: int, now: float) -> bool:
        """Whether entry ``seq`` of ``node_id`` is still inside its wait."""
        streak = self._current(node_id, seq)
        return streak is not None and now < streak.until

    def refused(self, node_id: str, seq: int, now: float) -> bool:
        """Note a refusal; True when it starts a streak (the one worth saying)."""
        streak = self._current(node_id, seq)
        previous = 0.0 if streak is None else streak.wait
        wait = doubled(previous, floor=INBOUND_RETRY_FIRST, cap=INBOUND_RETRY_MAX)
        self._streaks[node_id] = _Streak(seq=seq, wait=wait, until=now + wait)
        return streak is None

    def landed(self, node_id: str) -> None:
        self._streaks.pop(node_id, None)
