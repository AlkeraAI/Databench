"""Stop empties the queue behind the turn it ends.

A message relayed while a turn is running waits behind it, and ending a turn is
exactly what frees the lane. So before this, every Stop handed the harness the
next message in the queue the instant the turn it ended went quiet — and a
reader who could not see that a queue existed read that as Stop doing nothing
and pressed it again, ending the turn their own older message had just started.

A stop therefore takes the queue out first and says what became of each message
in it, rather than dropping them where the reader would never learn they went.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.attachments import MaterializedAttachments
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.harness import ChatSession, HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    Event,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PromptCancelled,
    SessionStatusChanged,
    TextPart,
    ToolCall,
    ToolCallUpdate,
)
from alkera_core.schemas.objects import PromptRelay, prompt_cancelled_event_id

pytestmark = pytest.mark.asyncio

CHAT_ID = "6f1c9a20-77c1-4d2e-9f0b-1a2b3c4d5e6f"
OWNER = "22222222-3333-4444-8555-666666666666"

#: The relay the route mints when a reader presses Stop.
STOP: dict[str, Any] = {
    "schema_version": "1.0.0",
    "metadata": {},
    "kind": "stop",
    "user_id": OWNER,
}


def _prompt(
    text: str, client_id: str, *, seq: int = 1, message_id: str | None = None
) -> dict[str, Any]:
    """A person's message on the wire, exactly as the route serialises one."""
    return PromptRelay(
        message_id=message_id if message_id is not None else f"row-{client_id}",
        seq=seq,
        text=text,
        client_id=client_id,
        user_id=OWNER,
    ).model_dump(mode="json")


def _turn_ended(event_id: str) -> SessionStatusChanged:
    """The harness saying the turn it was running is over — what an abort
    leaves behind, and what frees the lane for whatever is queued."""
    return SessionStatusChanged(
        event_id=event_id,
        time=datetime.now(UTC),
        session_id=CHAT_ID,
        status="aborted",
        phase="idle",
    )


#: How long a wait here may take before it is a failure. Generous on purpose:
#: it bounds a hang, and says nothing about how fast any of this should be.
SETTLE_SECONDS = 20.0


async def _settle(predicate: Callable[[], bool], what: str) -> None:
    """Turn the loop until ``predicate`` holds, bounded by a real deadline.

    The wait has to let WALL CLOCK pass, not merely yield: asking the harness a
    question crosses ``asyncio.to_thread`` several times over (the session
    stamp, the tool registry, the context brief), and a thread only makes
    progress once the OS schedules it. A spin of bare ``sleep(0)`` hands the
    loop back to itself and returns in microseconds, so on a loaded host — a CI
    runner with far more test workers than cores — it would run out of turns
    while every one of those threads was still waiting to be scheduled, and a
    prompt that arrives perfectly well would read as never sent.
    """
    deadline = time.monotonic() + SETTLE_SECONDS
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.001)


@dataclass
class _Rig:
    mirror: ChatMirror
    session: ChatSession
    adapter: FakeAdapter
    seen: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def asked(self) -> list[str]:
        """Every prompt the harness was actually handed, in order."""
        return [prompt.text for prompt in self.adapter.sent_prompts]

    def cancelled(self) -> list[tuple[str, str, str]]:
        """Every message this chat has recorded as never run."""
        return [
            (event.message_id, event.client_id, event.reason)
            for event in self.session.events()
            if isinstance(event, PromptCancelled)
        ]

    def entries(self) -> list[dict[str, Any]]:
        """Every entry the mirror has put on the wire, whole."""
        self.published()
        return self.rows

    def published(self) -> list[str]:
        """The kinds of every entry the mirror has put on the wire so far —
        what a reader of this chat would end up with. Meta (the turn word)
        rides the same queue and is not a transcript row."""
        while not self.mirror._outbound.empty():
            entry = self.mirror._outbound.get_nowait()
            if "__meta__" in entry:
                continue
            self.seen.append(str(entry.get("kind")))
            self.rows.append(entry)
        return self.seen

    def forget_published(self) -> None:
        """Start counting from here."""
        self.published()
        self.seen.clear()
        self.rows.clear()

    def cancelled_ids(self) -> list[str]:
        """The event ids those reports carry — what decides whether the box's
        report and the server's are one row or two."""
        return [
            event.event_id for event in self.session.events() if isinstance(event, PromptCancelled)
        ]

    async def relay(self, body: dict[str, Any]) -> None:
        await self.mirror._handle_relay(dict(body))

    async def run_first_turn(self, text: str = "count the mentions") -> None:
        """Put the chat where the bug lived: a turn running, the lane free."""
        await self.relay(_prompt(text, "c0", seq=1))
        await _settle(lambda: self.asked() == [text], "the first turn to reach the harness")


def _recorded_prompt(seq: int, text: str, client_id: str) -> dict[str, Any]:
    """A person's message on the RECORD, as the transcript route serves it."""
    return {
        "id": f"row-{seq}",
        "chat_id": CHAT_ID,
        "seq": seq,
        "role": "user",
        "kind": "prompt",
        "event_id": f"usr:{client_id}",
        "payload": {"text": text, "client_id": client_id, "user_id": OWNER},
        "created_at": datetime.now(UTC).isoformat(),
    }


