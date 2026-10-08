"""Two messages sent a moment apart are two turns, each with its own answer.

A shared chat is where this bites: two readers press Send within the same
second, and the box has to hand the first message to the harness, wait for its
answer, and only then hand over the second. A second message sent over a live
turn supersedes it (or is folded into it), so the first reader would get no
answer of their own.

The mirror runs over a real runtime and a real chat store with the fake
adapter, and the harness's own events are fed in by hand, so every assertion
is about what the lane handed over and when.
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
from alkera_cli.harness import ChatSession, HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    MessageCompleted,
    MessageCreated,
    PartCreated,
    SessionStatusChanged,
    TextPart,
)
from alkera_core.schemas.objects import PromptRelay

pytestmark = pytest.mark.asyncio

CHAT_ID = "38767e18-7b62-4aad-bdc6-bee1ec6c5178"
ANN = "cf81f58c-9884-4e28-b8f9-469dfed97673"
BEA = "2860f5c1-1c11-4ec2-a124-447eb4a0c0de"
SETTLE_SECONDS = 20.0


async def _settle(predicate: Callable[[], bool], what: str) -> None:
    """Turn the loop until ``predicate`` holds; the deadline only bounds a hang
    (handing a prompt over crosses ``asyncio.to_thread``, so wall clock has to
    pass, not merely loop turns)."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.001)


def _prompt(text: str, client_id: str, *, seq: int, user_id: str) -> dict[str, Any]:
    return PromptRelay(
        message_id=f"row-{seq}",
        seq=seq,
        text=text,
        client_id=client_id,
        user_id=user_id,
    ).model_dump(mode="json")


def _status(event_id: str, status: str, turn_id: str) -> SessionStatusChanged:
    return SessionStatusChanged(
        event_id=event_id,
        time=datetime.now(UTC),
        session_id=CHAT_ID,
        status=status,  # type: ignore[arg-type]
        phase="idle" if status == "idle" else "awaiting_llm",
        turn_id=turn_id,
    )


