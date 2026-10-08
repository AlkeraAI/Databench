"""The backward page over a chat's transcript.

A chat opens on its newest page and reads the pages above it on demand, so
the read that matters is the one that walks a long transcript from its tail
to its head: every row exactly once, each page starting on a turn when a turn
starts that close, a turn longer than a page cut rather than swallowed, and no
page boundary ever landing inside a message. The live tail — the forward read
from the newest page's highest sequence — must join that walk with no overlap,
and the realtime window must be able to say where each of its retained entries
sits in the transcript.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import actor_for_user
from alkera_core.models import ChatMessage, TeamRole, User, WorkspaceObject
from alkera_core.schemas.realtime import DocEnvelope, OpPayload
from backend.services.chats import chat_service
from backend.services.chats import templates as chat_template_service
from backend.services.realtime import channels as channel_service
from backend.services.realtime.channels import Channel, ChannelGrant
from backend.services.realtime.docsync import DocRegistry
from backend.services.realtime.filters import load_entitlements
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, app_client, login, make_member

LIMIT = 100

# ---------------------------------------------------------------------------
# Building a long transcript quickly
# ---------------------------------------------------------------------------


async def _admin(db: AsyncSession, org: OrgWithAdmin) -> User:
    user = await db.get(User, org.admin_id)
    assert user is not None
    return user


async def _chat(db: AsyncSession, owner: User) -> WorkspaceObject:
    chat, _ = await chat_service.create_chat(
        db,
        owner=owner,
        title="Long",
        client_id=None,
        machine_id=None,
        machine_status="none",
        org_id=owner.home_org_team_id,
    )
    await db.commit()
    return chat


def _turn_lengths(total: int, pattern: list[int]) -> list[int]:
    """Assistant-row counts per turn, cycling ``pattern`` until ``total`` rows."""
    lengths: list[int] = []
    rows = 0
    index = 0
    while rows < total:
        length = min(pattern[index % len(pattern)], total - rows - 1)
        lengths.append(max(length, 0))
        rows += 1 + max(length, 0)
        index += 1
    return lengths


async def _fill(
    db: AsyncSession, chat: WorkspaceObject, *, turns: list[int], start_seq: int = 1
) -> list[tuple[int, str]]:
    """Write ``turns`` — each one user row followed by that many assistant
    rows — straight into the table, returning ``(seq, role)`` per row.
    The service writes one INSERT per event; a ten-thousand-row transcript is
    written in one statement here because the read, not the write, is under
    test."""
    rows: list[dict[str, Any]] = []
    shape: list[tuple[int, str]] = []
    seq = start_seq
    for count in turns:
        for role in ["user", *(["assistant"] * count)]:
            rows.append(
                {
                    "id": uuid4(),
                    "chat_id": chat.id,
                    "org_team_id": chat.org_team_id,
                    "seq": seq,
                    "role": role,
                    "kind": "prompt" if role == "user" else "message.completed",
                    "event_id": f"e{seq}",
                    "payload": {"event_id": f"e{seq}", "role": role, "text": f"row {seq}"},
                }
            )
            shape.append((seq, role))
            seq += 1
    for offset in range(0, len(rows), 2000):
        await db.execute(pg_insert(ChatMessage).values(rows[offset : offset + 2000]))
    await db.commit()
    return shape


async def _walk_pages(
    db: AsyncSession, chat_id: UUID, *, limit: int
) -> list[tuple[list[ChatMessage], bool, bool]]:
    """Every backward page from the tail to the oldest row, newest page first,
    each with the ``(cut, has_older)`` it came back with."""
    pages: list[tuple[list[ChatMessage], bool, bool]] = []
    rows, prev_before, has_older, cut = await chat_service.list_messages_before(
        db, chat_id=chat_id, before=None, limit=limit
    )
    pages.append((rows, cut, has_older))
    guard = 0
    while has_older:
        assert prev_before is not None
        rows, prev_before, has_older, cut = await chat_service.list_messages_before(
            db, chat_id=chat_id, before=prev_before, limit=limit
        )
        pages.append((rows, cut, has_older))
        guard += 1
        assert guard < 10_000, "the walk must terminate"
    return pages


async def _walk(db: AsyncSession, chat_id: UUID, *, limit: int) -> list[list[ChatMessage]]:
    """Every backward page from the tail to the oldest row, newest page first."""
    return [rows for rows, _cut, _older in await _walk_pages(db, chat_id, limit=limit)]


# ---------------------------------------------------------------------------
# Building a transcript whose rows say which message they belong to
# ---------------------------------------------------------------------------


def _prompt() -> dict[str, Any]:
    """A person's message: flat, and belonging to no machine message."""
    return {"role": "user", "kind": "prompt", "message": None, "on_part": False}


def _event(
    kind: str, message: str | None = None, *, role: str = "assistant", on_part: bool = False
) -> dict[str, Any]:
    """A row the machine published. ``on_part`` states the message id on the
    part the event carries — where a ``part.created`` really puts it — rather
    than flat on the event."""
    return {"role": role, "kind": kind, "message": message, "on_part": on_part}


async def _script(
    db: AsyncSession, chat: WorkspaceObject, specs: list[dict[str, Any]]
) -> dict[str, tuple[int, int]]:
    """Write ``specs`` as consecutive rows from sequence 1, in the envelope the
    machine's events are stored in, and return each message's ``(first, last)``
    sequence as the SCRIPT declares it — the spans the page boundary is then
    asserted against, computed here from what the test wrote rather than from
    what the service reads back."""
    rows: list[dict[str, Any]] = []
    spans: dict[str, tuple[int, int]] = {}
    for index, spec in enumerate(specs):
        seq = index + 1
        kind, role, message = spec["kind"], spec["role"], spec["message"]
        if kind == "prompt":
            payload: dict[str, Any] = {"kind": kind, "event_id": f"e{seq}", "text": f"row {seq}"}
        else:
            event: dict[str, Any] = {"event_id": f"e{seq}", "event_type": kind}
            if message is not None:
                if spec["on_part"]:
                    event["part"] = {"part_id": f"p{seq}", "message_id": message}
                else:
                    event["message_id"] = message
            payload = {"event_id": f"e{seq}", "kind": kind, "role": role, "payload": event}
        if message is not None:
            low, high = spans.get(message, (seq, seq))
            spans[message] = (min(low, seq), max(high, seq))
        rows.append(
            {
                "id": uuid4(),
                "chat_id": chat.id,
                "org_team_id": chat.org_team_id,
                "seq": seq,
                "role": role,
                "kind": kind,
                "event_id": f"e{seq}",
                "payload": payload,
            }
        )
    for offset in range(0, len(rows), 2000):
        await db.execute(pg_insert(ChatMessage).values(rows[offset : offset + 2000]))
    await db.commit()
    return spans


