"""A browser or Slack mode switch settles the ask a reader is looking at.

Both surfaces switch a cloud chat's stance the same way: the server records it
on the chat row and relays ``{"mode": …}`` to the box running the chat. The box
decides every ask, so it is where a pending ask is decided again under the new
stance. What the readers then see arrives the way any resolution does: the
agent is answered, the harness echoes ``permission.resolved``, and that echo
rides this chat's outbound lane into the transcript every surface folds.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.harness import HarnessRuntime, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapters.opencode_translate import ask_options
from alkera_cli.host import paths
from alkera_cli.plugins.plugin_base.permissions.bash import classify_command
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionRequest, PermissionResolved

_T = datetime(2026, 9, 29, tzinfo=UTC)

#: How long a wait on the session's own signal may take before it is a failure
#: rather than a slow machine. Only a bound: every wait below returns the moment
#: its event lands. The first write ask in a fresh test worker goes through the
#: file-edit evidence lookup, whose deferred imports and manifest read run on a
#: worker thread; on a loaded Windows gate runner that alone has taken over five
#: seconds. Kept well inside the suite's per-test timeout so a lost ask still
#: fails here, by name, instead of as a timeout.
SETTLE_BOUND = 30.0

CHAT_ID = "chat-mode"
OWNER = "00000000-0000-4000-8000-000000000001"

#: A write the fence reads end to end into this chat's own folder: the stance,
#: and nothing else, decides whether a reader is asked about it.
WRITE = "echo hi > out.txt"


def _shell_ask(command: str, *, request_id: str) -> PermissionRequest:
    descriptor = classify_command(command)
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_T,
        session_id=CHAT_ID,
        request_id=request_id,
        tool_call_id=f"call-{request_id}",
        permission_kind="bash",
        canonical_kind="shell",
        patterns=[command],
        subject=descriptor.model_dump(mode="json"),
        options=ask_options(always=["**"], descriptor=descriptor),
    )


class _Mirror:
    def __init__(self, mirror: ChatMirror, adapter: FakeAdapter) -> None:
        self.mirror = mirror
        self.adapter = adapter
        self._seen: list[dict[str, Any]] = []

    async def parked(self, request: PermissionRequest) -> None:
        await self.adapter.feed(request)
        await self._until(
            lambda: True if request.request_id in self.mirror.pending_interrupts else None,
            f"{request.request_id} never reached a reader",
        )

    def replies(self, request_id: str) -> list[str]:
        return [str(o) for r, o in self.adapter.permission_replies if r == request_id]

    async def replied(self, request_id: str) -> str:
        return cast(
            str,
            await self._until(
                lambda: next(iter(self.replies(request_id)), None), f"no reply for {request_id}"
            ),
        )

    async def published_resolution(self, request_id: str, option: str) -> dict[str, Any]:
        """Let the harness echo its reply, and return the ``permission.resolved``
        entry this chat's outbound lane carried to the server."""
        await self.adapter.feed(
            PermissionResolved(
                event_id=f"done-{request_id}",
                time=_T,
                session_id=CHAT_ID,
                request_id=request_id,
                option_id=option,  # type: ignore[arg-type]
            )
        )
        return cast(
            dict[str, Any],
            await self._until(
                lambda: next(
                    (
                        entry
                        for entry in self._drain()
                        if entry.get("kind") == "permission.resolved"
                        and entry.get("payload", {}).get("request_id") == request_id
                    ),
                    None,
                ),
                f"no permission.resolved for {request_id} went out",
            ),
        )

    def _drain(self) -> list[dict[str, Any]]:
        while not self.mirror._outbound.empty():
            entry = self.mirror._outbound.get_nowait()
            if "__meta__" not in entry:
                self._seen.append(entry)
        return list(self._seen)

    async def _until(self, read: Callable[[], Any], complaint: str) -> Any:
        deadline = asyncio.get_running_loop().time() + SETTLE_BOUND
        while asyncio.get_running_loop().time() < deadline:
            value = read()
            if value is not None:
                return value
            await asyncio.sleep(0.02)
        raise AssertionError(complaint)


@pytest.fixture
async def chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Mirror]:
    home = tmp_path / "alkera-home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    workspace = tmp_path / "work"
    (workspace / "src").mkdir(parents=True)
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
    runtime.project.chats().create(session_id=CHAT_ID, title="t", harness_type="agent").close()
    handle = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://objects.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={})),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
        permission_mode="default",
    )
    handle._session = await handle.open_session(
        PermissionBroker(handle._resolve_permission, default_timeout_seconds=None)
    )
    handle._session.set_permission_mode("default")
    pump = asyncio.get_running_loop().create_task(handle._pump(handle._session.subscribe()))
    handle._outbound = asyncio.Queue()
    try:
        yield _Mirror(handle, factory.adapters[0])
    finally:
        pump.cancel()
        with contextlib.suppress(BaseException):
            await pump
        await runtime.close_chat(CHAT_ID)


@pytest.mark.parametrize(
    ("mode", "option"),
    [
        pytest.param("bypass", "allow_once", id="bypass-allows"),
        pytest.param("read_only", "reject_once", id="read-only-refuses"),
    ],
)
async def test_a_relayed_mode_switch_settles_the_parked_ask_for_every_reader(
    chat: _Mirror, mode: str, option: str
) -> None:
    ask = _shell_ask(WRITE, request_id="req-1")
    await chat.parked(ask)

    await chat.mirror._on_mode({"mode": mode})

    assert await chat.replied("req-1") == option
    assert chat.mirror.pending_interrupts == [], "no reader is holding the ask any more"
    entry = await chat.published_resolution("req-1", option)
    assert entry["payload"]["option_id"] == option
    assert entry["payload"]["decided_by"] == "policy", "the stance decided, not a reader"

    # A card drawn before the switch answers nothing now.
    chat.mirror._answer_interrupt("req-1", {"option_id": "allow_once"})
    for _ in range(10):
        await asyncio.sleep(0)
    assert chat.replies("req-1") == [option]


async def test_a_relayed_mode_that_still_asks_leaves_the_reader_s_card_as_it_was(
    chat: _Mirror,
) -> None:
    ask = _shell_ask(WRITE, request_id="req-2")
    await chat.parked(ask)
    announced = [e for e in chat._drain() if e.get("payload", {}).get("prompting") is True]

    await chat.mirror._on_mode({"mode": "auto"})
    for _ in range(20):
        await asyncio.sleep(0)

    assert chat.replies("req-2") == []
    assert chat.mirror.pending_interrupts == ["req-2"]
    again = [e for e in chat._drain() if e.get("payload", {}).get("prompting") is True]
    assert again == announced, "the card is not raised a second time"
    chat.mirror._answer_interrupt("req-2", {"option_id": "allow_once"})
    assert await chat.replied("req-2") == "allow_once"
