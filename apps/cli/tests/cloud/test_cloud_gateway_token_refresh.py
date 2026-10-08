"""A chat that stays open past its gateway token's lifetime keeps working.

The token reaches the agent only when it is spawned (opencode's inline config,
the Claude subprocess's environment), so it cannot be swapped under a running
agent. Before a turn starts, the mirror checks how long the token has left;
when it is inside the refresh window it mints a new one and opens the agent
again on it — between turns, never under one. A refused mint starts no turn on
a token that would be refused too; a mint that merely did not arrive lets the
turn run on the old token while it is still good.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.account import auth_file
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.harness import HarnessRuntime, PermissionBroker, gateway_session
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.gateway_session import default_gateway_config_builder

# Imported before any freeze: the runtime loads the SQL tools lazily, and a
# pydantic model built while ``datetime.date`` is freezegun's stand-in fails.
from alkera_cli.plugins.plugin_base import sql_tools  # noqa: F401
from alkera_core.auth import GATEWAY_TOKEN_REFRESH_BEFORE_SECONDS, GATEWAY_TOKEN_TTL_SECONDS
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PromptCancelled, SessionStatusChanged
from alkera_core.schemas.chat.events_signals import PROMPT_CANCELLED_FAILED
from alkera_core.schemas.objects import PromptRelay
from freezegun import freeze_time

pytestmark = pytest.mark.asyncio

CHAT_ID = "7a2b9c30-88d1-4e2f-8a0b-2b3c4d5e6f70"
MACHINE_ID = "machine-gw-refresh"
OWNER = "33333333-4444-4555-8666-777777777777"
MODEL = {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5", "efforts": ["high"]}
MINT_PATH = f"/api/v1/chats/{CHAT_ID}/gateway-token"
T0 = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
TTL = timedelta(seconds=GATEWAY_TOKEN_TTL_SECONDS)
WINDOW = timedelta(seconds=GATEWAY_TOKEN_REFRESH_BEFORE_SECONDS)
SETTLE_SECONDS = 20.0


@pytest.fixture(autouse=True)
def _chat_gateway_token_minted() -> None:
    """Overrides the suite's fixed-token stand-in: every mint here meets the
    scripted backend below."""


@pytest.fixture(autouse=True)
def _never_read_device_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def _never() -> None:
        raise AssertionError("the device token file must not be read for a cloud chat")

    monkeypatch.setattr(auth_file, "_read_document", _never)
    monkeypatch.setattr(
        gateway_session,
        "get_settings",
        lambda: type("S", (), {"alkera_gateway_url": "https://gw.example"})(),
    )


class _Backend:
    """The mint route. Each mint answers the next scripted status; a 200
    hands out ``gw-<n>`` expiring a full TTL after the (frozen) now."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = list(statuses or [])
        self.minted: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path != MINT_PATH:
            return httpx.Response(404, json={})
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            return httpx.Response(
                status, json={"detail": {"code": "refused", "message": "No session"}}
            )
        token = f"gw-{len(self.minted) + 1}"
        self.minted.append(token)
        expires = datetime.now(UTC) + TTL
        return httpx.Response(200, json={"token": token, "expires_at": expires.isoformat()})


def _prompt(text: str, seq: int) -> dict[str, Any]:
    return PromptRelay(
        message_id=f"row-{seq}", seq=seq, text=text, client_id=f"c{seq}", user_id=OWNER
    ).model_dump(mode="json")


def _turn_ended(event_id: str) -> SessionStatusChanged:
    return SessionStatusChanged(
        event_id=event_id,
        time=datetime.now(UTC),
        session_id=CHAT_ID,
        status="idle",
        phase="idle",
    )