def _split_by(spans: dict[str, tuple[int, int]], boundary: int) -> list[str]:
    """The messages a page starting at ``boundary`` would show the end of
    without their start."""
    return sorted(name for name, (low, high) in spans.items() if low < boundary <= high)


def _message_of(payload: dict[str, Any]) -> str | None:
    """Which message a stored row belongs to, read the way a reader of the raw
    transcript dump has to read it — flat on the event, or on the part."""
    event = payload.get("payload") if isinstance(payload.get("payload"), dict) else None
    event = event or payload
    flat = event.get("message_id")
    if isinstance(flat, str) and flat:
        return flat
    part = event.get("part")
    if isinstance(part, dict) and isinstance(part.get("message_id"), str) and part["message_id"]:
        return str(part["message_id"])
    return None


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


async def test_ten_thousand_rows_page_from_the_tail_with_no_gap_and_no_duplicate(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Every row exactly once, every page bounded, every page that CAN start on a
    turn does — the whole contract of the backward read, on a transcript long
    enough that a page count or an off-by-one at either end cannot hide."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    # Turns of every shape a real chat has: a one-liner, a chatty answer, a
    # tool-heavy one, and one longer than a page (LIMIT * 3), which no page can
    # start on.
    shape = await _fill(
        real_session, chat, turns=_turn_lengths(10_000, [3, 12, 40, 7, LIMIT * 3, 1])
    )
    assert shape[-1][0] == 10_000
    user_seqs = {seq for seq, role in shape if role == "user"}

    pages = await _walk(real_session, chat.id, limit=LIMIT)

    seen = [row.seq for page in reversed(pages) for row in page]
    assert seen == list(range(1, 10_001)), "the walk covers the transcript exactly once"
    for page in pages:
        assert page == sorted(page, key=lambda row: row.seq), "a page is ascending"
    reach = LIMIT * settings.chat_page_turn_reach
    for page in pages[:-1]:
        assert len(page) >= LIMIT, f"a page is at least the limit, got {len(page)}"
        # A page is at most the limit plus how far it reaches for a turn start.
        assert len(page) <= LIMIT + reach, f"a page is bounded, got {len(page)}"
    for page in pages:
        low = page[0].seq
        if low in user_seqs:
            continue
        # A page that does not start on a turn had no turn start within reach.
        nearest = max(seq for seq in user_seqs if seq < low)
        assert low - nearest > reach, f"page at {low} should have aligned to {nearest}"


async def test_a_turn_longer_than_a_page_but_within_reach_is_swallowed_whole(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A cold open holds no page above to repair a cut with, so a page does not
    open inside a turn whose prompt row is within reach: the tail reaches down
    to it and carries the whole turn, and says it is not cut."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    assert settings.chat_page_turn_reach >= 3, "the turn below must sit within reach"
    await _fill(real_session, chat, turns=[2, LIMIT * 3, 2])
    # rows: 1 user, 2-3 asst | 4 user, 5..304 asst | 305 user, 306-307 asst

    tail, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )
    # 100 rows would start at 208, inside the giant turn; the page reaches down
    # to that turn's prompt row at 4 and carries the whole turn.
    assert [row.seq for row in tail] == list(range(4, 308))
    assert tail[0].kind == "prompt"
    assert (prev_before, has_older, cut) == (4, True, False)

    first, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=4, limit=LIMIT
    )
    assert [row.seq for row in first] == [1, 2, 3]
    assert (prev_before, has_older, cut) == (1, False, False)


async def test_a_turn_longer_than_the_reach_is_cut_and_the_page_says_so(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Past the reach a turn is cut rather than pulled whole for one page of
    it: the page is capped at the limit plus the reach, carries ``cut``, and
    the walk above still covers every row of the turn exactly once."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    reach = LIMIT * settings.chat_page_turn_reach
    giant = reach + LIMIT * 2
    await _fill(real_session, chat, turns=[2, giant, 2])
    # rows: 1 user, 2-3 asst | 4 user, 5..4+giant asst | 5+giant user, +2 asst
    last_prompt = 5 + giant

    tail, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )
    # The limit reaches back to the last turn's start and into the giant turn;
    # its prompt row at 4 is further down than the reach, so the cut stays.
    page_low = last_prompt + 2 - LIMIT + 1
    assert [row.seq for row in tail] == list(range(page_low, last_prompt + 3))
    assert len(tail) == LIMIT
    assert tail[0].role == "assistant"
    assert (prev_before, has_older, cut) == (page_low, True, True)

    # The walk above covers the giant turn exactly once, each page bounded, and
    # the page that finally reaches the prompt row is the one not cut.
    pages = await _walk(real_session, chat.id, limit=LIMIT)
    seen = [row.seq for page in reversed(pages) for row in page]
    assert seen == list(range(1, last_prompt + 3))
    assert all(len(page) <= LIMIT + reach for page in pages)
    flags = [
        (
            await chat_service.list_messages_before(
                real_session, chat_id=chat.id, before=page[-1].seq + 1, limit=LIMIT
            )
        )[3]
        for page in pages
    ]
    assert flags[0] is True and flags[-1] is False
    assert [page[0].kind for page in pages[-2:]] == ["prompt", "prompt"]


async def test_the_box_s_own_user_message_row_does_not_anchor_a_page(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The box repeats `message.created` for the person's message from inside
    its answer; a page anchored to that repeat would open inside the turn."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    await _fill(real_session, chat, turns=[LIMIT * 2])
    # rows: 1 user (prompt), 2..201 assistant. Re-spell row 150 as the box's
    # re-announce of the person's message: role user, kind message.created.
    await real_session.execute(
        ChatMessage.__table__.update()
        .where(ChatMessage.chat_id == chat.id, ChatMessage.seq == 150)
        .values(role="user", kind="message.created")
    )
    await real_session.commit()

    tail, prev_before, has_older, _cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )
    assert tail[0].seq == 1 and tail[0].kind == "prompt"
    assert (prev_before, has_older) == (1, False)


