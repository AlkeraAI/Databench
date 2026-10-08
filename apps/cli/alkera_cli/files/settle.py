"""When a file a holder saw change has stopped changing long enough to read.

Reading a file while something still writes it proves only that the read
raced the writer, so the live plane waits for a file to go unchanged for a
while (the served ``settle_ms``) before it hashes and uploads it. Two things
bound that wait:

* A file a co-edited document is open on settles on :data:`LIVE_SETTLE_MS`.
  People are watching that document for the agent's edit, and an agent's
  edit tool writes such a file whole; the stamp check around the hash still
  refuses bytes that move under the read.
* No file waits longer than :data:`SETTLE_MAX_WAIT` seconds. A writer that
  touches a file more often than the settle window (a log, an agent saving
  every second) would otherwise starve it: nothing would leave the box until
  the writer stopped.

A file held back is due again at a moment this watch can name (:meth:`SettleWatch.due_in`),
so the live plane looks at it then rather than on its next round, which on a
quiet folder is up to a whole batch window later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

#: How long a file a live document is open on must go unchanged (ms).
LIVE_SETTLE_MS: Final = 200

#: The longest any file waits to settle (seconds) before it is read anyway.
SETTLE_MAX_WAIT: Final = 5.0

Stamp = tuple[int, int]


@dataclass
class SettleWatch:
    """Per path: the stamp last seen, since when, and since when the path has
    been waiting at all. ``wall_ns`` is the wall clock the disk's mtimes are
    on; ``monotonic`` the holder's own, for when the two disagree."""

    wall_ns: Callable[[], int]
    monotonic: Callable[[], float]
    _seen: dict[str, tuple[Stamp, float]] = field(default_factory=dict)
    _waiting_since: dict[str, float] = field(default_factory=dict)
    _due: dict[str, float] = field(default_factory=dict)
    """Per path held back: the monotonic time it next answers settled."""

    def settled(self, relative: str, stamp: Stamp | None, *, settle_ms: int) -> bool:
        """Whether ``relative`` (now stamped ``stamp``, ``None`` when gone)
        may be read. Its mtime says so when the wall clock agrees with the
        disk's; otherwise the same stamp seen for the settle time on the
        monotonic clock does."""
        if settle_ms <= 0 or stamp is None:
            self.forget(relative)
            return True
        if self.wall_ns() - stamp[1] >= settle_ms * 1_000_000:
            self.forget(relative)
            return True
        now = self.monotonic()
        since = self._waiting_since.setdefault(relative, now)
        if now - since >= SETTLE_MAX_WAIT:
            self.forget(relative)
            return True
        seen = self._seen.get(relative)
        if seen is None or seen[0] != stamp:
            seen = self._seen[relative] = (stamp, now)
        elif (now - seen[1]) * 1000.0 >= settle_ms:
            self.forget(relative)
            return True
        return self._held(relative, stamp, seen[1], since, settle_ms)

    def _held(
        self, relative: str, stamp: Stamp, seen_at: float, since: float, settle_ms: int
    ) -> bool:
        """Note when ``relative`` next answers settled, and answer ``False``:
        the first of the same stamp seen for the settle time, the longest wait
        running out, and (when the wall clock agrees with the disk's) the
        stamp's own age reaching the settle time."""
        window = settle_ms / 1000.0
        due = min(seen_at + window, since + SETTLE_MAX_WAIT)
        age = (self.wall_ns() - stamp[1]) / 1e9
        if age >= 0:
            due = min(due, self.monotonic() + window - age)
        self._due[relative] = due
        return False

    def due_in(self) -> float | None:
        """Seconds until the next file held back is due, ``None`` when none
        is. A due time already passed is not counted: it waits for
        :meth:`take_due`, so a round held off (a retry's wait) is not a spin."""
        now = self.monotonic()
        ahead = [due - now for due in list(self._due.values()) if due > now]
        return min(ahead) if ahead else None

    def take_due(self) -> bool:
        """Whether a file held back has reached its due time. Those are let
        go here; the next look at each decides again (and notes a new time)."""
        now = self.monotonic()
        passed = [relative for relative, due in list(self._due.items()) if due <= now]
        for relative in passed:
            self._due.pop(relative, None)
        return bool(passed)

    def forget(self, relative: str) -> None:
        self._seen.pop(relative, None)
        self._waiting_since.pop(relative, None)
        self._due.pop(relative, None)


__all__ = ["LIVE_SETTLE_MS", "SETTLE_MAX_WAIT", "SettleWatch"]
