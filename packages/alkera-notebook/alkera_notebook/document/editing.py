"""The one rule for "someone is editing this cell".

A cell is being edited while somebody's focus is in it now. Every document
store collects *claims* (who said, or showed, that they are in which cell)
and answers :meth:`DocumentStore.editing` with :func:`editing_now` over them;
the engine asks :func:`blocker` whether a cell's re-run has to wait.

Two kinds of claim:

* a **caret**: the actor publishes where its caret is, moves it and clears it
  when it leaves (blur, another cell, a hidden tab, a closed editor). The
  claim holds for as long as the caret stands there. A peer that vanished
  without clearing (a crashed tab, a lost network) stops confirming it, and
  the claim lapses :data:`CARET_UNCONFIRMED_FOR` after its last confirmation.
* an **edit** by an actor that publishes no caret (an agent, a script): the
  edit is the only sign of where it is, so it counts for
  :data:`EDIT_WITHOUT_CARET_FOR`. An actor that does publish a caret is
  wherever its caret is, so its edits claim nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from alkera_notebook.document.store import EditingInfo

#: How long a caret nobody confirmed still counts: only the fallback for a
#: peer that vanished without clearing it. Clients confirm a standing caret
#: every 10 s and drop one unheard for 30 s on their own screens.
CARET_UNCONFIRMED_FOR: Final = timedelta(seconds=30)
#: How long an edit counts as its author being in the cell, for an author
#: that publishes no caret.
EDIT_WITHOUT_CARET_FOR: Final = timedelta(seconds=15)


@dataclass(frozen=True)
class FocusClaim:
    """One actor's claim to be in one cell, last confirmed at ``at``."""

    actor_id: str
    display_name: str
    kind: Literal["person", "agent", "system"]
    cell_id: str
    at: datetime
    #: A caret the actor holds there (True), or an edit by an actor that
    #: publishes no caret (False).
    caret: bool


def editing_now(claims: Iterable[FocusClaim], *, now: datetime) -> dict[str, list[EditingInfo]]:
    """``cell id -> who is in it now``, one entry per actor (a caret before an
    edit, then the newest), carets first."""
    held = list(claims)
    with_caret = {c.actor_id for c in held if c.caret}
    newest: dict[tuple[str, str], FocusClaim] = {}
    for claim in held:
        window = CARET_UNCONFIRMED_FOR if claim.caret else EDIT_WITHOUT_CARET_FOR
        if claim.at < now - window:
            continue
        if not claim.caret and claim.actor_id in with_caret:
            continue
        key = (claim.cell_id, claim.actor_id)
        known = newest.get(key)
        if known is None or (claim.caret, claim.at) > (known.caret, known.at):
            newest[key] = claim
    found: dict[str, list[EditingInfo]] = {}
    for claim in sorted(newest.values(), key=lambda c: (not c.caret, -c.at.timestamp())):
        found.setdefault(claim.cell_id, []).append(
            EditingInfo(
                actor_id=claim.actor_id,
                display_name=claim.display_name,
                kind=claim.kind,
                at=claim.at,
                caret=claim.caret,
            )
        )
    return found


def holds_for(info: EditingInfo, *, now: datetime) -> timedelta:
    """How much longer ``info`` holds its cell from ``now`` unless renewed: a
    caret until it goes unconfirmed, an edit until its window ends. What a
    reader is told so it lets the claim go on its own, without asking again."""
    window = CARET_UNCONFIRMED_FOR if info.caret else EDIT_WITHOUT_CARET_FOR
    return max(window - (now - info.at), timedelta(0))


def blocker(infos: Sequence[EditingInfo], requester_id: str) -> EditingInfo | None:
    """Whose editing makes a re-run of the cell wait, for a run ``requester_id``
    asked for: anyone else who is in it, or the requester while their own
    caret stands in it. The requester's own earlier edit never does."""
    for info in infos:
        if info.actor_id != requester_id or info.caret:
            return info
    return None


__all__ = [
    "CARET_UNCONFIRMED_FOR",
    "EDIT_WITHOUT_CARET_FOR",
    "FocusClaim",
    "blocker",
    "editing_now",
    "holds_for",
]