@dataclass
class _Rig:
    mirror: ChatMirror
    session: ChatSession
    adapter: FakeAdapter

    def asked(self) -> list[str]:
        return [prompt.text for prompt in self.adapter.sent_prompts]

    def turn_of(self, index: int) -> str:
        return self.adapter.sent_prompts[index].turn_id

    def published(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not self.mirror._outbound.empty():
            entry = self.mirror._outbound.get_nowait()
            if "__meta__" not in entry:
                out.append(entry)
        return out

    async def answer(self, index: int, text: str) -> None:
        """The harness answering the prompt it was handed ``index``-th: the
        turn running, one assistant message, and the idle that ends it."""
        turn = self.turn_of(index)
        now = datetime.now(UTC)
        message_id = f"msg-answer-{index}"
        await self.adapter.feed_many(
            [
                _status(f"run-{index}", "running", turn),
                MessageCreated(
                    event_id=f"created-{index}",
                    time=now,
                    session_id=CHAT_ID,
                    message_id=message_id,
                    role="assistant",
                ),
                PartCreated(
                    event_id=f"part-{index}",
                    time=now,
                    session_id=CHAT_ID,
                    part=TextPart(part_id=f"p-{index}", message_id=message_id, text=text),
                ),
                MessageCompleted(
                    event_id=f"done-{index}",
                    time=now,
                    session_id=CHAT_ID,
                    message_id=message_id,
                    finish_reason="stop",
                ),
                _status(f"idle-{index}", "idle", turn),
            ]
        )


def _serves(rows: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    """The transcript route over ``rows`` (a live list: a test appends to it
    as the chat moves on), paged the way the real one pages it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/messages"):
            return httpx.Response(404)
        after = int(request.url.params.get("after_seq", 0))
        page = [row for row in rows if row["seq"] > after]
        return httpx.Response(
            200,
            json={
                "items": page,
                "next_after_seq": page[-1]["seq"] if page else after,
                "resync_from": None,
            },
        )

    return handler


@asynccontextmanager
async def _rig(
    tmp_path: Path, *, transcript: list[dict[str, Any]] | None = None
) -> AsyncIterator[_Rig]:
    project = ProjectDirectory(tmp_path / ".alkera")
    project.chats().create(session_id=CHAT_ID, title="shared").close()
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(project, adapter_factory=factory)
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://turns.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(_serves(transcript if transcript is not None else [])),
        ),
        user_id=ANN,
        owner_user_id=ANN,
    )
    mirror._ready.set()
    session = await runtime.open_chat(CHAT_ID)
    mirror._session = session
    tasks = [
        asyncio.create_task(mirror._pump(session.subscribe())),
        asyncio.create_task(mirror._prompt_lane()),
    ]
    try:
        yield _Rig(mirror=mirror, session=session, adapter=factory.adapters[-1])
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await session.close()


A_TEXT = "A: write the numbers one to forty as English words, then END-A"
B_TEXT = "B: write the numbers one to forty as English words, then END-B"


async def test_two_prompts_in_one_millisecond_are_two_turns_in_order(tmp_path: Path) -> None:
    """Both relays land in the same loop turn. The harness is handed A alone;
    B waits until A's turn is over, then gets a turn of its own — and the
    transcript reads A, A's answer, B, B's answer."""
    async with _rig(tmp_path) as rig:
        await asyncio.gather(
            rig.mirror._handle_relay(_prompt(A_TEXT, "web-a-2", seq=113, user_id=ANN)),
            rig.mirror._handle_relay(_prompt(B_TEXT, "web-b-2", seq=114, user_id=BEA)),
        )
        await _settle(lambda: rig.asked() == [A_TEXT], "A to reach the harness")
        # B is taken by the lane and held behind A's turn, not handed over.
        await _settle(lambda: rig.mirror._holding is not None, "the lane to take B")
        assert rig.mirror._holding is not None
        assert rig.mirror._holding["client_id"] == "web-b-2"
        assert rig.asked() == [A_TEXT]

        await rig.answer(0, "one … forty END-A")
        await _settle(lambda: len(rig.asked()) == 2, "B to reach the harness")
        assert rig.asked() == [A_TEXT, B_TEXT]
        assert rig.turn_of(0) != rig.turn_of(1), "two turns, not one"

        await rig.answer(1, "one … forty END-B")
        await _settle(lambda: rig.mirror._idle.is_set(), "B's turn to end")

        answers = [
            entry["payload"]["part"]["text"]
            for entry in rig.published()
            if entry.get("kind") == "part.created"
        ]
        assert answers == ["one … forty END-A", "one … forty END-B"]


async def test_a_late_status_from_the_last_turn_does_not_let_the_next_message_in(
    tmp_path: Path,
) -> None:
    """A turn ends and the lane hands B over; before the harness reports B's
    turn, a second idle for A's turn arrives. That idle is A's, not B's: B's
    turn is still running, and C — queued behind it — is not handed over until
    B's own turn ends. Read as B's, it frees the lane and C lands on B's live
    turn, which is two prompts in one turn."""
    async with _rig(tmp_path) as rig:
        await rig.mirror._handle_relay(_prompt(A_TEXT, "web-a", seq=1, user_id=ANN))
        await _settle(lambda: rig.asked() == [A_TEXT], "A to reach the harness")
        await rig.mirror._handle_relay(_prompt(B_TEXT, "web-b", seq=2, user_id=BEA))
        await rig.mirror._handle_relay(_prompt("C: and forty-one", "web-c", seq=3, user_id=ANN))

        await rig.answer(0, "END-A")
        await _settle(lambda: len(rig.asked()) == 2, "B to reach the harness")
        await rig.adapter.feed(_status("idle-0-again", "idle", rig.turn_of(0)))
        # A barrier: the pump handles events in order, so once this part is on
        # the wire the straggler before it has been read.
        now = datetime.now(UTC)
        await rig.adapter.feed(
            PartCreated(
                event_id="barrier",
                time=now,
                session_id=CHAT_ID,
                part=TextPart(part_id="p-barrier", message_id="msg-b", text="barrier"),
            )
        )
        await _settle(
            lambda: any(
                entry.get("event_id") == "barrier" for entry in rig.mirror._outbound._queue
            ),
            "the barrier to be published",
        )
        assert not rig.mirror._idle.is_set(), "B's turn is still running"
        assert rig.mirror.turn_running
        assert rig.asked() == [A_TEXT, B_TEXT]

        await rig.answer(1, "END-B")
        await _settle(lambda: len(rig.asked()) == 3, "C to reach the harness")
        assert rig.asked() == [A_TEXT, B_TEXT, "C: and forty-one"]


# --- a message the record shows answered is never run again -------------------


def _row(
    seq: int, *, role: str, kind: str, event_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": f"row-{seq}",
        "chat_id": CHAT_ID,
        "seq": seq,
        "role": role,
        "kind": kind,
        "event_id": event_id,
        "payload": payload,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _recorded_prompt(seq: int, text: str, client_id: str, user_id: str) -> dict[str, Any]:
    """A person's message on the record, as the transcript route serves it."""
    return _row(
        seq,
        role="user",
        kind="prompt",
        event_id=f"usr:{client_id}",
        payload={"kind": "prompt", "text": text, "client_id": client_id, "user_id": user_id},
    )


def _machine(seq: int, event: Any, *, role: str) -> dict[str, Any]:
    """A harness event the machine published, in the envelope it wears."""
    body = event.model_dump(mode="json")
    return _row(
        seq,
        role=role,
        kind=event.event_type,
        event_id=event.event_id,
        payload={
            "event_id": event.event_id,
            "role": role,
            "kind": event.event_type,
            "payload": body,
        },
    )


def _one_turn_over_two_prompts() -> list[dict[str, Any]]:
    """The saved chat's shape: A's and B's messages at 113 and 114, then one
    turn of the machine's — the harness's echoes of the messages it accepted
    (user-role rows that are not questions), its status, one answer, idle."""
    now = datetime.now(UTC)
    turn = "49de65be1eeb1b12c5fe"
    return [
        _recorded_prompt(113, A_TEXT, "web-14aadcfb-2", ANN),
        _recorded_prompt(114, B_TEXT, "web-f3536dc9-2", BEA),
        _machine(
            117,
            MessageCreated(
                event_id="echo-a", time=now, session_id=CHAT_ID, message_id="msg-a", role="user"
            ),
            role="user",
        ),
        _machine(
            118,
            MessageCreated(
                event_id="echo-b", time=now, session_id=CHAT_ID, message_id="msg-b", role="user"
            ),
            role="user",
        ),
        _machine(122, _status("run", "running", turn), role="system"),
        _machine(
            123,
            PartCreated(
                event_id="answer",
                time=now,
                session_id=CHAT_ID,
                part=TextPart(part_id="p", message_id="msg-reply", text="END-A END-B"),
            ),
            role="assistant",
        ),
        _machine(
            137,
            MessageCompleted(
                event_id="done",
                time=now,
                session_id=CHAT_ID,
                message_id="msg-reply",
                finish_reason="stop",
            ),
            role="assistant",
        ),
        _machine(150, _status("idle", "idle", turn), role="system"),
    ]


async def test_two_prompts_one_turn_consumed_are_not_rerun_by_a_later_catch_up(
    tmp_path: Path,
) -> None:
    """A box that holds no memory of either message — a restart, a second
    process — reads a transcript where one turn of the machine's follows both
    messages. Both were answered; the catch-up runs neither."""
    async with _rig(tmp_path, transcript=_one_turn_over_two_prompts()) as rig:
        assert await rig.mirror.catch_up() == 0
        assert await rig.mirror.catch_up() == 0
        assert rig.asked() == []
        assert rig.mirror._prompts.empty() and rig.mirror._holding is None


async def test_a_catch_up_during_the_turn_does_not_run_its_messages_again(
    tmp_path: Path,
) -> None:
    """Both messages arrived live and A's turn is running; nothing the machine
    published is on the record yet, so read alone the transcript says both are
    unanswered. They were taken the moment they arrived, so a catch-up now —
    and another once A is answered — hands neither over a second time."""
    rows = [
        _recorded_prompt(113, A_TEXT, "web-a-2", ANN),
        _recorded_prompt(114, B_TEXT, "web-b-2", BEA),
    ]
    async with _rig(tmp_path, transcript=rows) as rig:
        await rig.mirror._handle_relay(_prompt(A_TEXT, "web-a-2", seq=113, user_id=ANN))
        await rig.mirror._handle_relay(_prompt(B_TEXT, "web-b-2", seq=114, user_id=BEA))
        await _settle(lambda: rig.asked() == [A_TEXT], "A to reach the harness")

        assert await rig.mirror.catch_up() == 0

        await rig.answer(0, "END-A")
        await _settle(lambda: len(rig.asked()) == 2, "B to reach the harness")
        rig.mirror._consumed_seq = 0  # a read from the top, as after a reconnect
        assert await rig.mirror.catch_up() == 0
        await rig.answer(1, "END-B")
        await _settle(lambda: rig.mirror._idle.is_set(), "B's turn to end")
        assert rig.asked() == [A_TEXT, B_TEXT]


async def test_a_message_the_catch_up_took_is_not_run_again_by_its_late_relay(
    tmp_path: Path,
) -> None:
    """The saved chat's re-run: the transcript read reached the box before the
    live relay did, so the catch-up queued the message — and then the relay
    for the same message arrived and queued it again. The harness ran the
    bash command twice, and the second run asked the readers to approve it a
    second time. One message is one turn, whichever path brought it first."""
    bash = "use your bash tool to run exactly: mkdir -p explore-sharing-2207"
    rows = [_recorded_prompt(81, bash, "web-105e3aa6-1", BEA)]
    async with _rig(tmp_path, transcript=rows) as rig:
        assert await rig.mirror.catch_up() == 1
        await _settle(lambda: rig.asked() == [bash], "the message to reach the harness")

        await rig.mirror._handle_relay(_prompt(bash, "web-105e3aa6-1", seq=81, user_id=BEA))
        await rig.answer(0, "done-A-42")
        # The barrier: the lane is one queue in order, so by the time the next
        # message reaches the harness a second copy would have gone first.
        await rig.mirror._handle_relay(_prompt(A_TEXT, "web-a-2", seq=113, user_id=ANN))
        await _settle(lambda: len(rig.asked()) == 2, "the next message to reach the harness")
        assert rig.asked() == [bash, A_TEXT]
