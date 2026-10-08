"""A chat turn whose model call kept failing, as the cloud sees it.

On a node whose gateway could not be reached the agent retried its model call
for the whole of the turn's budget, and the reader watched "working" with
nothing to say why. The harness now stops such a turn and settles it failed;
this file proves the mirror carries that failure to the durable transcript
the way it carries any other failed turn — the retries as they happened, then
the failed terminal with its reason — and that the chat is free again, so the
next message is answered rather than queued behind a turn that is over.

The mirror runs over a real runtime and chat store with the fake adapter; the
agent's retries are fed in by hand.
"""

from __future__ import annotations

import asyncio
import time
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
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.cloud.publish import RoleIndex, append_entry
from alkera_cli.harness import ChatSession, HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import Retrying, SessionStatusChanged
from alkera_core.schemas.objects import PromptRelay

pytestmark = pytest.mark.asyncio

CHAT_ID = "5d0c2b8e-0f4a-4c61-9a7e-3f1b2c4d5e6f"
OWNER = "a1b2c3d4-e5f6-4789-8abc-def012345678"
SETTLE_SECONDS = 20.0
#: What bun's fetch says when the gateway's port is closed, URL and all.
UNREACHABLE = "Unable to connect. Is the computer able to access the url? http://10.0.4.7:8081/v1"
FAILED = "The model could not be reached after 3 attempts; the turn was stopped."


async def _settle(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + SETTLE_SECONDS
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.001)


def _prompt(text: str, seq: int) -> dict[str, Any]:
    return PromptRelay(
        message_id=f"row-{seq}", seq=seq, text=text, client_id=f"web-{seq}", user_id=OWNER
    ).model_dump(mode="json")


@dataclass
class _Rig:
    mirror: ChatMirror
    session: ChatSession
    adapter: FakeAdapter
    seen: list[dict[str, Any]]

    def asked(self) -> list[str]:
        return [prompt.text for prompt in self.adapter.sent_prompts]

    def published(self) -> list[dict[str, Any]]:
        while not self.mirror._outbound.empty():
            entry = self.mirror._outbound.get_nowait()
            if "__meta__" not in entry:
                self.seen.append(entry)
        return self.seen

    async def retry(self, attempt: int) -> None:
        await self.adapter.feed(
            Retrying(
                event_id=f"retry-{attempt}",
                time=datetime.now(UTC),
                session_id=CHAT_ID,
                attempt=attempt,
                reason=UNREACHABLE,
                next_attempt_in_ms=2000 * attempt,
            )
        )

    async def running(self, index: int) -> None:
        await self.adapter.feed(
            SessionStatusChanged(
                event_id=f"run-{index}",
                time=datetime.now(UTC),
                session_id=CHAT_ID,
                status="running",
                phase="awaiting_llm",
                turn_id=self.adapter.sent_prompts[index].turn_id,
            )
        )


def _serves_nothing(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/messages"):
        return httpx.Response(200, json={"items": [], "next_after_seq": 0, "resync_from": None})
    return httpx.Response(404)


@asynccontextmanager
async def _rig(tmp_path: Path) -> AsyncIterator[_Rig]:
    project = ProjectDirectory(tmp_path / ".alkera")
    project.chats().create(session_id=CHAT_ID, title="retrying").close()
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(project, adapter_factory=factory)
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://retry.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(_serves_nothing),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
    )
    mirror._ready.set()
    session = await runtime.open_chat(CHAT_ID)
    mirror._session = session
    tasks = [
        asyncio.create_task(mirror._pump(session.subscribe())),
        asyncio.create_task(mirror._prompt_lane()),
    ]
    try:
        yield _Rig(mirror=mirror, session=session, adapter=factory.adapters[-1], seen=[])
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await session.close()


async def test_a_turn_stopped_for_retrying_is_published_failed_and_frees_the_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("alkera_cli.harness.runtime.MAX_MODEL_RETRIES", 3)
    monkeypatch.setattr("alkera_cli.harness.runtime.MODEL_RETRY_WINDOW_SECONDS", None)
    async with _rig(tmp_path) as rig:
        await rig.mirror._handle_relay(_prompt("count the mentions", seq=1))
        await _settle(lambda: rig.asked() == ["count the mentions"], "the prompt to reach it")
        await rig.running(0)
        for attempt in (1, 2, 3):
            await rig.retry(attempt)

        def terminal() -> dict[str, Any] | None:
            for entry in rig.published():
                payload = entry.get("payload", {})
                if entry.get("kind") == "session.status_changed" and payload.get("status") in (
                    "error",
                    "aborted",
                    "idle",
                ):
                    return entry
            return None

        await _settle(lambda: terminal() is not None, "the turn's terminal to be published")
        assert rig.adapter.cancel_count == 1, "the agent was not stopped"

        # Each retry is on the record as it happened, unchanged.
        retries = [e for e in rig.published() if e.get("kind") == "retrying"]
        assert [e["payload"]["attempt"] for e in retries] == [1, 2, 3]

        # The failure is published as any failed turn is: the same row
        # ``append_entry`` makes of a failed terminal, with the reason.
        failures = [
            e
            for e in rig.published()
            if e.get("kind") == "session.status_changed" and e["payload"].get("status") == "error"
        ]
        assert len(failures) == 1, rig.published()
        (failed,) = failures
        assert failed["payload"]["detail"] == FAILED
        assert failed["payload"]["turn_id"] == rig.adapter.sent_prompts[0].turn_id
        reference = append_entry(
            SessionStatusChanged.model_validate(failed["payload"]), RoleIndex()
        )
        assert reference["kind"] == failed["kind"]
        assert reference["payload"]["detail"] == FAILED
        assert "10.0.4.7" not in str(failed), "the provider's URL reached the failure"

        # The chat is free: the next message is handed over, not held.
        await _settle(lambda: rig.mirror._idle.is_set(), "the chat to read as free")
        await rig.mirror._handle_relay(_prompt("try again", seq=2))
        await _settle(lambda: len(rig.asked()) == 2, "the next message to reach the harness")
        assert rig.asked() == ["count the mentions", "try again"]
