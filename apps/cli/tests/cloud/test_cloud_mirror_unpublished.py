"""A chat with an event the server has not yet taken is not idle.

On a real box a draining worker handed a chat back twelve seconds after its
turn ended, while the turn's last events (its completion, the session going
idle) still sat in the mirror's queue behind a slow send; stopping the mirror
dropped them, and the server was left with a chat that owed a turn it had in
fact answered."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import httpx
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory


class SlowDoc:
    """The chat's document on a socket that answers when told to."""

    def __init__(self) -> None:
        self.answer = asyncio.Event()
        self.sent: list[str] = []

    async def send_op(self, kind: str, **_fields: Any) -> None:
        await self.answer.wait()
        self.sent.append(kind)


def _mirror(tmp_path: Path) -> ChatMirror:
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    return ChatMirror(
        chat_id="chat-a",
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://objects.test",
            token="t",
            agent_id="chat-a",
            transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={})),
        ),
    )


async def test_a_chat_is_busy_until_the_server_has_taken_every_event(tmp_path: Path) -> None:
    mirror = _mirror(tmp_path)
    doc = SlowDoc()
    mirror._doc = doc  # type: ignore[assignment]
    assert mirror.quiescent is True

    mirror._queue_meta({"phase": "idle"})
    assert mirror.quiescent is False, "an event still queued for the server"

    publisher = asyncio.create_task(mirror._publisher())
    for _ in range(5):
        await asyncio.sleep(0)
    assert doc.sent == []
    assert mirror.quiescent is False, "an event the server has not answered for"

    doc.answer.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert doc.sent == ["set_meta"]
    assert mirror.quiescent is True

    publisher.cancel()
    await asyncio.gather(publisher, return_exceptions=True)