def _recorded_machine_row(seq: int) -> dict[str, Any]:
    """Something the machine published, which is what tells a reader of the
    transcript that the message above it was taken up."""
    return {
        "id": f"row-{seq}",
        "chat_id": CHAT_ID,
        "seq": seq,
        "role": "assistant",
        "kind": "part.created",
        "event_id": f"prt-{seq}",
        "payload": {"event_id": f"prt-{seq}", "kind": "part.created", "payload": {}},
        "created_at": datetime.now(UTC).isoformat(),
    }


def _serves(rows: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    """The transcript route, paged the way the real one pages it."""

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
    tmp_path: Path,
    *,
    prepare_attachments: (
        Callable[[str, Mapping[str, Any]], Awaitable[MaterializedAttachments]] | None
    ) = None,
    transcript: list[dict[str, Any]] | None = None,
) -> AsyncIterator[_Rig]:
    """A mirror over a real runtime and a real chat store, with the two pumps a
    started mirror runs: the harness stream in, the prompt lane out.

    ``prepare_attachments`` is the box's own seam for fetching the files a
    message names — the await a message sits in after it has left the queue and
    before the harness has it, which is the window a slow box opens.
    """
    project = ProjectDirectory(tmp_path / ".alkera")
    project.chats().create(session_id=CHAT_ID, title="ops").close()
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(project, adapter_factory=factory)
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://drain.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(
                _serves(transcript)
                if transcript is not None
                else lambda request: httpx.Response(404)
            ),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
        prepare_attachments=prepare_attachments,
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


async def test_a_stop_leaves_nothing_queued_to_start_the_next_turn(tmp_path: Path) -> None:
    """The reported failure, end to end: two messages waiting behind a running
    turn, one Stop, and the turn that ends is the last one. Neither queued
    message is handed to the harness — not while the stop is settling, and not
    when the abort frees the lane a moment later.

    A message sent AFTER the stop is the barrier: the lane is one queue in
    order, so the moment that message reaches the harness, anything the stop
    was supposed to drop would already have gone ahead of it.
    """
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        await rig.relay(_prompt("why did u stop", "c1", seq=2))
        await rig.relay(_prompt("keep going", "c2", seq=3))
        # The lane has taken the first of the two and is waiting out the turn;
        # the second is still on the queue. A stop has to reach both.
        await _settle(lambda: rig.mirror._prompts.qsize() == 1, "the lane to take a message")

        await rig.relay(STOP)
        await rig.adapter.feed(_turn_ended("stopped-1"))
        await rig.relay(_prompt("start over", "c3", seq=4))

        await _settle(lambda: len(rig.asked()) > 1, "the lane to hand over its next message")
        assert rig.adapter.cancel_count == 1, "the running turn was stopped"
        assert rig.asked() == ["count the mentions", "start over"]


async def test_every_message_a_stop_drops_is_recorded_as_never_run(tmp_path: Path) -> None:
    """A dropped message must not vanish. Each one is named on the transcript
    by the id its own bubble carries, so the reader is told which of their
    messages was not sent rather than watching them sit unanswered."""
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        await rig.relay(_prompt("why did u stop", "c1", seq=2))
        await rig.relay(_prompt("keep going", "c2", seq=3))
        await _settle(lambda: rig.mirror._prompts.qsize() == 1, "the lane to take a message")

        await rig.relay(STOP)

        await _settle(lambda: len(rig.cancelled()) == 2, "both messages to be recorded")
        assert rig.cancelled() == [
            ("usr:c1", "c1", "stopped"),
            ("usr:c2", "c2", "stopped"),
        ], "in the order they were sent, named as the server named them"
        # The id is the server's too. Both ends report a message they dropped,
        # and a reader must be told once: the server writes the same row for a
        # message nothing had begun, so the two have to spell one id.
        assert rig.cancelled_ids() == [
            prompt_cancelled_event_id("usr:c1"),
            prompt_cancelled_event_id("usr:c2"),
        ]


async def test_a_message_with_no_client_id_is_named_by_its_row(tmp_path: Path) -> None:
    """A relay carrying no client id still names something a reader holds — the
    transcript row it was recorded as — so no dropped message goes unnamed."""
    row = "9c4b1f7e-2d33-4a51-8e60-77a0b1c2d3e4"
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        await rig.relay(_prompt("keep going", "", seq=2, message_id=row))
        await _settle(lambda: rig.mirror._holding is not None, "the lane to take the message")

        await rig.relay(STOP)

        await _settle(lambda: len(rig.cancelled()) == 1, "the message to be recorded")
        assert rig.cancelled() == [(row, "", "stopped")]


async def test_a_stop_with_nothing_queued_records_nothing(tmp_path: Path) -> None:
    """The ordinary Stop is unchanged: it ends the turn and says nothing else.
    A chat with an empty lane must not grow a line about messages that do not
    exist."""
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()

        await rig.relay(STOP)
        await rig.adapter.feed(_turn_ended("stopped-1"))
        await _settle(lambda: rig.mirror._idle.is_set(), "the aborted turn to settle")

        assert rig.adapter.cancel_count == 1
        assert rig.cancelled() == [], "nothing was queued, so nothing was dropped"


