"""The one rule that says where a backward transcript page may open.

Both pagers — the server's over ``chat_messages`` and the daemon's over a
project's event log — drive :func:`align_boundary`, so this is where the rule
itself is exercised: over hand-built shapes that trap it, and over generated
transcripts nobody picked, with the caller holding everything (the daemon's
case) and reading on demand (the server's).
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import pytest
from alkera_core.objects.transcript_page import Boundary, PageReach, PageRow, align_boundary

if TYPE_CHECKING:
    from collections.abc import Sequence

LIMIT = 20
REACH = PageReach(limit=LIMIT, turn=4, message=2)
BUDGET = REACH.budget


def _prompt() -> tuple[bool, str | None, bool]:
    return True, None, False


def _part(message: str) -> tuple[bool, str | None, bool]:
    return False, message, False


def _opens(message: str) -> tuple[bool, str | None, bool]:
    return False, message, True


def _plain() -> tuple[bool, str | None, bool]:
    return False, None, False


def _rows(script: Sequence[tuple[bool, str | None, bool]]) -> list[PageRow]:
    return [
        PageRow(seq=index + 1, starts_turn=turn, message_id=name, opens_message=opens)
        for index, (turn, name, opens) in enumerate(script)
    ]


def _spans(rows: Sequence[PageRow]) -> dict[str, tuple[int, int]]:
    spans: dict[str, tuple[int, int]] = {}
    for row in rows:
        if row.message_id is None:
            continue
        low, high = spans.get(row.message_id, (row.seq, row.seq))
        spans[row.message_id] = (min(low, row.seq), max(high, row.seq))
    return spans


def _split_by(spans: dict[str, tuple[int, int]], boundary: int) -> list[str]:
    return sorted(name for name, (low, high) in spans.items() if low < boundary <= high)


def _settle(rows: Sequence[PageRow], *, page_low: int, budgeted: bool = False) -> Boundary:
    """Where the page opens. ``budgeted`` hands the rule only the stretch it
    may look at — the server's read — instead of the whole log the daemon
    holds; the answer must not depend on which."""
    known = list(rows)
    if budgeted:
        floor = max(page_low - BUDGET, 1)
        known = [row for row in known if row.seq >= floor]
    return align_boundary(known, page_low=page_low, oldest=1, reach=REACH)


def _walk(rows: Sequence[PageRow], *, budgeted: bool) -> list[Boundary]:
    """Every page from the tail down, as a reader scrolling up reads them."""
    pages: list[Boundary] = []
    before = len(rows) + 1
    guard = 0
    while before > 1:
        end = before - 1
        page_low = max(end - LIMIT, 0) + 1
        held = [row for row in rows if row.seq <= end]
        answer = _settle(held, page_low=page_low, budgeted=budgeted)
        pages.append(answer)
        before = answer.seq
        guard += 1
        assert guard < 10_000, "the walk must terminate"
    return pages


# ---------------------------------------------------------------------------
# The shapes that trap it
# ---------------------------------------------------------------------------


def test_a_page_opens_on_the_turn_its_first_row_belongs_to() -> None:
    """The reader's next prompt landed inside the answer before it, so the row
    that starts the turn is under the answer's own opening row."""
    script = [_prompt(), _opens("m0"), _part("m0")]
    script += [_prompt(), _opens("m1"), _part("m1"), _prompt(), _part("m1"), _part("m1")]
    script += [_opens("m2")] + [_part("m2")] * (LIMIT + 2)
    rows = _rows(script)
    spans = _spans(rows)
    assert _split_by(spans, 7) == ["m1"], "the inner prompt shows m1's end"

    settled = _settle(rows, page_low=len(rows) - LIMIT + 1)

    assert settled == Boundary(seq=4, cut=False)
    assert rows[3].starts_turn and _split_by(spans, 4) == []


def test_a_message_the_page_cannot_open_is_reported_not_carried() -> None:
    """A message whose rows sit further apart than the read goes: the page
    holds its end, cannot reach its start, and says so."""
    script = [_prompt(), _opens("far"), _part("far")]
    script += [_plain()] * (BUDGET + LIMIT)
    script += [_prompt()] + [_part("near")] * 2 + [_opens("near")]
    script += [_part("far")] + [_part("near")] * (LIMIT - 1)
    rows = _rows(script)

    settled = _settle(rows, page_low=len(rows) - LIMIT + 1)

    assert settled.cut is True, "it carries far's end and cannot reach far's start"
    assert settled.seq >= len(rows) - LIMIT + 1 - BUDGET


