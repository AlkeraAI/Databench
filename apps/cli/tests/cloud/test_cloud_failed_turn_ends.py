"""A turn the box began always ends, even when the message never reached the
agent.

The mirror stamps a turn ``working`` the moment it starts handing a message to
the harness. Composing that hand-over waits on several things (the turn's
briefs, the tool view, the agent itself), and any of them can raise. When one
did, the lane logged "a queued question failed" and moved on, but nothing
stamped the turn ``idle``: every reader saw "Working" for good, the next
message waited behind a turn no agent was running, and a supervised restart
held the chat for its whole ceiling because it still counted as working.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.activity import ChatActivity
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.harness import HarnessRuntime, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.gateway_session import default_gateway_config_builder
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PromptCancelled, SessionStatusChanged
from alkera_core.schemas.chat.events_signals import PROMPT_CANCELLED_FAILED
from alkera_core.schemas.objects import PromptRelay

pytestmark = pytest.mark.asyncio

CHAT_ID = "5c1d2e3f-4a5b-4c6d-8e7f-8091a2b3c4d5"
MACHINE_ID = "machine-failed-turn"
OWNER = "44444444-5555-4666-8777-888888888888"
MODEL = {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5", "efforts": ["high"]}
SETTLE_SECONDS = 5.0


def _nothing_else(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={})


async def _settle(predicate: Callable[[], bool], what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_SECONDS
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def _allow(_request: object) -> object:
    raise AssertionError("no permission is asked here")


@dataclass
class _Rig:
    mirror: ChatMirror
    factory: FakeAdapterFactory
    seq: int = 0

    @property
    def adapter(self) -> FakeAdapter:
        return self.factory.adapters[-1]

    def asked(self) -> list[str]:
        return [prompt.text for prompt in self.adapter.sent_prompts]

    async def send(self, text: str) -> None:
        self.seq += 1
        relay = PromptRelay(
            message_id=f"row-{self.seq}",
            seq=self.seq,
            text=text,
            client_id=f"c{self.seq}",
            user_id=OWNER,
        )
        await self.mirror._handle_relay(relay.model_dump(mode="json"))

    def cancelled(self) -> list[str]:
        session = self.mirror.session
        assert session is not None
        return [e.reason for e in session.events() if isinstance(e, PromptCancelled)]

    def published_turn_states(self) -> list[str]:
        """Every turn state the mirror put on its way to the chat's document,
        in order: what every reader, live or reloading, reads the turn from."""
        states: list[str] = []
        queue = cast("asyncio.Queue[dict[str, Any]]", self.mirror._outbound)
        while not queue.empty():
            entry = queue.get_nowait()
            meta = entry.get("__meta__")
            if isinstance(meta, dict):
                states.append(str(meta["turn_state"]["state"]))
        return states


@asynccontextmanager
async def _rig(tmp_path: Path) -> AsyncIterator[_Rig]:
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / "workspace" / ".alkera"),
        adapter_factory=factory,
        gateway_config_builder=default_gateway_config_builder,
    )
    runtime.project.chats().create(
        session_id=CHAT_ID, title="t", harness_type="agent", model=dict(MODEL)
    ).close()
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://objects.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(_nothing_else),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
        machine_id=MACHINE_ID,
    )
    session = await mirror.open_session(PermissionBroker(_allow, default_timeout_seconds=None))
    mirror._session = session
    mirror._ready.set()
    mirror._start_pump(session)
    lane = asyncio.create_task(mirror._prompt_lane())
    try:
        yield _Rig(mirror=mirror, factory=factory)
    finally:
        lane.cancel()
        await asyncio.gather(lane, return_exceptions=True)
        await mirror.stop()


async def test_a_message_that_fails_after_its_turn_began_ends_the_turn(tmp_path: Path) -> None:
    async with _rig(tmp_path) as rig:
        rig.adapter.fail_next_prompt = True

        await rig.send("first")

        await _settle(lambda: rig.cancelled() == [PROMPT_CANCELLED_FAILED], "the failure note")
        await _settle(lambda: rig.mirror._idle.is_set(), "the turn to end")
        # Working, then idle: a reader live on the chat and one who reloads it
        # both read the turn as over.
        assert rig.published_turn_states() == ["working", "idle"]
        # And a stopping box no longer waits on it.
        assert rig.mirror.activity is ChatActivity.IDLE
        assert rig.asked() == []


async def test_the_next_message_runs_after_a_failed_one(tmp_path: Path) -> None:
    async with _rig(tmp_path) as rig:
        rig.adapter.fail_next_prompt = True
        await rig.send("first")
        await _settle(lambda: rig.cancelled() == [PROMPT_CANCELLED_FAILED], "the failure note")

        await rig.send("second")

        await _settle(lambda: rig.asked() == ["second"], "the second message to reach the agent")
        await rig.adapter.feed(
            SessionStatusChanged(
                event_id="end-2",
                time=datetime.now(UTC),
                session_id=CHAT_ID,
                status="idle",
                phase="idle",
            )
        )
        await _settle(lambda: rig.mirror._idle.is_set(), "the second turn to end")
        assert rig.cancelled() == [PROMPT_CANCELLED_FAILED]


async def test_a_message_refused_before_its_turn_began_starts_no_turn(tmp_path: Path) -> None:
    """The other side of the rule: a message that failed while it was still
    held never stamped ``working``, so nothing is ended for it."""
    async with _rig(tmp_path) as rig:

        async def broken(_chat_id: str, _relay: Any) -> None:
            raise RuntimeError("the files the message names could not be read")

        rig.mirror._prepare_attachments = broken  # type: ignore[assignment]
        await rig.send("first")

        await _settle(lambda: rig.cancelled() == [PROMPT_CANCELLED_FAILED], "the failure note")
        assert rig.published_turn_states() == []
        assert rig.mirror._idle.is_set()