async def test_a_stop_while_the_message_is_being_prepared_never_starts_the_turn(
    tmp_path: Path,
) -> None:
    """The reported failure: Stop pressed after the message was sent and before
    the box had started answering it.

    A message past the queue is not yet the harness's — the files it names are
    fetched first, and a slow box is exactly when a reader gives up waiting. So
    a stop landing in that window has to reach it: the message is never handed
    over, and the reader is told it was not sent rather than watching the agent
    answer a question they had just ended.
    """
    fetching = asyncio.Event()
    release = asyncio.Event()

    async def _prepare(_chat_id: str, _relay: Mapping[str, Any]) -> MaterializedAttachments:
        fetching.set()
        await release.wait()
        return MaterializedAttachments()

    async with _rig(tmp_path, prepare_attachments=_prepare) as rig:
        await rig.relay(_prompt("count the mentions", "c0", seq=1))
        await _settle(fetching.is_set, "the lane to start preparing the message")

        await rig.relay(STOP)
        release.set()

        await _settle(lambda: len(rig.cancelled()) == 1, "the message to be recorded")
        assert rig.cancelled() == [("usr:c0", "c0", "stopped")]
        # The barrier: a message sent after the stop reaches the harness, so by
        # the time it has, the stopped one would already have gone ahead of it.
        await rig.relay(_prompt("start over", "c1", seq=2))
        await _settle(lambda: rig.asked() == ["start over"], "the next message to run")


async def test_a_stop_while_the_turn_is_starting_cancels_the_turn_it_started(
    tmp_path: Path,
) -> None:
    """Handing the message over is itself a wait — the system prompt and the
    tool registry are composed inside it — and a stop that arrives during it
    finds no turn to cancel, because the turn has not started yet.

    It has by the time the send returns, so it is cancelled there. A turn that
    outlives the Stop that ended it leaves the reader nothing left to press.
    """
    composing = asyncio.Event()
    release = asyncio.Event()
    sent: list[str] = []
    session_send = ChatSession.send_prompt

    async def _slow_send(self: ChatSession, text: str, **kwargs: Any) -> str:
        composing.set()
        await release.wait()
        sent.append(text)
        return await session_send(self, text, **kwargs)

    async with _rig(tmp_path) as rig:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(ChatSession, "send_prompt", _slow_send)
            await rig.relay(_prompt("count the mentions", "c0", seq=1))
            await _settle(composing.is_set, "the turn to start being composed")

            await rig.relay(STOP)
            # The stop's own cancel reaches an adapter with no turn in it: the
            # prompt is still being composed, so this is the cancel that caught
            # nothing and the bug left it at that.
            assert rig.adapter.cancel_count == 1
            release.set()

            await _settle(
                lambda: rig.adapter.cancel_count == 2, "the turn the send started to be cancelled"
            )
        assert sent == ["count the mentions"], "the send was already past recall"


async def test_a_message_the_lane_is_preparing_still_runs_without_a_stop(
    tmp_path: Path,
) -> None:
    """The lane itself is untouched by holding the message longer: a message
    whose files take a while to fetch is handed over as soon as they are."""
    release = asyncio.Event()

    async def _prepare(_chat_id: str, _relay: Mapping[str, Any]) -> MaterializedAttachments:
        await release.wait()
        return MaterializedAttachments()

    async with _rig(tmp_path, prepare_attachments=_prepare) as rig:
        await rig.relay(_prompt("count the mentions", "c0", seq=1))
        release.set()

        await _settle(lambda: rig.asked() == ["count the mentions"], "the message to run")
        assert rig.cancelled() == [], "a message that ran was never cancelled"


async def test_a_message_the_box_could_not_prepare_is_reported_not_dropped(
    tmp_path: Path,
) -> None:
    """The lane's other way of losing a message: preparing it raised.

    Nothing ended the turn, so a reader is told nothing by the stop path — and
    before this the message simply stopped existing as far as they could tell,
    leaving them watching their own words sit unanswered. It is reported the
    way every dropped message is, under a reason that is NOT ``stopped``: what
    they see is their message marked as not sent, with Resend beside it.
    """

    tried: list[str] = []

    async def _explode(_chat_id: str, relay: Mapping[str, Any]) -> MaterializedAttachments:
        tried.append(str(relay.get("client_id")))
        if len(tried) == 1:
            raise RuntimeError("the chat's folder could not be read")
        return MaterializedAttachments()

    async with _rig(tmp_path, prepare_attachments=_explode) as rig:
        await rig.relay(_prompt("count the mentions", "c0", seq=1))

        await _settle(lambda: len(rig.cancelled()) == 1, "the message to be reported")
        assert rig.cancelled() == [("usr:c0", "c0", "failed")]
        assert rig.asked() == [], "and it never reached the harness"
        # The lane is not wedged by it: the next message runs.
        await rig.relay(_prompt("try again", "c1", seq=2))
        await _settle(lambda: rig.asked() == ["try again"], "the next message to run")


