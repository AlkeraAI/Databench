"""Where people's carets are in a notebook, as every replica hears them.

A person's caret on ``doc:notebook:<item_id>`` is checked by the sandbox and
relayed to every replica as a ``doc.crdt_ephemeral`` hub event; the gateway
adds the cell the caret is in (``caret_cell``, from the sandbox's answer) to
the event beside its envelope. :class:`CaretBoard` takes those events in the
hub's publish and answers "who held a caret in these cells lately" for the
peer route's ``caret_present`` notice. A caret is gone once superseded, so
only each peer's latest one is kept.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from alkera_core.events import EventHub, HubEvent
from alkera_core.events.hub import Subscription

#: The hub event type a relayed caret travels as (the CRDT gateway's).
CARET_EVENT_TYPE: Final = "doc.crdt_ephemeral"
#: The key the gateway names a caret's cell under, beside its envelope. The
#: gateway's own spelling (``crdt.gateway.CARET_CELL_KEY``) is not imported:
#: the gateway sits above the realtime runtime that builds this board, and a
#: test pins that the two agree.
CARET_CELL_KEY: Final = "caret_cell"
#: How many notebooks the board remembers carets for.
NOTEBOOKS_KEPT: Final = 1024
#: How many carets per notebook (one per Loro peer).
CARETS_KEPT: Final = 256

_PREFIX: Final = "doc:notebook:"


@dataclass(frozen=True, slots=True)
class Caret:
    """One peer's latest caret: whose, in which cell, when (monotonic)."""

    peer: int
    cell_id: str
    who: str
    user_id: str | None
    at: float


class CaretBoard:
    """See the module docstring. ``clock`` is monotonic seconds."""

    def __init__(
        self, hub: EventHub | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._hub = hub
        self._clock = clock
        self._sub: Subscription | None = None
        self._carets: OrderedDict[str, OrderedDict[int, Caret]] = OrderedDict()

    def start(self) -> None:
        if self._hub is None or self._sub is not None:
            return

        def take(event: HubEvent) -> bool:
            self.observe(event)
            return False

        self._sub = self._hub.subscribe(take, label="notebook-carets", maxsize=1)

    def close(self) -> None:
        if self._hub is not None and self._sub is not None:
            self._hub.unsubscribe(self._sub)
        self._sub = None

    def observe(self, event: HubEvent) -> None:
        if event.type != CARET_EVENT_TYPE or event.channel is None:
            return
        if not event.channel.startswith(_PREFIX):
            return
        envelope = event.payload.get("envelope")
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(payload, dict):
            return
        peer = payload.get("loro_peer")
        if isinstance(peer, bool) or not isinstance(peer, int):
            return
        cell = event.payload.get(CARET_CELL_KEY)
        item = event.channel.removeprefix(_PREFIX)
        carets = self._carets.get(item)
        if carets is None:
            carets = self._carets[item] = OrderedDict()
            while len(self._carets) > NOTEBOOKS_KEPT:
                self._carets.popitem(last=False)
        self._carets.move_to_end(item)
        if not isinstance(cell, str) or not cell:
            # A caret that left every cell (or a cleared one) supersedes the last.
            carets.pop(peer, None)
            return
        user_id = payload.get("user_id")
        name = payload.get("display_name")
        carets[peer] = Caret(
            peer=peer,
            cell_id=cell,
            # Named when answering, by ``user_id`` first; the name the caret
            # carried answers for a person no longer known.
            who=name if isinstance(name, str) else "",
            user_id=user_id if isinstance(user_id, str) else None,
            at=self._clock(),
        )
        carets.move_to_end(peer)
        while len(carets) > CARETS_KEPT:
            carets.popitem(last=False)

    def now(self) -> float:
        """The board's clock now, which every caret's ``at`` is read on."""
        return self._clock()

    def present(
        self,
        item_id: UUID,
        cell_ids: Iterable[str],
        *,
        within: float,
        exclude_user: str | None = None,
    ) -> dict[str, list[Caret]]:
        """``cell id -> the carets in it heard within the last `within`
        seconds``, leaving out ``exclude_user``'s own."""
        carets = self._carets.get(str(item_id))
        if not carets:
            return {}
        wanted = set(cell_ids)
        horizon = self._clock() - within
        found: dict[str, list[Caret]] = {}
        for caret in carets.values():
            if caret.cell_id not in wanted or caret.at < horizon:
                continue
            if exclude_user is not None and caret.user_id == exclude_user:
                continue
            found.setdefault(caret.cell_id, []).append(caret)
        return found


__all__ = ["CARET_CELL_KEY", "CARET_EVENT_TYPE", "Caret", "CaretBoard"]