# ---------------------------------------------------------------------------
# A page boundary never lands inside a message
# ---------------------------------------------------------------------------


def _message(name: str, parts: int, *, role: str = "assistant") -> list[dict[str, Any]]:
    """A whole message's rows: the open, ``parts`` part rows, the close."""
    rows = [_event("message.created", name, role=role)]
    rows += [
        _event(
            "part.started" if index % 2 == 0 else "part.created",
            name,
            role=role,
            on_part=index % 2 == 1,
        )
        for index in range(parts)
    ]
    rows.append(_event("message.completed", name, role=role))
    return rows


def _prompt_inside_an_answer() -> list[dict[str, Any]]:
    """The shape the bug was found on: the reader sends the next prompt while
    the machine is still writing the rows of the message before it, so the
    prompt row sits INSIDE that message's span — row 1 and 2-21 an earlier
    turn, the prompt at 22 the turn m2 (23-29) answers, the prompt at 26 the
    one that landed inside m2, and 30-129 the answer to it."""
    specs = [_prompt()]
    specs += _message("m1", 18)
    specs += [
        _prompt(),
        _event("message.created", "m2"),
        _event("part.started", "m2"),
        _event("part.created", "m2", on_part=True),
        _prompt(),
        _event("part.started", "m2"),
        _event("part.created", "m2", on_part=True),
        _event("message.completed", "m2"),
    ]
    specs += _message("m3", 98)
    return specs


def _plain_transcript() -> list[dict[str, Any]]:
    """Turns that do not interleave: every message opens and closes before the
    next prompt. The page must not reach for anything here."""
    specs: list[dict[str, Any]] = []
    for index, parts in enumerate([3, 40, 12, LIMIT * 2, 7, 1]):
        specs.append(_prompt())
        specs += _message(f"t{index}", parts)
    return specs


async def test_a_prompt_recorded_inside_an_answer_does_not_cut_that_answer_in_half(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A page anchored on a prompt row that sits inside another message's rows
    would open on the END of that message — its parts with no message to hang
    them on. Reaching down to that message's first row is not the answer on its
    own either: the message is the box's answer (and, on a real transcript, its
    echo of the person's message), so its first row sits just BELOW the prompt
    the turn began at, and a page opening there still reads from the middle of
    its first turn. The page opens on the prompt row and carries both."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    spans = await _script(real_session, chat, _prompt_inside_an_answer())
    assert spans["m2"] == (23, 29) and spans["m3"] == (30, 129)
    # The trap is real: anchoring on the prompt row alone splits m2, and
    # reaching only to m2's first row opens one row above its turn.
    assert _split_by(spans, 26) == ["m2"]

    tail, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )

    assert tail[0].seq == 22 and tail[0].kind == "prompt", "the page opens on the turn's prompt"
    assert [row.seq for row in tail] == list(range(22, 130))
    assert _split_by(spans, tail[0].seq) == [], "the page opens on no half a message"
    assert (prev_before, has_older, cut) == (22, True, False)
    assert 26 in [row.seq for row in tail], "the prompt that landed inside it is still there"


@pytest.mark.parametrize(
    ("build", "trap"),
    [
        pytest.param(_prompt_inside_an_answer, 26, id="a-prompt-inside-an-answer"),
        pytest.param(_plain_transcript, None, id="turns-that-do-not-interleave"),
    ],
)
async def test_the_walk_covers_every_row_once_and_opens_no_page_inside_a_message(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    build: Any,
    trap: int | None,
) -> None:
    """Whatever the page reaches for, the walk is still a partition: every row
    exactly once, every page boundary outside every message's span, and every
    page that has anything below it opening on a turn."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    specs = build()
    spans = await _script(real_session, chat, specs)
    if trap is not None:
        assert _split_by(spans, trap) != [], "the shape must actually trap the old rule"

    pages = await _walk_pages(real_session, chat.id, limit=LIMIT)

    seen = [row.seq for page, _cut, _older in reversed(pages) for row in page]
    assert seen == list(range(1, len(specs) + 1)), "every row exactly once, in order"
    for page, cut, has_older in pages:
        assert page == sorted(page, key=lambda row: row.seq), "a page is ascending"
        assert _split_by(spans, page[0].seq) == [], f"page at {page[0].seq} opens inside a message"
        if has_older:
            assert not cut, f"page at {page[0].seq} had a clean boundary within reach"
            assert page[0].kind == "prompt", f"page at {page[0].seq} opens mid-turn"


async def test_a_message_longer_than_the_reach_is_flagged_and_the_next_page_completes_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """One enormous message must not make a page unbounded. The page reaches as
    far as its budget and says it is still carrying the message's middle, so a
    reader keeps going rather than believing it has the whole thing."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    budget = LIMIT * (settings.chat_page_turn_reach + settings.chat_page_message_reach)
    anchor = budget + 50  # the prompt row, further above the big message's start
    specs = [_prompt()]
    specs += [_event("message.created", "big")]
    specs += [_event("part.started", "big") for _ in range(anchor - 3)]
    specs += [
        _prompt(),
        _event("part.started", "big"),
        _event("part.created", "big", on_part=True),
        _event("message.completed", "big"),
    ]
    specs += _message("later", LIMIT - 2)
    spans = await _script(real_session, chat, specs)
    assert spans["big"] == (2, anchor + 3)
    assert specs[anchor - 1]["kind"] == "prompt"
    floor = len(specs) - LIMIT + 1 - budget  # the deepest the descent may go

    tail, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )

    # Reached as far down as the budget allows, and said so rather than
    # presenting the middle of `big` as if it were the whole message.
    assert tail[0].seq == floor
    assert (prev_before, has_older, cut) == (floor, True, True)
    assert [row.seq for row in tail] == list(range(floor, len(specs) + 1))
    assert len(tail) == LIMIT + budget, "and no further than the budget"

    below, prev_below, older_still, _cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=prev_before, limit=LIMIT
    )
    assert below[0].seq == 1 and below[-1].seq == floor - 1
    assert (prev_below, older_still) == (1, False)
    covered = [row.seq for row in below] + [row.seq for row in tail]
    assert covered == list(range(1, len(specs) + 1)), "no gap, no duplicate across the cap"
    first, last = spans["big"]
    assert set(range(first, last + 1)) <= set(covered), "the two pages carry the whole message"


