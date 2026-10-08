"""A chat the server ended under the box is dropped, without a push.

The server ends a chat's service in one transition (``chat_end``): the folder's
lease is released on the spot and the chat's ``end_seq`` moves. A box that reads
the row — its discovery pass, or a frame — and finds the counter past the one it
took the chat at lets the chat go: the session stops, the folder is forgotten
without a push (its lease is gone, so the push would only be refused), and the
chat is not taken straight back on the next pass. A box that put the chat to
sleep itself reports why, so the server records the same ending.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _mirror_service import Clock, build_service

MACHINE = "machine:x"
CHAT = "chat-live"


class Folders:
    """Custody that records what the service asked of it."""

    def __init__(self) -> None:
        self.enabled = True
        self.takes: list[str] = []
        self.hand_backs: list[tuple[str, str | None]] = []
        self.let_go_of: list[str] = []
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
        self.hand_backs.append((chat_id, ending))
        self._held.pop(chat_id, None)
        return None

    def let_go(self, chat_id: str) -> None:
        self.let_go_of.append(chat_id)
        self._held.pop(chat_id, None)

    def release(self, chat_id: str) -> bool:
        return self._held.pop(chat_id, None) is not None


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.folders = Folders()
        self.row: dict[str, Any] = {
            "id": CHAT,
            "machine_id": MACHINE,
            "last_seq": 3,
            "machine_status": "ready",
            "end_seq": 2,
        }
        self.reports: list[tuple[str, str, str | None]] = []
        self.service, _built = build_service(tmp_path, clock=Clock(), folders=self.folders)
        self.service._machine_id = MACHINE
        self.service._rest.list_chats = self.list_chats  # type: ignore[method-assign]
        self.service._rest.get_chat = self.get_chat  # type: ignore[method-assign]
        self.service._rest.report_publisher_state = self.report  # type: ignore[method-assign]

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": [self.row], "next_cursor": None}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        return self.row

    async def report(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
    ) -> None:
        self.reports.append((chat_id, state, ending))

    async def pass_once(self) -> None:
        await self.service.sync_once()
        await self.service.settle_background()


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


async def test_a_chat_ended_by_the_server_is_dropped_without_a_push(rig: Rig) -> None:
    await rig.pass_once()
    assert CHAT in rig.service.mirrors

    rig.row = {**rig.row, "end_seq": 3, "mirror_state": "asleep", "ended_reason": "deleted"}
    await rig.pass_once()

    assert CHAT not in rig.service.mirrors
    assert rig.folders.let_go_of == [CHAT]
    assert rig.folders.hand_backs == [], "the folder was pushed after the server ended it"
    # And it is not taken straight back: nothing new has come for it.
    await rig.pass_once()
    assert CHAT not in rig.service.mirrors
    assert rig.folders.takes == [CHAT]


async def test_a_chat_whose_counter_has_not_moved_stays_served(rig: Rig) -> None:
    """The asymmetric half: the counter the box took the chat at is not an
    ending, however high it is."""
    await rig.pass_once()
    await rig.pass_once()

    assert CHAT in rig.service.mirrors
    assert rig.folders.let_go_of == []


@pytest.mark.parametrize(
    ("sleep", "ending"),
    [
        pytest.param("idle", "idle", id="idle"),
        pytest.param("evicted", "evicted", id="evicted"),
    ],
)
async def test_the_box_says_why_it_put_a_chat_to_sleep(rig: Rig, sleep: str, ending: str) -> None:
    """Both halves of a box's own sleep carry the reason: the hand-back (whose
    release is the server's transition) and the report that follows it."""
    await rig.pass_once()

    await rig.service._release_mirror(CHAT, ending=sleep)

    assert rig.folders.hand_backs == [(CHAT, ending)]
    assert (CHAT, "asleep", ending) in rig.reports