async def test_a_queued_message_still_runs_when_the_turn_simply_ends(tmp_path: Path) -> None:
    """The lane itself is untouched. A turn that finishes on its own hands the
    waiting message to the harness exactly as before — the drain is a stop's
    doing, not the lane's."""
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        await rig.relay(_prompt("and the totals", "c1", seq=2))
        await _settle(lambda: rig.mirror._holding is not None, "the lane to take the message")

        await rig.adapter.feed(_turn_ended("finished-1"))

        await _settle(
            lambda: rig.asked() == ["count the mentions", "and the totals"],
            "the queued message to run",
        )
        assert rig.cancelled() == [], "a message that ran was never cancelled"


# ---------------------------------------------------------------------------
# The window after the harness has already taken the prompt
# ---------------------------------------------------------------------------


def _assistant_step(tag: str) -> list[Event]:
    """What a harness produces on the model step it takes AFTER a cancel: the
    cancelled tool comes back as a result, and the agent writes about it."""
    now = datetime.now(UTC)
    return [
        MessageCreated(
            event_id=f"{tag}-created",
            time=now,
            session_id=CHAT_ID,
            message_id=tag,
            role="assistant",
        ),
        PartCreated(
            event_id=f"{tag}-text",
            time=now,
            session_id=CHAT_ID,
            part=TextPart(
                part_id=f"{tag}-part",
                message_id=tag,
                text="The command was cancelled, so I stopped there.",
            ),
        ),
        MessageCompleted(
            event_id=f"{tag}-done",
            time=now,
            session_id=CHAT_ID,
            message_id=tag,
            finish_reason="stop",
        ),
    ]


async def test_a_stop_after_the_harness_took_the_prompt_ends_the_turn_on_the_transcript(
    tmp_path: Path,
) -> None:
    """The turn is over when the reader says so, not when the agent loop runs
    out of things to say about being cancelled.

    Cancelling stops the tool; the harness gets the cancelled call back as a
    result and takes another model step on it. So the mirror ends the turn
    itself — a terminal on the transcript and the chat stamped idle — rather
    than leaving the reader watching a turn they ended keep working.
    """
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        rig.forget_published()

        await rig.relay(STOP)

        await _settle(
            lambda: rig.published() == ["session.status_changed"],
            "the stop's own terminal to reach the transcript",
        )
        assert rig.mirror._idle.is_set(), "the chat reads idle the moment the reader is answered"


async def test_nothing_the_harness_says_after_the_stop_reaches_the_chat(
    tmp_path: Path,
) -> None:
    """The reported residual: Stop landed once the prompt was already the
    harness's, the cancelled bash came back as a result, and the model wrote a
    PARAGRAPH about it — onto a chat whose own record said the message was
    never sent. Tokens spent after a Stop, and a transcript arguing with its
    own label.

    The turn ended when the reader ended it, so nothing the harness produces
    for it afterwards is published, and the agent is told again to stop.
    """
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        await rig.relay(STOP)
        await _settle(
            lambda: rig.published() == ["session.status_changed"], "the stop's own terminal"
        )
        rig.forget_published()
        cancels = rig.adapter.cancel_count

        await rig.adapter.feed_many(_assistant_step("post-stop"))
        await _settle(lambda: rig.adapter.cancel_count > cancels, "the harness to be told again")

        assert rig.published() == [], "no answer lands on a turn the reader ended"


async def test_the_next_message_publishes_normally_after_a_stopped_turn(
    tmp_path: Path,
) -> None:
    """The drop belongs to the turn that was stopped and to nothing else: the
    reader asks again and their answer is on the transcript as always."""
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        await rig.relay(STOP)
        await rig.adapter.feed(_turn_ended("stopped-1"))
        await rig.relay(_prompt("start over", "c1", seq=2))
        await _settle(lambda: rig.asked() == ["count the mentions", "start over"], "the next turn")
        rig.forget_published()

        await rig.adapter.feed_many(_assistant_step("answer"))

        await _settle(
            lambda: rig.published() == ["message.created", "part.created", "message.completed"],
            "the next turn's answer to be published",
        )


async def test_a_stop_with_no_turn_under_way_ends_nothing(tmp_path: Path) -> None:
    """The asymmetry: a Stop pressed on a chat that is not running a turn — the
    reader stopping a message that never left the lane — must not stamp a
    terminal for a turn that does not exist, and must not gag the box."""
    async with _rig(tmp_path) as rig:
        await rig.relay(STOP)

        assert rig.published() == []
        assert rig.mirror._ending_turn is False

        await rig.adapter.feed_many(_assistant_step("unrelated"))

        await _settle(
            lambda: rig.published() == ["message.created", "part.created", "message.completed"],
            "an unrelated publish to go through",
        )


async def test_the_stops_terminal_is_the_shape_a_reader_settles_a_turn_on(
    tmp_path: Path,
) -> None:
    """The terminal the stop stamps is what every reader folds the end of a
    turn from, so its payload is the contract: an ``aborted`` status, phase
    ``idle``, under the attempt it ends.

    It carries no ``detail`` on purpose — the sentence for a reader's Stop is
    the "Stopped by <who>." the server writes, and only the server knows the
    name — so a fold that treats a detail as the CONDITION for settling leaves
    this turn running for good. That is pinned on the other side, in
    ``harnessEventFold``.
    """
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        rig.forget_published()

        await rig.relay(STOP)

        await _settle(
            lambda: rig.published() == ["session.status_changed"], "the stop's own terminal"
        )
        payload = rig.entries()[0]["payload"]
        assert payload["status"] == "aborted"
        assert payload["phase"] == "idle"
        assert payload.get("detail") is None
        assert rig.entries()[0]["role"] == "system"


