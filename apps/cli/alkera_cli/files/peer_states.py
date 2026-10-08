"""What a box's text peer last wrote to each file, kept across restarts.

A restarted box's file holds a state its peer wrote (or one just before it)
plus whatever the agent changed since. Not knowing which, its first send
named no state and was merged keeping everything: a line a person had typed
into came back beside its older copy from the file, and an answer the agent
saved over meanwhile left the peer standing on nothing. Kept per file, the
last few states the peer wrote let a restarted peer name the one its file
was made on, and its first send merges exactly.

One small file per held folder, beside its journal (``<journal>-peer``) and
removed with it when the lease goes back. A file's entry goes when its
live session closes or it stops being text, and the file keeps at most :data:`MAX_NODES` entries and
:data:`MAX_FILE_BYTES` of text, the least recently written going first: it
is rewritten whole on every state, so it must stay small. Best effort: a
state that cannot be written or read back is one the restarted peer does not
name, and its send goes as before.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = ["MAX_FILE_BYTES", "MAX_NODES", "STATES_KEPT", "PeerStates"]

logger = logging.getLogger(__name__)

#: How many of a file's latest states are kept: the one written last and the
#: ones before it that an agent's read may still have been made on.
STATES_KEPT: Final = 3
#: The most text one file's kept states may hold; past it none are kept.
MAX_KEPT_BYTES: Final = 4 * 1024 * 1024
#: The most files kept at once: the peer holds the files people have open,
#: a handful at a time, and lets each go when nobody touched it for minutes.
MAX_NODES: Final = 32
#: The most text the whole file may hold.
MAX_FILE_BYTES: Final = 16 * 1024 * 1024


@dataclass
class PeerStates:
    """One held folder's kept states, by node, in the file at ``path``
    (``None`` keeps nothing)."""

    path: Path | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def beside(cls, journal: Any) -> PeerStates:
        """The states kept beside ``journal`` (a live sync's, or ``None``)."""
        return cls(None if journal is None else Path(f"{journal.path}-peer"))

    def load(self, node_id: str) -> list[tuple[str, str]]:
        """The states kept for ``node_id``, oldest first."""
        with self._lock:
            found = self._read().get(node_id)
        if not isinstance(found, list):
            return []
        return [
            (state[0], state[1])
            for state in found
            if isinstance(state, list)
            and len(state) == 2
            and isinstance(state[0], str)
            and isinstance(state[1], str)
        ][-STATES_KEPT:]

    def save(self, node_id: str, states: Sequence[tuple[str, str]]) -> None:
        """Keep the latest :data:`STATES_KEPT` of ``states`` (oldest first)."""
        if self.path is None:
            return
        kept = [list(state) for state in states[-STATES_KEPT:]]
        with self._lock:
            held = self._read()
            # Taken out and put back last: the order is the order written.
            held.pop(node_id, None)
            if sum(len(text) for _token, text in kept) <= MAX_KEPT_BYTES:
                held[node_id] = kept
            while len(held) > MAX_NODES or (len(held) > 1 and _size(held) > MAX_FILE_BYTES):
                held.pop(next(iter(held)))
            self._write(held)

    def drop(self, node_id: str) -> None:
        """Forget what was kept for ``node_id`` (its session closed, or it
        stopped being text)."""
        if self.path is None:
            return
        with self._lock:
            held = self._read()
            if held.pop(node_id, None) is not None:
                self._write(held)

    def _read(self) -> dict[str, Any]:
        if self.path is None:
            return {}
        try:
            body = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return body if isinstance(body, dict) else {}

    def _write(self, held: dict[str, Any]) -> None:
        assert self.path is not None
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex[:8]}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as out:
                out.write(json.dumps(held))
                out.flush()
                # On disk before it is renamed in: a crash after the rename
                # must not leave an empty file under the name.
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
        except OSError as failure:
            with contextlib.suppress(OSError):
                temporary.unlink()
            logger.debug("live peer: states not kept (%s)", failure)


def _size(held: dict[str, Any]) -> int:
    """How much text ``held`` keeps."""
    return sum(
        len(state[1])
        for states in held.values()
        if isinstance(states, list)
        for state in states
        if isinstance(state, list) and len(state) == 2 and isinstance(state[1], str)
    )
