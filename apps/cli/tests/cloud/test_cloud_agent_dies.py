"""An agent that dies under a turn ends that turn with the reason, and the
NEXT message runs on a fresh agent.

The defect: a chat's agent was killed for memory; the turn ended saying the
workspace had stopped answering, and the next message in the chat was never
answered. Every later message was handed to the session whose agent was gone,
and the adapter refused each one. Here the dead agent is the fake's, the mirror
is real, the runtime and the chat store are real, and the second adapter the
factory builds is the proof that a fresh agent — not the dead one — answered.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.cloud.publish import MEMORY_LIMIT_COPY
from alkera_cli.harness import HarnessRuntime, PermissionBroker, gateway_session
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.gateway_session import default_gateway_config_builder
from alkera_cli.harness.sandbox import memory_limit_detail
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.objects import PromptRelay

pytestmark = pytest.mark.asyncio

CHAT_ID = "chat-agent-dies"
MACHINE_ID = "machine-dies-1"
OWNER = "00000000-0000-4000-8000-000000000002"
MODEL = {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5", "efforts": ["high"]}
MINT_PATH = f"/api/v1/chats/{CHAT_ID}/gateway-token"
SETTLE_SECONDS = 20.0


def _prompt(text: str, client_id: str, *, seq: int) -> dict[str, Any]:
    return PromptRelay(
        message_id=f"row-{client_id}", seq=seq, text=text, client_id=client_id, user_id=OWNER
    ).model_dump(mode="json")


async def _settle(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + SETTLE_SECONDS
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.001)


class _Backend:
    """The mint route, counting mints, and nothing else."""

    def __init__(self) -> None:
        self.mints = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == MINT_PATH:
            self.mints += 1
            return httpx.Response(
                200,
                json={"token": f"gw-{self.mints}", "expires_at": "2030-01-01T00:00:00+00:00"},
            )
        return httpx.Response(404, json={})


async def _allow(_request: object) -> object:
    raise AssertionError("no permission is asked here")


@dataclass
class _Rig:
    mirror: ChatMirror
    factory: FakeAdapterFactory
    backend: _Backend
    rows: list[dict[str, Any]] = field(default_factory=list)

    def published(self) -> list[dict[str, Any]]:
        while not self.mirror._outbound.empty():
            entry = self.mirror._outbound.get_nowait()
            if "__meta__" not in entry:
                self.rows.append(entry)
        return self.rows

    def details(self) -> list[str]:
        return [
            str(row["payload"].get("detail"))
            for row in self.published()
            if row.get("kind") == "session.status_changed" and row["payload"].get("detail")
        ]

    def asked_of(self, adapter: FakeAdapter) -> list[str]:
        return [prompt.text for prompt in adapter.sent_prompts]

    async def relay(self, body: dict[str, Any]) -> None:
        await self.mirror._handle_relay(dict(body))


@asynccontextmanager
async def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Rig]:
    monkeypatch.setattr(
        gateway_session,
        "get_settings",
        lambda: type("S", (), {"alkera_gateway_url": "https://gw.example"})(),
    )
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / "workspace" / ".alkera"),
        adapter_factory=factory,
        gateway_config_builder=default_gateway_config_builder,
    )
    runtime.project.chats().create(
        session_id=CHAT_ID, title="t", harness_type="agent", model=dict(MODEL)
    ).close()
    backend = _Backend()
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://dies.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(backend),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
        machine_id=MACHINE_ID,
    )
    mirror._ready.set()
    session = await mirror.open_session(PermissionBroker(_allow, default_timeout_seconds=None))
    mirror._session = session
    mirror._start_pump(session)
    lane = asyncio.create_task(mirror._prompt_lane())
    try:
        yield _Rig(mirror=mirror, factory=factory, backend=backend)
    finally:
        lane.cancel()
        await asyncio.gather(lane, return_exceptions=True)
        await mirror.stop()


async def test_the_turn_ends_with_the_reason_and_the_next_message_runs_on_a_fresh_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _rig(tmp_path, monkeypatch) as rig:
        first = rig.factory.adapters[-1]
        await rig.relay(_prompt("allocate six gigabytes", "c1", seq=1))
        await _settle(lambda: rig.asked_of(first) == ["allocate six gigabytes"], "the first turn")
        assert not rig.mirror._idle.is_set(), "the turn is running"

        # The sandbox killed the agent for memory, the way the adapter reports it.
        await first.die(f"agent exited unexpectedly (rc=137): {memory_limit_detail(2048)}")
        await _settle(rig.mirror._idle.is_set, "the crash to end the turn")
        assert rig.details()[-1] == MEMORY_LIMIT_COPY.format(limit="2 GB")
        assert "stopped answering" not in " ".join(rig.details())

        # The next message: a fresh agent, on a fresh gateway token, answers it.
        await rig.relay(_prompt("what happened?", "c2", seq=2))
        await _settle(lambda: len(rig.factory.adapters) == 2, "a fresh agent to be started")
        second = rig.factory.adapters[-1]
        await _settle(lambda: rig.asked_of(second) == ["what happened?"], "the second turn")
        assert rig.asked_of(first) == ["allocate six gigabytes"], "the dead agent got nothing"
        assert first.dead and not second.dead
        assert rig.mirror._session is not None and rig.mirror.state != "failed"


async def test_a_dead_agent_with_no_message_behind_it_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing restarts an agent nobody has asked anything of: the revival is
    the next message's, not the crash's, so a chat that dies and is never
    written to again spawns nothing."""
    async with _rig(tmp_path, monkeypatch) as rig:
        first = rig.factory.adapters[-1]
        await rig.relay(_prompt("hello", "c1", seq=1))
        await _settle(lambda: rig.asked_of(first) == ["hello"], "the first turn")
        await first.die()
        await _settle(rig.mirror._idle.is_set, "the crash to end the turn")
        await asyncio.sleep(0.05)
        assert len(rig.factory.adapters) == 1
