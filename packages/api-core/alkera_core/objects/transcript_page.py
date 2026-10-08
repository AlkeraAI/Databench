"""Where a backward page of a chat transcript may open. One rule, no I/O.

A reader opens a chat on its newest page and reads the pages above it as they
scroll up. Where a page BEGINS is the whole problem: a page opening in the
middle of a turn is a turn with its opening rows missing, and a page opening in
the middle of a message is parts with no message to hang them on. Both happen
for the same everyday reason — the reader sends the next prompt while the
machine is still writing, so the prompt row lands INSIDE the answer's rows.

Two reaches settle it, run alternately because neither settles it alone:

* down to the row that STARTS the turn, within ``turn_reach`` page limits;
* down past any message the boundary would open in the middle of, within
  ``message_reach`` page limits.

Lowering onto a turn's first row can land inside an answer; lowering past that
answer lands on the box's echo of the person's message, which begins one row
UNDER the row that started the turn. So they alternate to a joint fixed point,
which settles because every step strictly lowers the boundary, and the descent
stops ``(turn_reach + message_reach) * limit`` rows under the page — one page
is therefore at most ``limit`` plus that.

A message is held whole only when the page holds the row that OPENED it AND
something under the page has actually been read: ``message.created`` is NOT
written once per message — the box re-announces it from inside its own turn —
so holding one proves nothing on its own. When the page cannot account for a
message it carries, it says ``cut`` and the reader keeps reading rather than
being handed an orphaned part in silence.

Pure: rows in, a boundary out — including ``cut``, so the two readers cannot
drift on what a page is admitting to. Both the server's ``chat_messages``
pager and the daemon's local-log pager drive this, and a caller hands it the
whole budget's worth of rows rather than reading its way down a step at a
time, so the answer depends on the transcript and never on the reader.

KNOWN EXPOSURE, and its bound. The proof that a message is held whole is: the
page holds a row that opened it, and no row of it appears in the stretch read
under the page. A page whose descent ended within a message's reach of its
floor cannot look a whole message under itself and says ``cut`` outright, so
what is left needs a message with a row under the page's floor and NOT ONE of
its rows anywhere in the stretch between — a gap of at least
``limit * message_reach`` rows (400 at the limit a reader opens a chat on) —
and a re-announced opening row inside the page. ``cut=False`` is that strong
and no stronger. Closing the gap needs the message id itself to be indexed;
today it lives only inside the row's JSON payload and no index serves it, so
proving absence would cost a scan of every row of the chat below the page
(~9.5k rows on a ten-thousand-row chat, against the budget the descent looks
over now). It is pinned as it stands in the rule's tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "Boundary",
    "PageReach",
    "PageRow",
    "align_boundary",
]


class PageRow(NamedTuple):
    """One transcript row, as the page rule needs to see it.

    Each caller decides these from its own row shape: which row starts a turn,
    which message a row belongs to (by the id the row states — never its text,
    its timing or its role), and which row opened that message.
    """

    seq: int
    starts_turn: bool
    message_id: str | None
    opens_message: bool


@dataclass(frozen=True, slots=True)
class PageReach:
    """How far a page may reach down, in multiples of its own limit."""

    limit: int
    turn: int
    message: int

    @property
    def budget(self) -> int:
        """Rows the boundary may descend below the page it started as."""
        return self.limit * (max(self.turn, 1) + max(self.message, 1))


@dataclass(frozen=True, slots=True)
class Boundary:
    """Where the page opens, and whether it is still carrying someone's middle."""

    seq: int
    cut: bool


def _unaccounted(rows: Sequence[PageRow]) -> set[str]:
    """Messages ``rows`` shows part of without showing the row that opened them."""
    carried: set[str] = set()
    opened: set[str] = set()
    for row in rows:
        if row.message_id is None:
            continue
        carried.add(row.message_id)
        if row.opens_message:
            opened.add(row.message_id)
    return carried - opened


def _reach_below(below: Sequence[PageRow], page: Sequence[PageRow]) -> tuple[bool, int | None]:
    """``(unproved, target)`` for a page opening above ``below``.

    ``target`` is the lowest sequence the page would have to drop to for the
    messages it cannot account for, or ``None`` when it cannot see them at all
    — which is not a licence to carry them quietly, it is what ``cut`` is for.
    """
    unproved = _unaccounted(page)
    carried = {row.message_id for row in page if row.message_id is not None}
    lowest: dict[str, int] = {}
    for row in below:
        if row.message_id is None or row.message_id not in carried:
            continue
        unproved.add(row.message_id)
        lowest.setdefault(row.message_id, row.seq)
    reachable = [lowest[name] for name in unproved if name in lowest]
    return bool(unproved), min(reachable) if reachable else None


def align_boundary(
    known: Sequence[PageRow], *, page_low: int, oldest: int = 1, reach: PageReach
) -> Boundary:
    """Where the page whose ``limit`` rows begin at ``page_low`` should open.

    ``known`` is every row from ``max(page_low - reach.budget, oldest)`` up
    through the page, ascending — the whole stretch the descent may look at.
    Deciding on exactly that stretch and no more is what makes the answer the
    caller's row data and nothing else: a caller holding the entire log and
    one that read only the budget must open the page in the same place and
    agree on ``cut``, or the same chat pages differently for two readers.

    ``oldest`` is the lowest sequence the transcript still holds, so a page
    that opens on the very first row is never reported as carrying a middle.
    """
    turn_reach = reach.limit * max(reach.turn, 1)
    message_reach = reach.limit * max(reach.message, 1)
    floor = max(page_low - reach.budget, oldest)
    boundary = page_low
    cut = False
    while True:
        page = [row for row in known if row.seq >= boundary]
        if not page:
            return Boundary(seq=boundary, cut=cut and boundary > oldest)
        moved = False
        if not page[0].starts_turn:
            turn_floor = max(boundary - turn_reach, floor)
            starts = [
                row.seq for row in known if row.starts_turn and turn_floor <= row.seq < boundary
            ]
            if starts:
                boundary = max(starts)
                moved = True
                page = [row for row in known if row.seq >= boundary]
        # Only a row that names a message can be the end of one, so a page
        # holding none of them cannot be splitting anything.
        if any(row.message_id is not None for row in page):
            below = [row for row in known if floor <= row.seq < boundary]
            unproved, target = _reach_below(below, page)
            if floor > oldest and boundary - floor < message_reach:
                # The descent came within a message's reach of its floor, so
                # what it can see under the page is shorter than a message —
                # and an opening row inside the page proves nothing on its own,
                # because ``message.created`` is re-announced from inside the
                # turn and the one in the page can be the second of two. A page
                # that cannot look a whole message under itself says so.
                cut = True
            elif target is None:
                # Nothing left to reach for. With the chat's first row inside
                # the budget there is nothing below and the page is whole;
                # with the budget spent short of it, what the page cannot open
                # may run on underneath, and it says so.
                cut = cut or (unproved and floor > oldest)
            else:
                boundary = target
                moved = True
        if not moved:
            opens_a_turn = next(row.starts_turn for row in known if row.seq >= boundary)
            return Boundary(seq=boundary, cut=(cut or not opens_a_turn) and boundary > oldest)