async def test_the_stops_terminal_is_the_same_row_however_often_it_is_stamped(
    tmp_path: Path,
) -> None:
    """A stop met twice is one terminal, and it is the ID that says so.

    The once-per-turn barrier is a flag in one process's memory: a box
    restarted mid-stop, a chat re-provisioned onto a second box, and a re-hello
    that replays the stop all reach the stamp with that flag clear. A minted id
    makes each of them a second ``aborted`` row for one turn on every reader's
    transcript — the exact failure the barrier exists to prevent, at the one
    moment it cannot. Derived from the turn, the second stamp is the row the
    record already holds and the reader sees one end to one turn.
    """
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        rig.forget_published()

        await rig.relay(STOP)
        await _settle(
            lambda: rig.published() == ["session.status_changed"], "the stop's own terminal"
        )
        first = rig.entries()[0]
        rig.forget_published()

        # The two paths that reach the stamp in the SAME process — the relay's
        # own handler and the send that was in flight when the relay landed.
        rig.mirror._stamped_stop = False
        await rig.mirror._close_stopped_turn()
        await _settle(
            lambda: rig.published() == ["session.status_changed"], "the replayed terminal"
        )
        second = rig.entries()[0]

        assert second["event_id"] == first["event_id"], (
            "the same turn stopped again is the same row, which the record drops"
        )
        assert first["event_id"] == "stopped-usr:c0", "named by the message that started the turn"


async def test_a_restarted_box_stamps_the_stopped_turn_under_the_same_id(
    tmp_path: Path,
) -> None:
    """The case the process-local barrier cannot cover: a box that actually
    restarted, a chat re-provisioned onto a second box, a re-hello from a fresh
    process. None of them holds the turn in memory, so the name has to come
    back off the record — the transcript the new box reads to catch up already
    carries the person's message, which is what the turn is called.

    A name only the first process could spell is a minted id under another
    word: the second box stamps a second ``aborted`` terminal and the reader
    sees one turn end twice. Naming it after the CHAT instead is the opposite
    failure — every stop in the chat would spell one id and a genuinely later
    turn's terminal would be dropped as a duplicate.
    """
    rows = [_recorded_prompt(1, "count the mentions", "c0"), _recorded_machine_row(2)]
    async with _rig(tmp_path, transcript=rows) as rig:
        await rig.run_first_turn()
        rig.forget_published()
        await rig.relay(STOP)
        await _settle(
            lambda: rig.published() == ["session.status_changed"], "the stop's own terminal"
        )
        first = rig.entries()[0]
        rig.forget_published()

        # A NEW process holds none of this: not the barrier, not the harness's
        # turn id, not the name the turn was dispatched under, and it has read
        # no row of the transcript.
        rig.mirror._stamped_stop = False
        rig.mirror._turn_mark = None
        rig.mirror._current_turn_id = None
        rig.mirror._consumed_seq = 0
        rig.mirror._consumed_clients.clear()

        assert await rig.mirror.catch_up() == 0, "the record owes this box no message to run"
        await rig.mirror._close_stopped_turn()
        await _settle(
            lambda: "session.status_changed" in rig.published(), "the restarted box's terminal"
        )
        terminals = [
            entry["event_id"]
            for entry in rig.entries()
            if entry["kind"] == "session.status_changed"
            and entry["payload"].get("status") == "aborted"
        ]
        assert terminals == [first["event_id"]], (
            "a box that restarted names the turn the way the box before it did"
        )
        assert first["event_id"] == "stopped-usr:c0"


async def test_two_turns_stopped_are_two_terminals(tmp_path: Path) -> None:
    """The other half: a stop on a LATER turn is a different row. Deriving the
    id is a way of not writing a fact twice, never of losing the second one —
    a turn whose terminal was dropped as a duplicate never ends on screen."""
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        await rig.relay(STOP)
        await _settle(
            lambda: rig.published().count("session.status_changed") == 1,
            "the first stop's terminal",
        )
        await rig.adapter.feed(_turn_ended("harness-ended-1"))
        await _settle(
            lambda: rig.published().count("session.status_changed") == 2,
            "the harness's own end of the first turn",
        )
        rig.forget_published()

        await rig.relay(_prompt("try again", "c9", seq=2))
        await _settle(lambda: len(rig.asked()) == 2, "the second turn to reach the harness")
        await rig.relay(STOP)
        await _settle(
            lambda: "session.status_changed" in rig.published(), "the second stop's terminal"
        )

        terminals = [
            entry["event_id"]
            for entry in rig.entries()
            if entry["kind"] == "session.status_changed"
            and entry["payload"].get("status") == "aborted"
        ]
        assert terminals == ["stopped-usr:c9"], "the second turn ends under its own name"