def test_a_re_announced_opening_row_is_not_proof_when_nothing_below_was_read() -> None:
    """``message.created`` is re-announced from inside a turn, so holding one
    proves nothing when the descent spent its budget arriving and read no row
    under the page."""
    script = [_opens("echo"), _part("echo"), _prompt()]
    script += [_opens("answer")] + [_part("answer")] * (BUDGET - 3)
    script += [_opens("echo")] + [_part("answer")] * LIMIT
    rows = _rows(script)
    page_low = len(rows) - LIMIT + 1
    assert page_low - BUDGET == 3, "the descent's floor is the prompt row"

    settled = _settle(rows, page_low=page_low, budgeted=True)

    assert settled == Boundary(seq=3, cut=True)


def test_a_page_that_holds_nothing_of_any_message_settles_on_the_turn_alone() -> None:
    """Rows belonging to no message cannot be anyone's end, so nothing but the
    turn start can move the boundary — and a page with no turn to open on says
    it opens mid-turn."""
    rows = _rows([_prompt()] + [_plain()] * (LIMIT * 3))

    settled = _settle(rows, page_low=LIMIT * 2 + 1, budgeted=True)

    assert settled == Boundary(seq=1, cut=False), "down to the one turn there is"
    mid = _settle(_rows([_plain()] * (LIMIT * 3)), page_low=LIMIT * 2 + 1, budgeted=True)
    assert mid.cut is True, "no turn start within reach, so the page admits it"


def test_an_opening_row_inside_the_page_is_checked_against_the_whole_budget() -> None:
    """The message's other rows need not be near the page: here one sits well
    under it with nothing of the message in between, and the opening row the
    page holds is the box's SECOND announcement. Proving against the stretch
    just under the page would miss it; the proof is the whole budget."""
    script = [_prompt(), _part("m"), _plain()]
    script += [_plain()] * (LIMIT + 4)
    script += [_prompt()]
    script += [_opens("m"), _opens("n")] + [_part("n") for _ in range(LIMIT - 2)]
    rows = _rows(script)
    spans = _spans(rows)
    page_low = len(rows) - LIMIT + 1
    assert spans["m"][0] < page_low - LIMIT, "m's other row is nowhere near the page"
    assert max(page_low - BUDGET, 1) < spans["m"][0], "but well inside the budget"

    settled = _settle(rows, page_low=page_low, budgeted=True)

    assert settled.seq <= spans["m"][0], "the page drops to the row it really starts at"
    assert _split_by(spans, settled.seq) == []


def test_a_page_that_cannot_look_a_message_under_itself_says_so() -> None:
    """A descent that ends a row or two above its floor has read almost
    nothing under the page — and an opening row inside the page proves nothing
    there, because the box re-announces one from inside the turn. The rows can
    be three apart; it is the boundary's distance from the FLOOR that decides
    whether anything could have been seen."""
    settles_at = LIMIT * 3 + 1  # where the descent's floor will land
    script = [_part("m")] + [_plain()] * (settles_at - 1)
    script += [_prompt(), _opens("m"), _opens("a")]
    script += [_part("a")] * (settles_at + BUDGET + LIMIT - 1 - len(script))
    rows = _rows(script)
    spans = _spans(rows)
    page_low = len(rows) - LIMIT + 1
    floor = max(page_low - BUDGET, 1)
    assert floor == settles_at and spans["m"] == (1, settles_at + 2), "m straddles the floor"

    settled = _settle(rows, page_low=page_low, budgeted=True)

    assert settled.seq - floor < LIMIT * REACH.message, "it ended near its floor"
    assert _split_by(spans, settled.seq) == ["m"], "the page really does hold m's end"
    assert settled.cut is True, "and says so"


def test_a_message_with_a_gap_wider_than_its_reach_is_the_rule_s_known_exposure() -> None:
    """PINS THE EXPOSURE the rule's docstring names and bounds, so it cannot
    change in silence. ``m`` has a row under the page's floor, its RE-ANNOUNCED
    opening row inside the page, and NOT ONE row of it in the whole stretch
    between — a gap wider than a message's reach. The page holds its end and
    reports itself whole.

    Closing it needs the message id indexed; it lives only inside the row's
    JSON payload today, so proving absence would cost a scan of every row of
    the chat below the page instead of the budget the descent looks over now.
    """
    script = [_part("m"), _prompt()]
    script += [_plain()] * (BUDGET + LIMIT)
    script += [_prompt()]
    script += [_opens("m"), _opens("n")] + [_part("n") for _ in range(LIMIT - 3)]
    rows = _rows(script)
    spans = _spans(rows)
    page_low = len(rows) - LIMIT + 1
    floor = max(page_low - BUDGET, 1)
    assert spans["m"][0] < floor, "m's other row is under the floor"

    settled = _settle(rows, page_low=page_low, budgeted=True)

    assert settled.seq - floor >= LIMIT * REACH.message, "it looked a message under itself"
    assert _split_by(spans, settled.seq) == ["m"], "the page really does hold m's end"
    assert settled.cut is False, "and does not say so — the exposure, pinned"