async def _settle(predicate: Callable[[], bool], what: str) -> None:
    """Turn the loop until ``predicate`` holds. The deadline is the loop's own
    clock, which the freeze leaves running (``real_asyncio``)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_SECONDS
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.001)


def _token_of(config: Any) -> str:
    agent_config = config.harness_native["agent_config"]
    return str(agent_config["provider"]["alkera-anthropic"]["options"]["apiKey"])


async def _allow(_request: object) -> object:
    raise AssertionError("no permission is asked here")


@dataclass
class _Rig:
    mirror: ChatMirror
    runtime: HarnessRuntime
    factory: FakeAdapterFactory
    backend: _Backend
    lane: asyncio.Task[None]
    seq: int = 0
    turns: list[str] = field(default_factory=list)

    def asked(self, adapter: int) -> list[str]:
        return [prompt.text for prompt in self.factory.adapters[adapter].sent_prompts]

    def tokens(self) -> list[str]:
        """The gateway token every agent the chat has spawned was handed."""
        return [_token_of(config) for config in self.factory.configs]

    async def send(self, text: str) -> None:
        self.seq += 1
        await self.mirror._handle_relay(_prompt(text, self.seq))

    async def finish_turn(self) -> None:
        """The running agent ends its turn, which is what frees the lane."""
        await self.factory.adapters[-1].feed(_turn_ended(f"end-{self.seq}"))
        await _settle(lambda: self.mirror._idle.is_set(), "the turn to end")

    def cancelled(self) -> list[str]:
        """Why each message the chat recorded as never run was not run."""
        session = self.mirror.session
        assert session is not None
        return [e.reason for e in session.events() if isinstance(e, PromptCancelled)]


@asynccontextmanager
async def _rig(tmp_path: Path, backend: _Backend) -> AsyncIterator[_Rig]:
    """A mirror over a real runtime and chat store, opened the way ``start``
    opens it (a minted token, the event pump, the prompt lane)."""
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
            transport=httpx.MockTransport(backend),
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
        yield _Rig(mirror=mirror, runtime=runtime, factory=factory, backend=backend, lane=lane)
    finally:
        lane.cancel()
        await asyncio.gather(lane, return_exceptions=True)
        await mirror.stop()


async def _first_turn(rig: _Rig) -> None:
    await rig.send("first")
    await _settle(lambda: rig.asked(0) == ["first"], "the first turn to reach the agent")
    await rig.finish_turn()


@pytest.mark.parametrize(
    ("elapsed", "renewed"),
    [
        pytest.param(TTL - WINDOW - timedelta(minutes=1), False, id="outside-the-window"),
        pytest.param(TTL - WINDOW + timedelta(minutes=1), True, id="inside-the-window"),
        pytest.param(TTL + timedelta(minutes=1), True, id="already-lapsed"),
    ],
)
async def test_the_next_turn_starts_on_a_token_with_the_window_left(
    tmp_path: Path, elapsed: timedelta, renewed: bool
) -> None:
    with freeze_time(T0, real_asyncio=True) as frozen:
        async with _rig(tmp_path, _Backend()) as rig:
            await _first_turn(rig)
            frozen.move_to(T0 + elapsed)

            await rig.send("second")
            if renewed:
                await _settle(lambda: len(rig.factory.adapters) == 2, "the agent to reopen")
                await _settle(lambda: rig.asked(1) == ["second"], "the turn on the new agent")
                assert rig.backend.minted == ["gw-1", "gw-2"]
                # The new agent holds the new token; the old one ran only the
                # turn before and was shut down, not left beside it.
                assert rig.tokens() == ["gw-1", "gw-2"]
                assert rig.asked(0) == ["first"]
                assert rig.factory.adapters[0]._stopped
                assert rig.mirror.gateway_token_expires_at == T0 + elapsed + TTL
                # What the new agent says reaches the chat: the pump follows it.
                rig.mirror._outbound = asyncio.Queue()
                await rig.finish_turn()
                assert not rig.mirror._outbound.empty()
            else:
                await _settle(lambda: rig.asked(0) == ["first", "second"], "the second turn")
                assert rig.backend.minted == ["gw-1"]
                assert rig.tokens() == ["gw-1"]
                assert rig.mirror.gateway_token_expires_at == T0 + TTL


async def test_a_turn_still_running_is_never_restarted_for_a_new_token(tmp_path: Path) -> None:
    """The renewal restarts the agent, so it waits for a turn boundary: a
    renewal asked for while a turn is running leaves that turn's agent and
    token alone."""
    with freeze_time(T0, real_asyncio=True) as frozen:
        async with _rig(tmp_path, _Backend()) as rig:
            await rig.send("first")
            await _settle(lambda: rig.asked(0) == ["first"], "the first turn")
            frozen.move_to(T0 + TTL - timedelta(minutes=5))

            await rig.mirror._refresh_gateway_token()
            assert rig.backend.minted == ["gw-1"]
            assert len(rig.factory.adapters) == 1
            assert not rig.factory.adapters[0]._stopped

            # Once it ends, the next turn is the one that renews.
            await rig.finish_turn()
            await rig.send("second")
            await _settle(lambda: len(rig.factory.adapters) == 2, "the agent to reopen")
            assert rig.backend.minted == ["gw-1", "gw-2"]


@pytest.mark.parametrize(
    ("status", "elapsed", "runs_on_old"),
    [
        pytest.param(401, TTL - timedelta(minutes=30), False, id="session-gone"),
        pytest.param(403, TTL - timedelta(minutes=30), False, id="not-the-publisher"),
        pytest.param(503, TTL - timedelta(minutes=30), True, id="unavailable-old-still-good"),
        pytest.param(503, TTL + timedelta(minutes=1), False, id="unavailable-old-lapsed"),
    ],
)
async def test_a_mint_that_fails_never_starts_a_turn_on_a_dead_token(
    tmp_path: Path, status: int, elapsed: timedelta, runs_on_old: bool
) -> None:
    with freeze_time(T0, real_asyncio=True) as frozen:
        # The mint is sent twice when the backend was cut (a 503), so a cut
        # that persists is scripted twice.
        async with _rig(tmp_path, _Backend([200, status, status])) as rig:
            await _first_turn(rig)
            frozen.move_to(T0 + elapsed)

            await rig.send("second")
            if runs_on_old:
                await _settle(lambda: rig.asked(0) == ["first", "second"], "the second turn")
                assert rig.mirror.gateway_token_expires_at == T0 + TTL
            else:
                await _settle(
                    lambda: rig.cancelled() == [PROMPT_CANCELLED_FAILED],
                    "the message to be dropped",
                )
                assert rig.asked(0) == ["first"]
            # Either way the agent the chat had is the one it still has.
            assert len(rig.factory.adapters) == 1
            assert not rig.factory.adapters[0]._stopped
            assert rig.backend.minted == ["gw-1"]