async def test_the_call_the_stop_killed_is_settled_on_the_transcript(
    tmp_path: Path,
) -> None:
    """What a stop must NOT swallow. The cancel kills the running tool, and the
    harness says so with the call's own result — the row every reader's tool
    card stops spinning on. Dropped with the rest of the wind-down, the card
    span forever, on reload as much as live.

    The agent's paragraph ABOUT being cancelled is still dropped: what closes a
    card is kept, what would answer the question is not.
    """
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        await rig.adapter.feed(
            ToolCall(
                event_id="call-1",
                time=datetime.now(UTC),
                session_id=CHAT_ID,
                message_id="a1",
                tool_call_id="call-1",
                tool_name="bash",
                status="running",
                input={"command": "sleep 40"},
            )
        )
        await _settle(lambda: rig.published() == ["tool.call"], "the call the reader watches")
        await rig.relay(STOP)
        await _settle(
            lambda: rig.published() == ["tool.call", "session.status_changed"],
            "the stop's own terminal",
        )
        rig.forget_published()

        await rig.adapter.feed(
            ToolCallUpdate(
                event_id="call-1-cancelled",
                time=datetime.now(UTC),
                session_id=CHAT_ID,
                tool_call_id="call-1",
                status="error",
                error="command cancelled",
            )
        )
        await rig.adapter.feed_many(_assistant_step("post-stop"))
        await rig.adapter.feed(_turn_ended("stopped-1"))

        await _settle(
            lambda: rig.published() == ["tool.call_update", "session.status_changed"],
            "the killed call and the harness's own end of the turn",
        )
        settled = rig.entries()[0]["payload"]
        assert settled["status"] == "error" and settled["error"] == "command cancelled"


# ---------------------------------------------------------------------------
# The sequence the live check recorded, as the harness produced it
# ---------------------------------------------------------------------------


def _agent_loop_after_a_cancel(turn: str) -> list[Event]:
    """What opencode actually did after the cancel, from the server rows of
    chat 6ba98e93 (seq 7..39): it ended the killed attempt, and then went
    STRAIGHT ON — another ``running`` under the same turn id, a fresh tool
    call, and a paragraph about having been cancelled — before going idle.

    Every row of it is the agent's, none of it was asked for after the stop,
    and the reader's own record already said the turn was over.
    """
    now = datetime.now(UTC)
    return [
        SessionStatusChanged(
            event_id="harness-aborted",
            time=now,
            session_id=CHAT_ID,
            status="aborted",
            phase="idle",
            turn_id=turn,
        ),
        SessionStatusChanged(
            event_id="rerun-running",
            time=now,
            session_id=CHAT_ID,
            status="running",
            phase="awaiting_llm",
            turn_id=turn,
        ),
        ToolCall(
            event_id="rerun-call",
            time=now,
            session_id=CHAT_ID,
            message_id="rerun-msg",
            tool_call_id="rerun-1",
            tool_name="bash",
            status="pending",
            input={"command": "sleep 40"},
        ),
        ToolCallUpdate(
            event_id="rerun-call-error",
            time=now,
            session_id=CHAT_ID,
            tool_call_id="rerun-1",
            status="error",
            error="command cancelled",
        ),
        *_assistant_step("rerun-paragraph"),
        SessionStatusChanged(
            event_id="rerun-idle",
            time=now,
            session_id=CHAT_ID,
            status="idle",
            phase="idle",
            turn_id=turn,
        ),
    ]


async def test_the_agent_loop_going_on_after_the_cancel_reaches_nobody(
    tmp_path: Path,
) -> None:
    """The reported failure, from the rows that produced it.

    Cancelling ends the ATTEMPT, and opencode's loop takes the cancelled call
    as a result and carries on under the same turn: another ``running``, a new
    call, and a paragraph explaining itself. Reading the attempt's terminal as
    "the harness is done" handed the whole of that to the reader — a sentence
    answering a message the same transcript said was never sent.

    A terminal ends an attempt, not the reader's Stop. Only a new message does
    that, so nothing here is published but the terminals that close.
    """
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        await rig.relay(STOP)
        await _settle(
            lambda: rig.published() == ["session.status_changed"], "the stop's own terminal"
        )
        rig.forget_published()

        await rig.adapter.feed_many(_agent_loop_after_a_cancel("turn-1"))

        await _settle(
            lambda: len(rig.published()) == 2, "the two terminals, and nothing between them"
        )
        assert rig.published() == ["session.status_changed", "session.status_changed"]
        assert [row["payload"]["status"] for row in rig.entries()] == ["aborted", "idle"]


