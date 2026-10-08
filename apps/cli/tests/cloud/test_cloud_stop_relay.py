"""Stop, from the chat's channel to the harness that is mid-turn.

A reader presses Stop in the browser; the route puts a ``stop`` relay on the
chat's document and the box running the chat has to end the turn it is in. The
hop covered here is the last one — the relay arriving at a mirror — and it
matters because the failure is silent in both directions: a stop that never
reaches the adapter leaves the reader watching a turn they thought they ended,
and a stop applied by a box that is NOT running the chat would cancel somebody
else's turn from a relay it should have ignored.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory

pytestmark = pytest.mark.asyncio

CHAT_ID = "1f2e3d4c-5b6a-4798-8899-aabbccddeeff"
OWNER = "11111111-2222-4333-8444-555555555555"

#: The relay the route mints, exactly as ``StopRelay`` serialises one.
STOP = {"schema_version": "1.0.0", "metadata": {}, "kind": "stop", "user_id": OWNER}


def _mirror(tmp_path: Path) -> tuple[ChatMirror, HarnessRuntime, FakeAdapterFactory]:
    project = ProjectDirectory(tmp_path / ".alkera")
    project.chats().create(session_id=CHAT_ID, title="ops").close()
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(project, adapter_factory=factory)
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://stop.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
    )
    mirror._ready.set()
    return mirror, runtime, factory


async def test_a_relayed_stop_ends_the_turn_the_harness_is_running(tmp_path: Path) -> None:
    """The reader's Stop reaches the harness through the ordinary relay
    dispatch — not a handler a test calls directly — and the adapter running
    the turn is told to abort it."""
    mirror, runtime, factory = _mirror(tmp_path)
    session = await runtime.open_chat(CHAT_ID)
    try:
        mirror._session = session
        adapter = factory.adapters[-1]
        await session.send_prompt("count the mentions")
        assert adapter.cancel_count == 0, "nothing has stopped the turn yet"

        await mirror._handle_relay(dict(STOP))

        assert adapter.cancel_count == 1
    finally:
        await session.close()


async def test_a_box_that_is_not_running_the_chat_ignores_the_stop(tmp_path: Path) -> None:
    """The same rule a mode switch follows: a relay reaches every box
    subscribed to the chat, and one that holds no session for it has no turn to
    end. Acting on it anyway would abort whatever else that box was doing."""
    mirror, runtime, factory = _mirror(tmp_path)
    session = await runtime.open_chat(CHAT_ID)
    try:
        adapter = factory.adapters[-1]
        await session.send_prompt("count the mentions")
        assert mirror._session is None, "this mirror never opened the chat"

        await mirror._handle_relay(dict(STOP))

        assert adapter.cancel_count == 0, "a turn this box does not hold is left alone"
    finally:
        await session.close()


async def test_a_stop_is_not_read_as_a_prompt_to_run(tmp_path: Path) -> None:
    """The relay union routes on the wire kind. A stop that fell through to the
    prompt lane would be queued as a turn — the box would answer the word
    "stop" instead of ending the turn the reader wanted stopped."""
    mirror, runtime, _factory = _mirror(tmp_path)
    session = await runtime.open_chat(CHAT_ID)
    try:
        mirror._session = session

        await mirror._handle_relay(dict(STOP))

        assert mirror._prompts.empty(), "a stop never queues a question"
    finally:
        await session.close()


@pytest.mark.parametrize(
    "relay",
    [
        pytest.param({"kind": "stop"}, id="a stop naming nobody"),
        pytest.param({"kind": "stop", "user_id": 7}, id="a user id of the wrong type"),
    ],
)
async def test_a_stop_is_honoured_whatever_it_says_about_who_sent_it(
    tmp_path: Path, relay: dict[str, object]
) -> None:
    """Who pressed Stop is recorded by the server, not decided here, and the
    box needs none of it to end a turn. A stop refused over an unreadable
    attribution would leave the turn running — the failure the reader cannot
    work around."""
    mirror, runtime, factory = _mirror(tmp_path)
    session = await runtime.open_chat(CHAT_ID)
    try:
        mirror._session = session
        adapter = factory.adapters[-1]
        await session.send_prompt("count the mentions")

        await mirror._handle_relay(dict(relay))

        assert adapter.cancel_count == 1
    finally:
        await session.close()
