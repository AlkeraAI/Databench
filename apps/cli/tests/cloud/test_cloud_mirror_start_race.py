"""A mirror stopped while it is still starting stays stopped.

Starting a mirror is a sequence of awaits — the chat document, the source
brief, the harness session — and a drain reaches the box in whichever one it
lands in. The stop tears the mirror down under the start: the document is
closed and dropped, the tasks are cancelled, the chat comes off the box. What
the start does next decides whether that is orderly or a crash.

On the live box it was a crash: a drain arrived while a chat's session was
opening, and the start came back to a document that was no longer there —
``AttributeError: 'NoneType' object has no attribute 'on_snapshot'`` — read by
the service as "this chat failed to start" and retried every thirty seconds.
Worse than the log line: the harness session the start had just spawned was
opened AFTER the stop looked for one, so nothing was ever going to close it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket, DocHandle
from alkera_cli.cloud.mirror import ChatMirror, ChatMirrorStoppedError
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory

CHAT_ID = "11111111-2222-3333-4444-555555555555"
OWNER = "66666666-7777-8888-9999-000000000000"


class _LiveSocket(CloudSocket):
    """A socket whose document is live the moment it is opened."""

    def open_doc(self, doc_type: Any, doc_id: str, *, presence: bool = True) -> DocHandle:
        handle = super().open_doc(doc_type, doc_id, presence=presence)
        handle.can_write = True
        handle.live.set()
        return handle


def _mirror(tmp_path: Path) -> tuple[ChatMirror, HarnessRuntime, FakeAdapterFactory]:
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)
    rest = CloudRestClient(
        api_url="http://objects.test",
        token="device-jwt",
        agent_id=CHAT_ID,
        transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={})),
    )
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=_LiveSocket(rest),
        rest=rest,
        user_id=OWNER,
        owner_user_id=OWNER,
    )
    return mirror, runtime, factory


async def test_a_stop_that_lands_while_the_session_is_opening_leaves_no_session_behind(
    tmp_path: Path,
) -> None:
    """The live shape: the drain arrives in the window the harness is spawning in.

    The start must not go on to use the document the stop dropped, and the
    session it spawned a moment too late has to be closed by the start itself —
    the stop is already past the point where it looks for one.
    """
    mirror, runtime, factory = _mirror(tmp_path)
    opening = mirror.open_session

    async def stop_while_opening(*args: Any, **kwargs: Any) -> Any:
        session = await opening(*args, **kwargs)
        await mirror.stop()
        return session

    mirror.open_session = stop_while_opening  # type: ignore[method-assign]

    with pytest.raises(ChatMirrorStoppedError):
        await mirror.start()

    assert runtime.open_session_ids == [], "the chat the start opened is still on the runtime"
    assert [adapter.stopped for adapter in factory.adapters] == [True], (
        "the agent the start spawned was left running"
    )
    assert mirror.quiescent is True


async def test_a_stop_that_lands_while_the_document_is_opening_refuses_the_start(
    tmp_path: Path,
) -> None:
    """The same window, one await earlier: no session was spawned at all, and
    the start says the chat is not served here rather than raising on the
    document the stop closed."""
    mirror, runtime, factory = _mirror(tmp_path)
    awaiting = mirror._await_doc

    async def stop_while_awaiting(doc: Any) -> Any:
        refusal = await awaiting(doc)
        await mirror.stop()
        return refusal

    mirror._await_doc = stop_while_awaiting  # type: ignore[method-assign]

    with pytest.raises(ChatMirrorStoppedError):
        await mirror.start()

    assert runtime.open_session_ids == []
    assert factory.adapters == [], "a stopped mirror spawned an agent anyway"


async def test_an_ordinary_start_is_untouched_by_the_guard(tmp_path: Path) -> None:
    """The premise of both cases above: with no stop in the window the start
    runs to the end, so the refusal is the race and never the ordinary path."""
    mirror, runtime, _factory = _mirror(tmp_path)
    await mirror.start()
    try:
        assert runtime.open_session_ids == [CHAT_ID]
    finally:
        await mirror.stop()
    assert runtime.open_session_ids == []
    await asyncio.sleep(0)