async def test_a_call_the_agent_opened_after_the_stop_draws_no_card(
    tmp_path: Path,
) -> None:
    """The asymmetry inside the same sequence: a call the reader never saw
    START has no card to close, so its result is not published either — while
    the call that WAS on screen when the stop landed is settled, which is the
    whole reason terminals are kept at all."""
    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        watched = ToolCall(
            event_id="watched-call",
            time=datetime.now(UTC),
            session_id=CHAT_ID,
            message_id="a1",
            tool_call_id="watched-1",
            tool_name="bash",
            status="running",
            input={"command": "sleep 40"},
        )
        await rig.adapter.feed(watched)
        await _settle(lambda: rig.published() == ["tool.call"], "the call the reader watches")

        await rig.relay(STOP)
        await _settle(
            lambda: "session.status_changed" in rig.published(), "the stop's own terminal"
        )
        rig.forget_published()

        await rig.adapter.feed(
            ToolCallUpdate(
                event_id="watched-cancelled",
                time=datetime.now(UTC),
                session_id=CHAT_ID,
                tool_call_id="watched-1",
                status="error",
                error="command cancelled",
            )
        )
        await rig.adapter.feed_many(_agent_loop_after_a_cancel("turn-1"))

        await _settle(lambda: len(rig.published()) >= 1, "the watched call to settle")
        # Both calls settle in the same wind-down. Exactly one result goes out,
        # and it names the call the reader was watching: the other has no card
        # to close, so its result would draw one for work nobody saw start.
        assert rig.published().count("tool.call_update") == 1
        settled = [row for row in rig.entries() if row["kind"] == "tool.call_update"]
        assert settled[0]["payload"]["tool_call_id"] == "watched-1"
        assert "tool.call" not in rig.published(), "the call opened after the stop never appears"


async def test_a_stop_during_the_send_gags_the_turn_it_started(tmp_path: Path) -> None:
    """The third of the four windows a stop can land in — inside ``send_prompt``
    itself. The turn is already the harness's by the time the send returns, so
    it is cancelled there; and what the agent loop makes of that cancel is no
    more this chat's record than in any other window."""
    composing = asyncio.Event()
    release = asyncio.Event()
    session_send = ChatSession.send_prompt

    async def _slow_send(self: ChatSession, text: str, **kwargs: Any) -> str:
        composing.set()
        await release.wait()
        return await session_send(self, text, **kwargs)

    async with _rig(tmp_path) as rig:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(ChatSession, "send_prompt", _slow_send)
            await rig.relay(_prompt("count the mentions", "c0", seq=1))
            await _settle(composing.is_set, "the turn to start being composed")
            await rig.relay(STOP)
            release.set()
            await _settle(
                lambda: rig.mirror._ending_turn, "the started turn to be gagged as well as ended"
            )
        rig.forget_published()

        await rig.adapter.feed_many(_agent_loop_after_a_cancel("turn-1"))

        await _settle(
            lambda: "idle" in [row["payload"].get("status") for row in rig.entries()],
            "the harness's own end of the turn",
        )
        assert set(rig.published()) == {"session.status_changed"}, (
            "only what closes; nothing the agent went on to say"
        )


#: An event fed through the harness AFTER both stamping paths have run. The
#: bus is one queue in order, so when this has been published everything
#: published before it has been too — which is what lets a test say "and
#: nothing else came" without asking the clock.
SENTINEL = "sentinel-last"


def _sentinel() -> SessionStatusChanged:
    return SessionStatusChanged(
        event_id=SENTINEL,
        time=datetime.now(UTC),
        session_id=CHAT_ID,
        status="idle",
        phase="idle",
        turn_id="sentinel-turn",
    )


def _terminals_before_the_sentinel(rig: _Rig) -> list[str] | None:
    """The turn-ending statuses published ahead of the sentinel, or None while
    the sentinel has not come through yet."""
    rows = rig.entries()
    if not any(row["event_id"] == SENTINEL for row in rows):
        return None
    out: list[str] = []
    for row in rows:
        if row["event_id"] == SENTINEL:
            return out
        status = row["payload"].get("status")
        if status in ("aborted", "cancelled"):
            out.append(str(status))
    return out


async def test_one_stop_stamps_one_end_of_the_turn(tmp_path: Path) -> None:
    """A Stop ends a turn once, whichever window it lands in.

    Two paths stamp it — the relay's own handler, and the send that was in
    flight when the relay landed — and in the window where BOTH run, a turn
    stamped twice is two terminals for one Stop on every reader's transcript.
    """
    composing = asyncio.Event()
    release = asyncio.Event()
    session_send = ChatSession.send_prompt
    stamped: list[None] = []
    close = ChatMirror._close_stopped_turn

    async def _slow_send(self: ChatSession, text: str, **kwargs: Any) -> str:
        composing.set()
        await release.wait()
        return await session_send(self, text, **kwargs)

    async def _counted(self: ChatMirror) -> None:
        """Both paths run to completion before the count reaches two, so the
        wait below is on the code itself rather than on a stopwatch."""
        await close(self)
        stamped.append(None)

    async with _rig(tmp_path) as rig:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(ChatSession, "send_prompt", _slow_send)
            patch.setattr(ChatMirror, "_close_stopped_turn", _counted)
            await rig.relay(_prompt("count the mentions", "c0", seq=1))
            await _settle(composing.is_set, "the turn to start being composed")
            await rig.relay(STOP)
            release.set()
            await _settle(lambda: len(stamped) == 2, "both paths to stamp the end of the turn")

        # Both paths have run and both have finished whatever they published.
        # The sentinel goes on the same queue behind them, so once it is out,
        # everything either of them sent is out — and the count is final.
        await rig.adapter.feed(_sentinel())
        await _settle(
            lambda: _terminals_before_the_sentinel(rig) is not None, "the sentinel to come through"
        )
        assert _terminals_before_the_sentinel(rig) == ["aborted"]


