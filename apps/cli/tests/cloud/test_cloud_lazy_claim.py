"""A box takes a chat's folder when the chat is used, not when the box claims it.

A node that came up over the chats two orgs had stranded on earlier boxes took
every one of their folders the moment it claimed them — a lease, a full walk of
the drive and an agent server apiece, one take slot each — and the one chat a
reader had just written to waited its whole budget behind them. The discovery
pass now leaves a bound chat that nobody has touched for an hour and that owes
nothing: it stays bound and untaken until the frame, the message or the wake
that comes for it, exactly as a chat a box put to sleep does, and its row is
told it is asleep, once, so the reader's banner promises nothing. A chat with a
turn owed, a reader looking (the wake stamp) or activity within the hour is
taken at claim as before — a reader mid-session on a box that restarted finds
the chat live without asking — and so is a row that carries no activity stamp.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _mirror_service import SERVICE_LOGGER, Clock, FakeMirror, build_service
from alkera_cli.cloud.service import CloudMirrorService

MACHINE = "machine:x"
OWED = "chat-owed"
WATCHED = "chat-watched"
RECENT = "chat-recent"
UNSTAMPED = "chat-unstamped"
STALE = "chat-stale"
BARE = "chat-bare"
ELSEWHERE = "chat-elsewhere"
LEFT_LINE = (
    "2 bound chat(s) untouched for over an hour with nothing owed are left until a "
    "reader or a message comes for them"
)


def _ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


class CountingFolders:
    """Custody that records every take and holds nothing back."""

    def __init__(self) -> None:
        self.enabled = True
        self.takes: list[str] = []
        self._held: dict[str, Any] = {}

    def bind_machine(self, machine_id: str) -> None:
        return None

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        self.takes.append(chat_id)
        held = SimpleNamespace(record=SimpleNamespace(node_id=f"node-{chat_id}"), live=None)
        self._held[chat_id] = held
        return held

    def held(self, chat_id: str) -> Any:
        return self._held.get(chat_id)

    def live(self, chat_id: str, working_dir: Path) -> Any:
        return None

    def owed(self) -> list[str]:
        return []

    def beat(self, chat_id: str) -> bool:
        return chat_id in self._held

    def push(self, chat_id: str) -> Any:
        return None

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        self._held.pop(chat_id, None)
        return None

    def release(self, chat_id: str) -> bool:
        return self._held.pop(chat_id, None) is not None


class Rig:
    """A box over a listing the test edits, recording what it reports."""

    def __init__(self, tmp_path: Path) -> None:
        self.folders = CountingFolders()
        self.clock = Clock()
        self.rows: dict[str, dict[str, Any]] = {
            OWED: {
                "id": OWED,
                "machine_id": MACHINE,
                "last_seq": 1,
                "pending_turn": True,
                "last_activity_at": _ago(hours=5),
            },
            WATCHED: {
                "id": WATCHED,
                "machine_id": MACHINE,
                "last_seq": 1,
                "wake_requested_at": "2026-09-28T03:52:00Z",
                "last_activity_at": _ago(hours=5),
            },
            RECENT: {
                "id": RECENT,
                "machine_id": MACHINE,
                "last_seq": 3,
                "machine_status": "ready",
                "last_activity_at": _ago(minutes=5),
            },
            UNSTAMPED: {"id": UNSTAMPED, "machine_id": MACHINE, "last_seq": 1},
            STALE: {
                "id": STALE,
                "machine_id": MACHINE,
                "last_seq": 4,
                "machine_status": "ready",
                "last_activity_at": _ago(hours=2),
            },
            BARE: {
                "id": BARE,
                "machine_id": MACHINE,
                "last_seq": 2,
                "machine_status": "asleep",
                "last_activity_at": _ago(hours=3),
            },
            ELSEWHERE: {
                "id": ELSEWHERE,
                "machine_id": "machine:other",
                "last_seq": 1,
                "last_activity_at": _ago(hours=3),
            },
        }
        self.reports: list[tuple[str, str]] = []
        self.service, self.built = build_service(tmp_path, clock=self.clock, folders=self.folders)
        self.service._machine_id = MACHINE
        self.service._rest.list_chats = self.list_chats  # type: ignore[method-assign]
        self.service._rest.get_chat = self.get_chat  # type: ignore[method-assign]
        self.service._rest.report_publisher_state = self.report  # type: ignore[method-assign]

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": list(self.rows.values()), "next_cursor": None}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        return self.rows[chat_id]

    async def report(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
    ) -> None:
        self.reports.append((chat_id, state))

    async def pass_once(self) -> None:
        await self.service.sync_once()
        await self.service.settle_background()

    @property
    def mirrors(self) -> set[str]:
        return set(self.service.mirrors)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


async def test_a_pass_leaves_the_chats_untouched_for_an_hour_that_owe_nothing(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        await rig.pass_once()

    assert rig.mirrors == {OWED, WATCHED, RECENT, UNSTAMPED}, (
        "a chat untouched for hours that owes nothing was taken at claim, or one that is "
        "owed, watched, recent or unstamped was not"
    )
    assert sorted(rig.folders.takes) == sorted([OWED, WATCHED, RECENT, UNSTAMPED])
    assert rig.reports.count((STALE, "asleep")) == 1, (
        "the row that still read as live on a gone box was not told it is asleep"
    )
    assert not any(chat == BARE for chat, _state in rig.reports), (
        "a row that already read asleep was told so again"
    )
    said = [r.getMessage() for r in caplog.records if "left until a reader" in r.getMessage()]
    assert said == [LEFT_LINE]

    # The same listing again: nothing new is taken and nothing is said twice.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        await rig.pass_once()
    assert rig.mirrors == {OWED, WATCHED, RECENT, UNSTAMPED}
    assert rig.reports.count((STALE, "asleep")) == 1
    assert not [r for r in caplog.records if "left until a reader" in r.getMessage()]


async def test_a_message_or_a_wake_takes_a_chat_the_pass_left(rig: Rig) -> None:
    await rig.pass_once()
    assert BARE not in rig.mirrors and STALE not in rig.mirrors

    rig.rows[BARE]["pending_turn"] = True
    await rig.pass_once()
    assert BARE in rig.mirrors and rig.folders.takes.count(BARE) == 1

    rig.rows[STALE]["wake_requested_at"] = "2026-09-28T04:00:00Z"
    await rig.pass_once()
    assert STALE in rig.mirrors and rig.folders.takes.count(STALE) == 1
    # Served, the row is told so — after the asleep the claim earned it.
    assert [state for chat, state in rig.reports if chat == STALE] == ["asleep", "publishing"]


async def test_activity_within_the_hour_takes_a_chat_the_pass_left(rig: Rig) -> None:
    """A reader who writes into the folder (no message, no wake) moves the
    row's activity stamp; the next pass takes the chat."""
    await rig.pass_once()
    assert STALE not in rig.mirrors

    rig.rows[STALE]["last_activity_at"] = _ago(minutes=1)
    await rig.pass_once()
    assert STALE in rig.mirrors and rig.folders.takes.count(STALE) == 1