# ---------------------------------------------------------------------------
# Generated transcripts
# ---------------------------------------------------------------------------


def _generated(seed: int, *, giants: bool) -> list[PageRow]:
    """Prompts landing inside a running answer, answers opened while the one
    before is still running, the box's echo of the person's message — whose
    opening row comes LAST and is re-announced later — and rows belonging to no
    message. With ``giants``, one answer also outruns the whole budget."""
    rng = random.Random(seed)
    script: list[tuple[bool, str | None, bool]] = [_prompt()]
    turns = rng.randint(6, 9)
    giant = rng.randrange(turns) if giants else -1
    for turn in range(turns):
        echo = f"u{turn}"
        script += [_part(echo) for _ in range(rng.randint(1, 3))]
        script.append(_opens(echo))
        running: list[str] = []
        announced_again = False
        for index in range(rng.randint(1, 3)):
            answer = f"a{turn}-{index}"
            script.append(_opens(answer))
            running.append(answer)
            length = BUDGET + LIMIT if turn == giant else 0
            for step in range(length or rng.randint(1, 5)):
                script.append(_part(rng.choice(running)))
                if rng.random() < 0.2:
                    script.append(_plain())
                if length and step == length // 2:
                    script.append(_prompt())
                if not announced_again and rng.random() < 0.25:
                    script.append(_opens(echo))
                    announced_again = True
            if len(running) > 1 and rng.random() < 0.5:
                running.pop(0)
        early = bool(running) and rng.random() < 0.5
        if early:
            script.append(_prompt())
        script += [_part(name) for name in running]
        if not early:
            script.append(_prompt())
    return _rows(script)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
@pytest.mark.parametrize("giants", [False, True], ids=["within-reach", "one-answer-past-reach"])
@pytest.mark.parametrize("budgeted", [False, True], ids=["whole-log", "budget-only"])
def test_the_walk_is_a_partition_of_whole_turns(seed: int, giants: bool, budgeted: bool) -> None:
    """Every row exactly once; every page bounded by its ceiling; no page
    carries a message's end unless it says ``cut``; and a transcript whose
    turns all fit the budget produces no cut page at all — without which
    "opens on a turn unless cut" would pass on a rule that cut everything.

    A caller holding the whole log and one reading on demand must land on the
    same boundaries, or the server and the daemon would page a chat
    differently.
    """
    rows = _generated(seed, giants=giants)
    spans = _spans(rows)
    trapped = [row.seq for row in rows if row.starts_turn and _split_by(spans, row.seq)]
    assert trapped, "the corpus must trap the boundary at least once"

    pages = _walk(rows, budgeted=budgeted)

    seen: list[int] = []
    upper = len(rows) + 1
    for page in pages:
        seen = [seq for seq in range(page.seq, upper)] + seen
        upper = page.seq
    assert seen == [row.seq for row in rows], "every row exactly once, in order"
    by_seq = {row.seq: row for row in rows}
    for page in pages:
        if page.seq == 1:
            continue
        if page.cut:
            assert giants, "a transcript inside the budget needs no cut page"
        else:
            assert by_seq[page.seq].starts_turn, f"page at {page.seq} opens mid-turn"
            assert _split_by(spans, page.seq) == [], f"page at {page.seq} holds an end"


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
@pytest.mark.parametrize("giants", [False, True], ids=["within-reach", "one-answer-past-reach"])
def test_a_budgeted_read_lands_where_holding_the_whole_log_lands(seed: int, giants: bool) -> None:
    """The server reads the budget and the daemon holds everything; the two
    must agree on every boundary AND on every ``cut``, or the same chat pages
    differently — one reader told to keep reading while the other is told the
    page is whole."""
    rows = _generated(seed, giants=giants)

    budgeted = _walk(rows, budgeted=True)
    whole = _walk(rows, budgeted=False)

    assert [page.seq for page in budgeted] == [page.seq for page in whole]
    assert [page.cut for page in budgeted] == [page.cut for page in whole]