@pytest.mark.parametrize(
    "window",
    [
        pytest.param("preparing", id="stopped-while-the-message-was-being-prepared"),
        pytest.param("in_flight_send", id="stopped-while-the-send-was-in-flight"),
        pytest.param("dispatched", id="stopped-just-after-the-harness-took-it"),
        pytest.param("mid_turn", id="stopped-well-into-the-turn"),
    ],
)
async def test_every_window_a_stop_lands_in_stamps_at_most_one_terminal(
    tmp_path: Path, window: str
) -> None:
    """The same count, from every depth a Stop can reach the box at. A message
    still in the lane has no turn to end and stamps nothing at all; one the
    harness has is ended exactly once."""
    fetching = asyncio.Event()
    release = asyncio.Event()

    async def _prepare(_chat_id: str, _relay: Mapping[str, Any]) -> MaterializedAttachments:
        fetching.set()
        await release.wait()
        return MaterializedAttachments()

    composing = asyncio.Event()
    send_release = asyncio.Event()
    session_send = ChatSession.send_prompt

    async def _slow_send(self: ChatSession, text: str, **kwargs: Any) -> str:
        composing.set()
        await send_release.wait()
        return await session_send(self, text, **kwargs)

    hold = window == "preparing"
    async with _rig(tmp_path, prepare_attachments=_prepare if hold else None) as rig:
        if hold:
            await rig.relay(_prompt("count the mentions", "c0", seq=1))
            await _settle(fetching.is_set, "the lane to start preparing")
        elif window == "in_flight_send":
            # The one window where BOTH stamping paths run, so the one where a
            # turn can be ended twice.
            stamped: list[None] = []
            close = ChatMirror._close_stopped_turn

            async def _counted(self: ChatMirror) -> None:
                await close(self)
                stamped.append(None)

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(ChatSession, "send_prompt", _slow_send)
                patch.setattr(ChatMirror, "_close_stopped_turn", _counted)
                await rig.relay(_prompt("count the mentions", "c0", seq=1))
                await _settle(composing.is_set, "the turn to start being composed")
                await rig.relay(STOP)
                send_release.set()
                # Both paths, run to completion — the second is the one that
                # could stamp the turn a second time.
                await _settle(lambda: len(stamped) == 2, "both paths to stamp the end")
            await rig.adapter.feed(_sentinel())
            await _settle(
                lambda: _terminals_before_the_sentinel(rig) is not None,
                "the sentinel to come through",
            )
            assert _terminals_before_the_sentinel(rig) == ["aborted"]
            return
        else:
            await rig.run_first_turn()
            if window == "mid_turn":
                await rig.adapter.feed(
                    SessionStatusChanged(
                        event_id="running-1",
                        time=datetime.now(UTC),
                        session_id=CHAT_ID,
                        status="running",
                        phase="awaiting_llm",
                        turn_id="turn-1",
                    )
                )
                await _settle(
                    lambda: rig.published() == ["session.status_changed"], "the turn to be live"
                )
        rig.forget_published()

        await rig.relay(STOP)
        release.set()

        await rig.adapter.feed(_sentinel())
        await _settle(
            lambda: _terminals_before_the_sentinel(rig) is not None, "the sentinel to come through"
        )
        assert _terminals_before_the_sentinel(rig) == ([] if hold else ["aborted"])


async def test_the_turn_publishes_nothing_while_the_cancel_is_still_in_flight(
    tmp_path: Path,
) -> None:
    """The rows of the live check's dispatched attempt, in the order they came.

    Cancelling is a round trip to the agent. The turn already in flight keeps
    the wire across it — its ``running``, its echo of the message, the shell of
    the answer it was about to write — and every one of those landed on the
    chat while the stop was still waiting for the adapter (seq 118..127 of that
    attempt, between the stop's own note at 117 and the terminal at 129). The
    reader was then shown a message marked "not sent" with the beginning of an
    answer underneath it.

    Nothing about the decision needs the adapter: it is made before the wait.
    """
    cancelling = asyncio.Event()
    finish_cancel = asyncio.Event()
    adapter_cancel = FakeAdapter.cancel

    async def _slow_cancel(self: FakeAdapter) -> None:
        cancelling.set()
        await finish_cancel.wait()
        await adapter_cancel(self)

    async with _rig(tmp_path) as rig:
        await rig.run_first_turn()
        rig.forget_published()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(FakeAdapter, "cancel", _slow_cancel)
            stop = asyncio.create_task(rig.relay(STOP))
            await _settle(cancelling.is_set, "the cancel to be on its way to the agent")

            # What the harness goes on publishing while the cancel is in the
            # air: the turn it had already started.
            await rig.adapter.feed(
                SessionStatusChanged(
                    event_id="running-in-flight",
                    time=datetime.now(UTC),
                    session_id=CHAT_ID,
                    status="running",
                    phase="awaiting_llm",
                    turn_id="turn-1",
                )
            )
            await rig.adapter.feed_many(_assistant_step("in-flight"))
            finish_cancel.set()
            await stop

        await rig.adapter.feed(_sentinel())
        await _settle(
            lambda: any(row["event_id"] == SENTINEL for row in rig.entries()),
            "the sentinel to come through",
        )
        assert set(rig.published()) == {"session.status_changed"}, (
            "the chat hears the end of the turn and nothing the turn was writing"
        )