async def test_a_page_opening_inside_a_persons_message_reaches_its_first_row(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The machine re-announces the person's message from inside its own turn,
    and it publishes that message's PARTS before its ``message.created``. So
    "the message began earlier" cannot be read off the open row's position —
    it is the rows' message ids that say which message a row belongs to."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    specs = [_prompt()]
    specs += _message("pad", 27)
    specs += [_event("part.started", "umsg", role="user") for _ in range(9)]
    specs += [_event("message.created", "umsg", role="user")]
    specs += _message("reply", 8)
    spans = await _script(real_session, chat, specs)
    assert spans["umsg"] == (31, 40)
    assert specs[39]["kind"] == "message.created", "the open row is the message's LAST row"
    assert _split_by(spans, 36) == ["umsg"], "the trap: a page opening at 36 shows half of it"

    # An OLDER page, not the tail, and with no prompt row within reach.
    page, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=41, limit=5
    )

    assert [row.seq for row in page] == list(range(31, 41))
    assert (prev_before, has_older, cut) == (31, True, True)


async def test_two_prompts_landing_inside_two_open_answers_are_both_repaired(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Messages nest: the machine opens a second one while the first is still
    running, and a prompt lands inside each. Reaching down to the nearer
    message's first row lands inside the outer one, so the page keeps reaching
    until its first row is inside nothing — and then one row further, onto the
    prompt the outer message's own turn began at."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    specs = [_prompt()]
    specs += _message("pad", 21)
    specs += [
        _prompt(),
        _event("message.created", "a"),
        _event("part.started", "a"),
        _event("part.created", "a", on_part=True),
        _event("part.started", "a"),
        _prompt(),
        _event("message.created", "b"),
        _event("part.started", "b"),
        _event("part.created", "b", on_part=True),
        _prompt(),
        _event("part.started", "b"),
        _event("message.completed", "b"),
        _event("part.started", "a"),
        _event("message.completed", "a"),
    ]
    specs += _message("c", 98)
    spans = await _script(real_session, chat, specs)
    assert (spans["a"], spans["b"]) == ((26, 38), (31, 36))
    assert _split_by(spans, 34) == ["a", "b"], "the nearer prompt splits both"
    assert _split_by(spans, 31) == ["a"], "and reaching only to b's first row still splits a"
    assert specs[24]["kind"] == "prompt", "the turn a answers begins at 25"

    tail, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )

    assert tail[0].seq == 25 and tail[0].kind == "prompt"
    assert [row.seq for row in tail] == list(range(25, 139))
    assert (prev_before, has_older, cut) == (25, True, False)
    assert {30, 34} <= {row.seq for row in tail}, "both prompts came with it"


async def test_a_re_announced_open_row_is_not_proof_the_page_holds_the_message(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """``message.created`` is NOT written once per message: the box re-announces
    the person's message from inside its own turn, so a page can hold the LATER
    announcement while the message's real first rows sit below it.

    Here the descent spends its whole budget arriving at a prompt row, so it
    has read nothing under the page at all — and the page holds a re-announced
    open row for ``echo`` whose first rows are three below. Holding an open row
    is proof of nothing when nothing under the page has been looked at.
    """
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    budget = LIMIT * (settings.chat_page_turn_reach + settings.chat_page_message_reach)
    specs = [
        _prompt(),  # 1
        _event("message.created", "echo", role="user"),  # 2 — the REAL open row
        _event("part.started", "echo", role="user"),  # 3
        _event("part.started", "echo", role="user"),  # 4
        _prompt(),  # 5 — exactly the descent's floor
        _event("message.created", "answer"),  # 6
    ]
    # Sized so the page's own first row sits exactly ``budget`` above row 5.
    specs += [_event("part.started", "answer") for _ in range(budget - 3)]
    specs += [_event("message.created", "echo", role="user")]  # the re-announcement
    specs += [_event("part.started", "answer") for _ in range(LIMIT)]
    spans = await _script(real_session, chat, specs)
    floor = len(specs) - LIMIT + 1 - budget
    assert floor == 5 and specs[4]["kind"] == "prompt", "the descent lands on its floor"
    assert spans["echo"][0] == 2 and spans["echo"][1] > floor, "re-announced above the floor"
    assert _split_by(spans, floor) == ["echo"], "so a page opening there holds its tail"

    tail, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )

    assert (tail[0].seq, tail[0].kind) == (floor, "prompt")
    assert (prev_before, has_older, cut) == (floor, True, True)


async def test_a_message_named_only_by_its_parts_still_holds_the_page_open(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A ``part.created`` states its message id on the PART it carries, not on
    the event. Here the only rows of ``m`` above the prompt are those, so a
    read that looked no further than the event would see the page carrying
    nothing and open on the prompt, in the middle of ``m``."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    specs = [_prompt()]
    specs += _message("pad", 19)
    specs += [
        _prompt(),  # 23 — the turn m answers
        _event("message.created", "m"),  # 24
        _event("part.started", "m"),  # 25
        _prompt(),  # 26 — sent while m was still writing
        # From here on, m is named ONLY on the parts these rows carry.
        _event("part.created", "m", on_part=True),  # 27
        _event("part.created", "m", on_part=True),  # 28
    ]
    specs += _message("next", 98)  # 29..128
    spans = await _script(real_session, chat, specs)
    assert spans["m"] == (24, 28)
    assert all(specs[index]["on_part"] for index in (26, 27)), "named on the part alone up there"
    assert not any(specs[index]["on_part"] for index in (23, 24)), "and flat below the prompt"

    tail, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )

    assert (tail[0].seq, tail[0].kind) == (23, "prompt")
    assert (prev_before, has_older, cut) == (23, True, False)
    assert _split_by(spans, tail[0].seq) == []