async def test_a_chat_this_box_already_serves_is_kept_when_its_row_goes_quiet(rig: Rig) -> None:
    """Laziness is about taking, never about letting go: a served chat whose
    turn was answered (the row no longer owes one, and its stamp is hours
    old) keeps its mirror and its folder, and is not put to sleep by the pass
    that finds it quiet — the idle sweep decides that, on its own clock."""
    await rig.pass_once()
    assert OWED in rig.mirrors

    del rig.rows[OWED]["pending_turn"]
    await rig.pass_once()
    assert OWED in rig.mirrors
    mirror: FakeMirror = rig.built[OWED]
    assert not mirror.stopped
    assert rig.folders.takes.count(OWED) == 1
    assert (OWED, "asleep") not in rig.reports


async def test_a_frame_for_a_chat_the_pass_left_still_takes_it(rig: Rig) -> None:
    """The pass is the only thing that is lazy. A frame is news about one
    chat — a reader did something in it — and is served as it always was,
    whatever the row's counters say."""
    await rig.pass_once()
    assert STALE not in rig.mirrors

    await rig.service.reconcile_chat(STALE)
    await rig.service.settle_background()
    assert STALE in rig.mirrors and rig.folders.takes.count(STALE) == 1


async def test_a_chat_bound_elsewhere_is_neither_taken_nor_reported(rig: Rig) -> None:
    rig.rows[ELSEWHERE]["machine_status"] = "ready"
    await rig.pass_once()
    assert ELSEWHERE not in rig.mirrors
    assert ELSEWHERE not in rig.folders.takes
    assert not any(chat == ELSEWHERE for chat, _state in rig.reports)


def test_the_service_keeps_its_deferrals_apart_from_its_mirrors(tmp_path: Path) -> None:
    """The record of what a pass left is the service's, per process: a chat
    served later leaves it, so the next quiet spell says asleep again."""
    service, _built = build_service(tmp_path, clock=Clock())
    assert isinstance(service, CloudMirrorService)
    assert service._deferred == set()


async def test_a_chat_a_frame_took_while_the_pass_ran_is_not_told_it_is_asleep(rig: Rig) -> None:
    """The pass decides from its listing which chats it leaves and tells their
    rows asleep only after it has gone through every row. A message sent in
    between rings the chat's frame, and the frame's take serves the chat while
    the pass is still running; the pass's word from the older listing then
    told the row asleep, and the backend ended the chat under its own turn.
    The pass's listing is stale by the time it speaks: a chat being served is
    not left."""
    await rig.service.reconcile_chat(STALE)
    await rig.service.settle_background()
    assert STALE in rig.mirrors

    await rig.service._leave_for_later([dict(rig.rows[STALE])])

    assert (STALE, "asleep") not in rig.reports
    assert STALE in rig.mirrors
