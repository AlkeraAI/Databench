"""The platform caps the channels one socket holds; a box that mirrors that
many chats meets the cap with the next one. On the demo box that read as a
verdict on the chat: "Your workspace cannot publish this chat", and the chat
was never served while thirty idle chats kept their channels. The cap is this
box's own capacity: it learns where it bit, puts the idlest chat to sleep to
make the slot, and takes the refused chat again. Nothing is put on the chat.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _mirror_service import Clock, FakeMirror, build_service
from alkera_cli.cloud.mirror import ChatMirrorRefusedError
from alkera_cli.cloud.publisher_identity import PublishingRefusal
from alkera_cli.cloud.service import CloudMirrorService

MACHINE = "machine:x"
#: The platform's own word for the cap, as the socket says it.
CHANNEL_CAP = "too_many_channels"
HELD = ("chat-a", "chat-b")
NEW = "chat-new"
BOUND = 5.0


class PlainFolders:
    """Custody that takes every folder at once and remembers what it holds."""

    def __init__(self) -> None:
        self.enabled = True
        self._held: dict[str, Any] = {}

    def bind_machine(self, machine_id: str) -> None:
        return None

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
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
    """A box serving two chats, with a third bound to it whose first start
    the platform refuses with the channel cap — once, the way the real socket
    does when the slot has since been made."""

    def __init__(self, tmp_path: Path, *, refusal: str = CHANNEL_CAP, busy: bool = False) -> None:
        self.clock = Clock()
        self.chats: list[dict[str, Any]] = [
            {"id": HELD[0], "machine_id": MACHINE, "last_seq": 1},
            {"id": HELD[1], "machine_id": MACHINE, "last_seq": 1},
        ]
        self.reported: list[tuple[str, str]] = []
        self.builds: dict[str, int] = {}
        self.service, self.built = build_service(tmp_path, clock=self.clock, folders=PlainFolders())
        self.service._machine_id = MACHINE
        self.service._rest.list_chats = self.list_chats  # type: ignore[method-assign]
        self.service._rest.get_chat = self.get_chat  # type: ignore[method-assign]
        self.service._rest.report_publisher_state = self.report  # type: ignore[method-assign]
        plain = self.service._mirror_factory

        def factory(chat_id: str, chat: dict[str, Any]) -> Any:
            mirror: FakeMirror = plain(chat_id, chat)
            self.builds[chat_id] = self.builds.get(chat_id, 0) + 1
            if chat_id == NEW and self.builds[NEW] == 1:
                mirror.fail_start = ChatMirrorRefusedError(NEW, refusal)
            if chat_id in HELD and busy:
                mirror.turn_running = True
            return mirror

        self.service._mirror_factory = factory

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": list(self.chats), "next_cursor": None}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        for chat in self.chats:
            if chat["id"] == chat_id:
                return chat
        raise AssertionError(f"no such chat {chat_id}")

    async def report(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
    ) -> None:
        self.reported.append((chat_id, state))

    def bind_new_chat(self) -> None:
        self.chats.insert(
            0, {"id": NEW, "machine_id": MACHINE, "last_seq": 1, "pending_turn": True}
        )

    def serving(self, chat_id: str) -> bool:
        mirror = self.built.get(chat_id)
        return chat_id in self.service.mirrors and mirror is not None and mirror.state == "running"

    def refused(self) -> list[str]:
        return [chat for chat, state in self.reported if state == "refused"]


async def _until(predicate: Callable[[], bool], *, within: float = BOUND) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _sync(service: CloudMirrorService) -> None:
    await asyncio.wait_for(service.sync_once(), 30.0)
    await service.settle_background()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    yield Rig(tmp_path)


async def test_the_cap_sleeps_the_idlest_chat_and_the_refused_one_is_served(rig: Rig) -> None:
    await _sync(rig.service)
    assert set(rig.service.mirrors) == set(HELD)

    rig.bind_new_chat()
    await _sync(rig.service)

    assert await _until(lambda: rig.serving(NEW)), (
        "the chat the channel cap refused was never served, though two idle chats held channels"
    )
    await rig.service.settle_background()
    asleep = [c for c in HELD if c not in rig.service.mirrors]
    assert len(asleep) == 1, f"exactly one idle chat makes the slot; asleep: {asleep}"
    assert rig.built[asleep[0]].stopped
    assert rig.builds[NEW] == 2, "the refused start was taken again"
    # Nothing was put on the chat: the reader waited for a slot, not for an admin.
    assert NEW not in rig.refused()
    assert rig.refused() == []


async def test_the_learned_cap_is_what_the_box_reports_from_then_on(rig: Rig) -> None:
    await _sync(rig.service)
    before = rig.service._capacity()
    rig.bind_new_chat()
    await _sync(rig.service)
    await _until(lambda: rig.serving(NEW))
    await rig.service.settle_background()

    assert rig.service._capacity() == len(HELD)
    assert rig.service._capacity() < before or before == len(HELD)
    assert rig.service._effective_cap() == len(HELD)


async def test_with_every_held_chat_busy_the_refused_chat_waits_and_is_served_when_one_ends(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, busy=True)
    await _sync(rig.service)
    rig.bind_new_chat()
    await _sync(rig.service)

    # No slot to make: nothing is taken from a reader whose turn is running,
    # and nothing is put on the waiting chat.
    assert set(rig.service.mirrors) == set(HELD)
    assert not rig.serving(NEW)
    assert rig.refused() == []

    rig.built[HELD[0]].end_turn(told=False)
    await _sync(rig.service)
    assert await _until(lambda: rig.serving(NEW)), "the chat was not served once a slot came free"
    await rig.service.settle_background()
    assert HELD[0] not in rig.service.mirrors and HELD[1] in rig.service.mirrors
    assert rig.refused() == []


async def test_a_refusal_that_is_a_verdict_still_reaches_the_chat(tmp_path: Path) -> None:
    # The control: a refusal that is about the chat (another box publishes it)
    # is put on the chat as before, and no slot is made for it.
    rig = Rig(tmp_path, refusal="not_publisher")
    await _sync(rig.service)
    rig.bind_new_chat()
    await _sync(rig.service)

    assert rig.refused() == [NEW]
    assert set(rig.service.mirrors) == set(HELD)
    assert rig.builds[NEW] == 1


async def test_a_cap_met_mid_turn_is_not_put_on_the_chat(rig: Rig) -> None:
    await _sync(rig.service)
    await rig.service._on_mirror_refused(NEW, PublishingRefusal(CHANNEL_CAP, "channel cap"))
    assert rig.refused() == []
    assert rig.service._effective_cap() == len(HELD)

    await rig.service._on_mirror_refused(NEW, PublishingRefusal("not_publisher", "elsewhere"))
    assert rig.refused() == [NEW]