async def test_a_page_never_carries_a_message_s_tail_in_silence(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A message's rows need not be neighbours. Here one row of ``sparse``
    lands in the page and the rest of it sits hundreds of rows below, with
    nothing of it in between — so a read that decided "nothing of this message
    is just under the page, therefore the page holds it whole" would open on a
    prompt row, look correct, and hand the reader an orphaned part.

    The page proves it holds a message by holding the row that OPENED it, and
    says ``cut`` when it cannot: the reader is told to keep reading.
    """
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    specs = [_prompt(), _event("message.created", "sparse"), _event("part.started", "sparse")]
    specs += [_prompt()]  # 4
    specs += _message("pad", 794)  # 5..800
    specs += [_prompt()]  # 801 — a clean turn start right under the page
    specs += [_event("message.created", "filler")]  # 802
    specs += [_event("part.started", "filler") for _ in range(98)]  # 803..900
    specs += [_event("part.started", "sparse")]  # 901 — the far-flung row
    specs += [_event("message.completed", "filler")]  # 902
    spans = await _script(real_session, chat, specs)
    assert spans["sparse"] == (2, 901) and spans["pad"] == (5, 800)
    budget = LIMIT * (settings.chat_page_turn_reach + settings.chat_page_message_reach)
    assert 901 - 2 > budget, "the message's own rows are further apart than the read goes"
    assert _split_by(spans, 801) == ["sparse"], "a page opening at 801 holds sparse's tail"

    tail, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )

    # It opens on a prompt row and still says cut, because `sparse` is in it
    # and its opening row is not.
    assert (tail[0].seq, tail[0].kind) == (801, "prompt")
    assert (prev_before, has_older, cut) == (801, True, True)
    assert 901 in [row.seq for row in tail]


async def test_a_cancelled_prompt_row_does_not_make_every_page_say_it_is_cut(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A ``prompt.cancelled`` row states the id of the PROMPT it cancels, and
    no ``message.created`` ever opens a prompt. Read as membership it is a
    message the page can never account for, so a page carrying one would keep
    telling the reader to read on for something that was never there."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    # Long enough that the descent stops on its budget rather than on row 1,
    # which is the only case where an unaccountable message forces a cut.
    specs = [_prompt()]
    specs += _message("filler", 600)  # 2..603
    specs += [_prompt()]  # 604
    specs += _message("answer", 58)  # 605..664
    specs += [_prompt()]  # 665
    specs += _message("last", 62)  # 666..729
    specs += [_event("prompt.cancelled", "usr:web-1")]  # 730
    spans = await _script(real_session, chat, specs)
    assert spans["usr:web-1"] == (730, 730) and "usr:web-1" not in ("answer", "last")
    budget = LIMIT * (settings.chat_page_turn_reach + settings.chat_page_message_reach)
    assert len(specs) - LIMIT + 1 - budget > 1, "the descent must stop short of the first row"

    tail, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )

    assert (tail[0].seq, tail[0].kind) == (604, "prompt")
    assert (prev_before, has_older, cut) == (604, True, False)


async def test_a_boundary_is_lowered_by_each_reach_in_turn_until_both_hold(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Neither reach settles this on its own, and neither settles it in one
    pass each. The page lands on a prompt inside two nested answers; dropping
    past them reaches a row that opens no turn; the prompt that turn began at
    is far enough down that the rows it pulls in belong to an ANSWER BEFORE it
    which has to be dropped past in turn. Five steps, alternating."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    specs = [_prompt()]
    specs += _message("pre", 8)  # 2..11
    specs += [
        _prompt(),  # 12 — the turn `w` answers
        _event("message.created", "w"),  # 13
        *[_event("part.started", "w") for _ in range(6)],  # 14..19
        _prompt(),  # 20 — sent while `w` was still writing
        *[_event("part.started", "w") for _ in range(4)],  # 21..24
        _event("message.completed", "w"),  # 25
        _event("message.created", "outer"),  # 26
        _event("part.started", "outer"),  # 27
        _prompt(),  # 28 — inside `outer`
        _event("message.created", "inner"),  # 29
        _event("part.started", "inner"),  # 30
        _prompt(),  # 31 — inside `inner` AND `outer`
        _event("part.created", "inner", on_part=True),  # 32
        _event("message.completed", "inner"),  # 33
        _event("part.started", "outer"),  # 34
        _event("message.completed", "outer"),  # 35
    ]
    specs += _message("last", 98)  # 36..135
    spans = await _script(real_session, chat, specs)
    assert (spans["w"], spans["outer"], spans["inner"]) == ((13, 25), (26, 35), (29, 33))
    # 31 shows the end of both nested answers; dropping past them reaches 26,
    # which splits nothing — a read that stopped there would open mid-turn.
    assert _split_by(spans, 31) == ["inner", "outer"]
    assert _split_by(spans, 26) == []
    # The turn that answer belongs to began at 20 — which is inside `w`.
    assert _split_by(spans, 20) == ["w"]

    tail, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )

    assert (tail[0].seq, tail[0].kind) == (12, "prompt")
    assert (prev_before, has_older, cut) == (12, True, False)
    assert [row.seq for row in tail] == list(range(12, 136))


async def test_the_descent_stops_at_its_ceiling_however_far_the_turn_runs(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A page is bounded whatever the transcript does: one turn, one message,
    both longer than either reach, and the page still comes back no larger than
    the ceiling — its own limit PLUS the whole descent budget, 1400 rows at the
    shipped limit of 200 — and says it is cut."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    budget = LIMIT * (settings.chat_page_turn_reach + settings.chat_page_message_reach)
    ceiling = LIMIT + budget
    specs = [_prompt(), _event("message.created", "endless")]
    specs += [_event("part.started", "endless") for _ in range(ceiling * 2)]
    specs += [_event("message.completed", "endless")]
    await _script(real_session, chat, specs)

    tail, prev_before, has_older, cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )

    assert len(tail) <= ceiling, "a page never outgrows limit + the descent budget"
    assert tail[-1].seq == len(specs) and prev_before == tail[0].seq
    assert tail[0].seq >= len(specs) - ceiling + 1
    assert (has_older, cut) == (True, True)


def _generated_transcript(seed: int, *, giants: bool) -> list[dict[str, Any]]:
    """A transcript built from the shapes a real one puts in the page's way.

    Prompts landing inside an answer still being written, answers opened while
    the one before them is still running, the box's echo of the person's
    message — whose ``message.created`` is its LAST row, and which it
    re-announces later from inside its own turn — and rows belonging to no
    message at all. With ``giants``, one turn's answer also runs longer than
    the read will reach for, which is the one thing that may cut a page.
    """
    rng = random.Random(seed)
    specs: list[dict[str, Any]] = [_prompt()]
    turns = rng.randint(8, 12)
    giant = rng.randrange(turns) if giants else -1
    for turn in range(turns):
        echo = f"u{turn}"
        for _ in range(rng.randint(1, 4)):
            specs.append(_event("part.started", echo, role="user"))
        # The echo's open row comes LAST, the way the machine publishes it.
        specs.append(_event("message.created", echo, role="user"))
        running: list[str] = []
        announced_again = False
        for index in range(rng.randint(1, 3)):
            answer = f"a{turn}-{index}"
            specs.append(_event("message.created", answer))
            running.append(answer)
            length = LIMIT * (settings.chat_page_turn_reach + 3) if turn == giant else 0
            for step in range(length or rng.randint(1, 14)):
                specs.append(_event("part.started", rng.choice(running)))
                if rng.random() < 0.5:
                    specs.append(_event("part.created", rng.choice(running), on_part=True))
                if rng.random() < 0.2:
                    specs.append(_event("session.status_changed", role="system"))
                if length and step == length // 2:
                    # The reader keeps typing while the giant answer runs.
                    specs.append(_prompt())
                if not announced_again and rng.random() < 0.25:
                    # The box says `message.created` for the person's message
                    # AGAIN, from inside the answer. It is not a unique row.
                    specs.append(_event("message.created", echo, role="user"))
                    announced_again = True
            if len(running) > 1 and rng.random() < 0.5:
                specs.append(_event("message.completed", running.pop(0)))
        early = bool(running) and rng.random() < 0.5
        if early:
            specs.append(_prompt())
        for answer in running:
            specs.append(_event("part.started", answer))
            specs.append(_event("message.completed", answer))
        if not early:
            specs.append(_prompt())
    return specs


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("giants", [False, True], ids=["within-reach", "one-answer-past-reach"])
async def test_a_generated_transcript_pages_as_a_partition_of_whole_turns(
    real_session: AsyncSession, org_admin: OrgWithAdmin, seed: int, giants: bool
) -> None:
    """Over shapes nobody hand-picked: the walk covers every row exactly once,
    every page is bounded, and no page hands a reader the end of a message.

    A transcript whose turns and messages all fit inside the read's budget has
    NO cut page at all — which is what makes "opens on a prompt unless cut"
    worth asserting: without the re-anchor most pages open on the box's echo
    of the person's message and say cut, and the claim passes while the page
    is wrong. When one answer IS longer than the budget, only the pages that
    genuinely carry its middle may say so.
    """
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    specs = _generated_transcript(seed, giants=giants)
    spans = await _script(real_session, chat, specs)
    trapped = [
        index + 1
        for index, spec in enumerate(specs)
        if spec["kind"] == "prompt" and _split_by(spans, index + 1)
    ]
    assert trapped, "the corpus must trap the boundary at least once"

    pages = await _walk_pages(real_session, chat.id, limit=LIMIT)

    seen = [row.seq for page, _cut, _older in reversed(pages) for row in page]
    assert seen == list(range(1, len(specs) + 1)), "every row exactly once, in order"
    budget = LIMIT * (settings.chat_page_turn_reach + settings.chat_page_message_reach)
    before = len(specs) + 1
    for page, cut, has_older in pages:
        assert len(page) <= LIMIT + budget, f"page at {page[0].seq} is unbounded"
        # How far the descent for THIS page was allowed to go.
        floor = max(before - LIMIT - budget, 1)
        before = page[0].seq
        if not has_older:
            continue
        if not cut:
            assert page[0].kind == "prompt", f"page at {page[0].seq} opens mid-turn"
            assert _split_by(spans, page[0].seq) == [], f"page at {page[0].seq} splits a message"
        elif not giants:
            pytest.fail(f"page at {page[0].seq} is cut though every turn fits the budget")
        else:
            # A cut page is carrying something it cannot open, or it ran so far
            # down that it could not look a whole message under itself. Say
            # which; "cut" is never a shrug.
            near_floor = page[0].seq - floor < LIMIT * settings.chat_page_message_reach
            assert _split_by(spans, page[0].seq) or page[0].kind != "prompt" or near_floor, (
                f"page at {page[0].seq} is cut with a clean boundary, floor {floor}"
            )


# ---------------------------------------------------------------------------
# The transcript the bug was reported on
# ---------------------------------------------------------------------------

#: A chat as it was actually recorded: 393 rows, kinds, roles, event ids and
#: the message id on every row that states one, with long text trimmed. The
#: reader sent a prompt (row 170) while the stopped answer was still writing
#: its last rows, so that answer spans 157-174 with the prompt inside it.
_RECORDED = Path(__file__).parent / "fixtures" / "chat" / "paging_transcript.json"


async def _replay(db: AsyncSession, chat: WorkspaceObject) -> list[dict[str, Any]]:
    """Write the recorded transcript into this chat, sequences unchanged."""
    recorded: list[dict[str, Any]] = json.loads(_RECORDED.read_text())
    rows = [
        {
            "id": uuid4(),
            "chat_id": chat.id,
            "org_team_id": chat.org_team_id,
            "seq": row["seq"],
            "role": row["role"],
            "kind": row["kind"],
            "event_id": row["event_id"],
            "payload": row["payload"],
        }
        for row in recorded
    ]
    for offset in range(0, len(rows), 200):
        await db.execute(pg_insert(ChatMessage).values(rows[offset : offset + 200]))
    await db.commit()
    return recorded


async def test_the_sql_and_python_readings_of_a_row_s_message_agree(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The descent looks over every row inside its budget without SHOWING any
    of them, so it reads each row's message as a COLUMN rather than hydrating
    a thousand transcript entries. That is a second spelling of one rule, and
    two spellings drift: here both are run over the transcript the bug was
    reported on — every envelope shape the machine really writes, including
    the ``part.created`` that states its message on the part and the
    ``prompt.cancelled`` that names a prompt instead of a message."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    recorded = await _replay(real_session, chat)

    in_sql = (
        await real_session.execute(
            select(ChatMessage.seq, chat_service.message_id_column())
            .where(ChatMessage.chat_id == chat.id)
            .order_by(ChatMessage.seq)
        )
    ).all()

    in_python = [
        (row["seq"], chat_service.message_id_for_event(row["payload"])) for row in recorded
    ]
    assert [(seq, name) for seq, name in in_sql] == in_python
    named = [name for _seq, name in in_python if name is not None]
    assert len(named) > 200 and len(set(named)) > 10, "the corpus really exercises both"
    assert any(row["kind"] == "part.created" for row in recorded)
    assert any(row["kind"] == "prompt.cancelled" for row in recorded)


async def test_the_recorded_transcript_opens_on_a_whole_turn(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The reported chat, replayed: a fresh open reads the tail page and must
    get the stopped answer's thinking part — rows 163-174 arrive together, not
    from 171 onwards with the message they belong to left below the page — and
    it must open on the prompt row that turn began at (151), not on the box's
    echo of that prompt (152), which a page-first reader would then date and
    anchor its first turn by."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    recorded = await _replay(real_session, chat)
    spans: dict[str, tuple[int, int]] = {}
    for row in recorded:
        name = _message_of(row["payload"])
        if name is None:
            continue
        low, high = spans.get(name, (row["seq"], row["seq"]))
        spans[name] = (min(low, row["seq"]), max(high, row["seq"]))
    assert spans["msg_0c521b5d8001n8EMpjkyvlUGS3"] == (157, 174)
    assert _split_by(spans, 170) == ["msg_0c521b5d8001n8EMpjkyvlUGS3"], "the reported cut"
    assert spans["msg_0c521b5cc001jEwhM52Z2yzLEU"] == (152, 160), "the echo, one row under 151"
    assert [row["kind"] for row in recorded if row["seq"] == 151] == ["prompt"]

    pages = await _walk_pages(real_session, chat.id, limit=200)

    tail, cut, has_older = pages[0]
    seqs = [row.seq for row in tail]
    assert set(range(157, 175)) <= set(seqs), "the stopped answer arrives whole"
    assert (tail[0].seq, tail[0].kind) == (151, "prompt"), "the page opens on the turn's prompt"
    assert (cut, has_older) == (False, True)
    seen = [row.seq for page, _cut, _older in reversed(pages) for row in page]
    assert seen == [row["seq"] for row in recorded], "the walk is still a partition"
    for page, page_cut, older in pages:
        assert _split_by(spans, page[0].seq) == [], f"page at {page[0].seq} opens inside a message"
        if older:
            assert not page_cut and page[0].kind == "prompt", (
                f"page at {page[0].seq} opens mid-turn"
            )


async def test_the_live_tail_continues_forward_from_the_newest_page(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The newest page's highest sequence is the forward cursor; rows appended
    after it page forward with no overlap and no gap."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    await _fill(real_session, chat, turns=[5, 5])
    tail, _, _, _ = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )
    cursor = tail[-1].seq
    assert cursor == 12

    await _fill(real_session, chat, turns=[2], start_seq=13)
    rows, next_after, resync = await chat_service.list_messages(
        real_session, chat_id=chat.id, after_seq=cursor, limit=LIMIT
    )
    assert [row.seq for row in rows] == [13, 14, 15]
    assert (next_after, resync) == (15, None)


async def test_a_walk_upward_is_unmoved_by_rows_arriving_at_the_tail(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The cursor is a SEQUENCE, not an offset, so the machine writing into the
    chat while the reader scrolls up cannot shift the pages under them.

    An offset-based page would: every turn appended below pushes the window one
    page further from where the reader is, which reads on screen as a duplicated
    page or a swallowed one. Here a turn is appended between every two pages of
    the walk, and the walk still covers the rows it started with exactly once —
    and never sees a row written after it began, because each of those sits
    above the tail it opened on.
    """
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    await _fill(real_session, chat, turns=[LIMIT // 4] * 12)
    original = (
        (
            await real_session.execute(
                select(ChatMessage.seq)
                .where(ChatMessage.chat_id == chat.id)
                .order_by(ChatMessage.seq)
            )
        )
        .scalars()
        .all()
    )
    highest = original[-1]

    seen: list[int] = []
    rows, prev_before, has_older, _cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )
    next_seq = highest + 1
    while True:
        seen = [row.seq for row in rows] + seen
        if not has_older:
            break
        assert prev_before is not None
        # The machine answers a turn while the reader is still reading upward.
        await _fill(real_session, chat, turns=[3], start_seq=next_seq)
        next_seq += 4
        rows, prev_before, has_older, _cut = await chat_service.list_messages_before(
            real_session, chat_id=chat.id, before=prev_before, limit=LIMIT
        )

    assert seen == list(original), "the walk covers what it started with, exactly once"
    assert max(seen) == highest, "nothing written after the walk began came back in it"
    # And what DID arrive is reachable the way the live tail reaches it.
    appended, _, _ = await chat_service.list_messages(
        real_session, chat_id=chat.id, after_seq=highest, limit=500
    )
    assert [row.seq for row in appended] == list(range(highest + 1, next_seq))


@pytest.mark.parametrize(
    ("before", "expected"),
    [
        pytest.param(1, ([], None, False), id="below-the-oldest-row"),
        pytest.param(None, ([], None, False), id="tail-of-an-empty-chat"),
    ],
)
async def test_a_page_with_nothing_below_it(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    before: int | None,
    expected: tuple[list[Any], int | None, bool],
) -> None:
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    if before is not None:
        await _fill(real_session, chat, turns=[1])
    rows, prev_before, has_older, _cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=before, limit=LIMIT
    )
    assert (rows, prev_before, has_older) == expected


async def test_the_tail_of_a_short_chat_is_the_whole_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    await _fill(real_session, chat, turns=[2, 3])
    rows, prev_before, has_older, _cut = await chat_service.list_messages_before(
        real_session, chat_id=chat.id, before=None, limit=LIMIT
    )
    assert [row.seq for row in rows] == list(range(1, 8))
    assert (prev_before, has_older) == (1, False)


# ---------------------------------------------------------------------------
# The realtime window says where its entries sit
# ---------------------------------------------------------------------------


def _op(intent: str, **kwargs: Any) -> OpPayload:
    return OpPayload(op_id=kwargs.pop("op_id", f"op-{uuid4().hex[:8]}"), intent=intent, **kwargs)  # type: ignore[arg-type]


async def _grant_for(db: AsyncSession, user: User, channel: Channel) -> ChannelGrant:
    return await channel_service.authorize(
        db, user, channel, ent=await load_entitlements(db, user, org_id=user.home_org_team_id)
    )


async def test_the_window_stamps_each_retained_entry_with_its_transcript_sequence(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A snapshot is a suffix of the transcript, and a reader holding only the
    newest page needs to know which of its entries sit below that page. The
    sequence is the row's — the same number the REST page carries."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    events = [{"event_id": f"e{index}", "event_type": "message.completed"} for index in (1, 2, 3)]
    for event in events:
        await registry.apply_op(
            real_session,
            grant=grant,
            user=admin,
            envelope=DocEnvelope(
                doc_id=channel.doc_id,
                doc_type=channel.doc_type,
                epoch=1,
                peer_id="p:pub",
                seq=0,
                kind="op",
                payload=_op("append", events=[event]).model_dump(mode="json"),
            ),
            actor=actor_for_user(admin, org_id=admin.home_org_team_id),
            ent=await load_entitlements(real_session, admin, org_id=admin.home_org_team_id),
        )
        await real_session.commit()
    doc = await registry.ensure(
        real_session,
        grant=grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await load_entitlements(real_session, admin, org_id=admin.home_org_team_id),
    )
    await real_session.commit()

    async with AsyncSessionLocal() as db:
        rows = (
            (await db.execute(select(ChatMessage).where(ChatMessage.chat_id == chat.id)))
            .scalars()
            .all()
        )
    by_event = {row.event_id: row.seq for row in rows}
    assert by_event == {"e1": 1, "e2": 2, "e3": 3}
    assert [(entry["event_id"], entry["seq"]) for entry in doc.state["events"]] == [
        ("e1", 1),
        ("e2", 2),
        ("e3", 3),
    ]


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


async def _create_chat(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/api/v1/chats", json={"title": "Paged", "client_id": uuid4().hex})
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def test_the_route_serves_the_tail_and_the_page_below_it(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    assert row is not None
    await _fill(real_session, row, turns=[1, 1, 1])  # 6 rows, turns start at 1, 3, 5

    tail = await client.get(
        f"/api/v1/chats/{chat['id']}/messages", params={"tail": True, "limit": 2}
    )
    assert tail.status_code == 200, tail.text
    body = tail.json()
    assert [item["seq"] for item in body["items"]] == [5, 6]
    assert (body["prev_before"], body["has_older"], body["next_after_seq"]) == (5, True, 6)
    assert body["resync_from"] is None

    below = await client.get(
        f"/api/v1/chats/{chat['id']}/messages", params={"before": 5, "limit": 3}
    )
    assert below.status_code == 200, below.text
    body = below.json()
    # Three rows would start at 2; the turn start at 1 is within reach.
    assert [item["seq"] for item in body["items"]] == [1, 2, 3, 4]
    assert (body["prev_before"], body["has_older"]) == (1, False)

    forward = await client.get(f"/api/v1/chats/{chat['id']}/messages", params={"after_seq": 4})
    assert forward.status_code == 200, forward.text
    body = forward.json()
    assert [item["seq"] for item in body["items"]] == [5, 6]
    assert body["has_older"] is False and body["prev_before"] is None


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"tail": True, "before": 3}, id="tail-and-before"),
        pytest.param({"after_seq": 1, "before": 3}, id="after-and-before"),
        pytest.param({"after_seq": 1, "tail": True}, id="after-and-tail"),
        pytest.param({"before": 0}, id="before-below-the-first-sequence"),
        pytest.param({"before": 2**31}, id="before-past-the-32-bit-sequence"),
        pytest.param({"before": 2**63}, id="before-past-a-64-bit-integer"),
        pytest.param({"after_seq": 2**31}, id="after-past-the-32-bit-sequence"),
    ],
)
async def test_a_page_asked_for_in_two_directions_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, params: dict[str, Any]
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    response = await client.get(f"/api/v1/chats/{chat['id']}/messages", params=params)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"before": 2**31 - 1}, id="before-at-the-largest-sequence"),
        pytest.param({"after_seq": 2**31 - 1}, id="after-at-the-largest-sequence"),
    ],
)
async def test_the_largest_sequence_is_still_a_page(
    client: AsyncClient, org_admin: OrgWithAdmin, params: dict[str, Any]
) -> None:
    """The boundary's other side: the bound refuses what the column cannot
    hold, not the column's own largest value."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    response = await client.get(f"/api/v1/chats/{chat['id']}/messages", params=params)
    assert response.status_code == 200, response.text


async def test_a_backward_page_is_the_same_opaque_404_for_a_stranger(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    async with app_client() as other:
        await login(other, member.email, password)
        tail = await other.get(f"/api/v1/chats/{chat['id']}/messages", params={"tail": True})
        below = await other.get(f"/api/v1/chats/{chat['id']}/messages", params={"before": 5})
    assert tail.status_code == 404 and below.status_code == 404
    assert tail.json()["error"]["message"] == "Not found"


# ---------------------------------------------------------------------------
# The readers that are NOT pages
# ---------------------------------------------------------------------------


async def test_the_template_digest_reads_the_whole_transcript_not_a_page_of_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Saving a chat as a template reads every row, so no page rule applies.

    The backward page exists because a reader is shown a WINDOW: rows below it
    are not on screen, so a window opening inside a message hands the reader
    parts with no message to hang them on. This read has no window — it takes
    the transcript entire — which is the other way to hold a message whole, and
    the only thing that could quietly turn it into a window is a ``limit``
    somebody adds for speed on a long chat. So the seam is asserted where it
    shows: the sequence the template records as where it was cut from is the
    chat's LAST row, on a transcript far longer than any page.
    """
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    recorded = await _replay(real_session, chat)

    _brief, last_seq = await chat_template_service.transcript_brief(real_session, chat)

    assert last_seq == max(row["seq"] for row in recorded)
    assert last_seq == len(recorded)
