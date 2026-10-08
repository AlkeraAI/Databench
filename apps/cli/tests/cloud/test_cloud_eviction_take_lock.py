"""A chat slept to make room is slept under its own take lock.

A full box makes a slot for a new chat by sleeping its least recently used
idle one: the session goes, the folder is pushed and released, the chat is
reported asleep. A message for that chat arriving while its folder is still
being pushed must wait for the sleep to finish and then wake it. Taken beside
the sleep, the chat was served again on a folder the sleep then handed back,
and the sleep's ``asleep`` landed after the wake's ``publishing``: the reader
saw a chat asleep that was running on a box without its lease.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from _mirror_service import build_service, unreachable
from alkera_cli.cloud.rest import CloudRestClient

MACHINE = "m-1"


def _monotonic() -> float:
    return time.monotonic()


class _Custody:
    """The folder custody, whose hand-back of ``slow`` waits for the test."""

    def __init__(self, root: Path, *, slow: str) -> None:
        self.root = root
        self.slow = slow
        self.held_keys: set[str] = set()
        self.pushing = threading.Event()
        self.go_on = threading.Event()

    @property
    def enabled(self) -> bool:
        return True

    def take(self, chat_id: str, chat: Mapping[str, Any], *, instance: str) -> Any:
        self.held_keys.add(chat_id)
        return object()

    def held(self, chat_id: str) -> Any:
        return object() if chat_id in self.held_keys else None

    def local_root(self, chat_id: str) -> Path:
        return self.root / chat_id

    def live(self, chat_id: str, working_dir: Path) -> Any:
        return None

    def push(self, chat_id: str) -> Any:
        return None

    def owed(self) -> list[str]:
        return []

    def release(self, chat_id: str) -> bool:
        self.held_keys.discard(chat_id)
        return True

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        if chat_id == self.slow:
            self.pushing.set()
            assert self.go_on.wait(30), "the test never let the push finish"
        self.held_keys.discard(chat_id)


class _Reports(CloudRestClient):
    def __init__(self) -> None:
        super().__init__(
            api_url="http://127.0.0.1:1",
            token="t",
            agent_id="machine:x",
            transport=httpx.MockTransport(unreachable),
        )
        self.said: list[tuple[str, str]] = []

    def for_agent(self, agent_id: str) -> CloudRestClient:
        return self

    async def report_publisher_state(self, chat_id: str, *, state: str, **_: Any) -> dict[str, Any]:
        self.said.append((chat_id, state))
        return {}


def _row(chat_id: str, **extra: Any) -> dict[str, Any]:
    return {"id": chat_id, "machine_id": MACHINE, **extra}


async def _until(condition: Any) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


async def test_a_message_for_a_chat_being_slept_for_a_slot_wakes_it_after_the_sleep(
    tmp_path: Path,
) -> None:
    custody = _Custody(tmp_path / "folders", slow="victim")
    rest = _Reports()
    service, built = build_service(
        tmp_path, clock=_monotonic, folders=custody, rest=rest, max_mirrors=2
    )
    service._machine_id = MACHINE
    await service._take("victim", _row("victim"), start_owed_turn=False)
    await service._take("other", _row("other"), start_owed_turn=False)

    newcomer = asyncio.create_task(service._take("new", _row("new"), start_owed_turn=False))
    try:
        await _until(custody.pushing.is_set)
        message = asyncio.create_task(
            service._take("victim", _row("victim", last_seq=7), start_owed_turn=False)
        )
        for _ in range(20):
            await asyncio.sleep(0.01)
    finally:
        custody.go_on.set()
    await asyncio.wait_for(asyncio.gather(newcomer, message), 30)

    victim = [state for chat, state in rest.said if chat == "victim"]
    assert victim[-1] == "publishing", f"the victim's last word was {victim}"
    assert "victim" in service._mirrors and built["victim"].state == "running"
    assert custody.held("victim") is not None, "a running chat holds its folder"
