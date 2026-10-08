"""What the catch-up read may take a transcript row to be.

A chat's transcript is the durable record of what still needs an answer, and
the mirror re-reads it on every reconnect. Two different producers write
``role="user"`` rows into it:

* the SERVER, when a person posts a message — a :class:`ChatPromptRecord`
  (``payload.kind == "prompt"``), the question the box has to run;
* the MACHINE, when the harness echoes the message it accepted back onto the
  transcript as its own ``message.created`` event — a published envelope whose
  ``payload.kind`` is the harness event type.

The echo answers nothing, so the watermark (which counts only what the machine
published after a question) steps over it — and it is the LAST row of the chat
whenever a box dies between taking a question and publishing the first part of
its answer, which is exactly what the kill drill does. Read as a prompt record
it raised out of the pending comprehension, so the whole pass died and that
chat could never be caught up again.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import CATCH_UP_RETRY_SECONDS, ChatMirror
from alkera_cli.cloud.publish import RoleIndex, append_entry
from alkera_cli.harness import ChatSession, HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    PROMPT_CANCELLED_STOPPED,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PermissionOption,
    PermissionRequest,
    PermissionResolved,
    PromptCancelled,
    SessionStatusChanged,
    TextPart,
    ToolCall,
    ToolCallUpdate,
)
from alkera_core.schemas.objects import ChatPromptRecord, aside_note_id, prompt_cancelled_event_id

_T = datetime(2026, 9, 6, tzinfo=UTC)
CHAT_ID = "chat-catch-up"
MEMBER = "00000000-0000-4000-8000-000000000001"


class _Transcript:
    """A REST stand-in serving one chat's transcript, paged the way the real
    route pages it, and recording every read."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.reads: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        after = int(request.url.params.get("after_seq", 0))
        limit = int(request.url.params.get("limit", 100))
        self.reads.append(after)
        page = [row for row in self.rows if row["seq"] > after][:limit]
        return httpx.Response(
            200,
            json={
                "items": page,
                "next_after_seq": page[-1]["seq"] if page else after,
                "resync_from": None,
            },
        )


def _row(seq: int, *, role: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"row-{seq}",
        "chat_id": CHAT_ID,
        "seq": seq,
        "role": role,
        "kind": str(payload.get("kind") or ""),
        "event_id": f"e-{seq}",
        "payload": payload,
        "created_at": _T.isoformat(),
    }


def _asked(seq: int, text: str, *, client_id: str) -> dict[str, Any]:
    """A person's message, as the server records it."""
    return _row(
        seq,
        role="user",
        payload=ChatPromptRecord(text=text, client_id=client_id, user_id=MEMBER).model_dump(
            mode="json"
        ),
    )


def _published(seq: int, event: Any) -> dict[str, Any]:
    """A harness event, as the machine publishes it and the server stores it —
    the entry envelope, with the event itself under ``payload``."""
    entry = append_entry(event, RoleIndex())
    return _row(seq, role=entry["role"], payload=entry)


def _echo(seq: int, message_id: str = "m-lost") -> dict[str, Any]:
    """The harness's own echo of the message it just accepted."""
    return _published(
        seq,
        MessageCreated(
            event_id=f"created-{message_id}",
            time=_T,
            session_id=CHAT_ID,
            message_id=message_id,
            role="user",
        ),
    )


def _machine_note(seq: int, status: str = "running") -> dict[str, Any]:
    return _published(
        seq,
        SessionStatusChanged(
            event_id=f"status-{seq}", time=_T, session_id=CHAT_ID, status=status, phase="idle"
        ),
    )


def _answer(seq: int, text: str = "answered") -> dict[str, Any]:
    return _published(
        seq,
        PartCreated(
            event_id=f"part-{seq}",
            time=_T,
            session_id=CHAT_ID,
            part=TextPart(part_id=f"p-{seq}", message_id="m-assistant", text=text),
        ),
    )


def _mirror(tmp_path: Path, transcript: _Transcript) -> ChatMirror:
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=FakeAdapterFactory(FakeAdapter)
    )
    rest = CloudRestClient(
        api_url="http://transcript.test",
        token="device-jwt",
        agent_id=CHAT_ID,
        transport=httpx.MockTransport(transcript),
    )
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=rest,
        user_id=MEMBER,
        owner_user_id=MEMBER,
    )
    # A caught-up question rides the same lane a live relay does; these tests
    # read what the pass puts on that lane, so the harness itself stays out.
    mirror._ready.set()
    mirror._session = cast(ChatSession, object())
    return mirror


def _queued(mirror: ChatMirror) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while not mirror._prompts.empty():
        out.append(mirror._prompts.get_nowait())
    return out


def test_the_harness_echo_is_a_user_row_that_is_not_a_prompt_record() -> None:
    """The premise, pinned at the producer: the machine's echo of a person's
    message lands as a user-role row whose payload is not a prompt record."""
    echo = _echo(1)
    assert echo["role"] == "user"
    assert echo["payload"]["kind"] == "message.created"
    with pytest.raises(ValueError):
        ChatPromptRecord.model_validate(echo["payload"])


async def test_a_question_taken_but_never_answered_still_runs_after_the_echo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The kill drill's tail: the box took the question, the harness echoed it
    onto the transcript, and the machine died before publishing one part of an
    answer — so a user-role row that is not a prompt is the LAST thing in the
    chat, above the watermark. The question is still outstanding, and catching
    up is what runs it."""
    transcript = _Transcript(
        [
            _machine_note(1, "idle"),
            _asked(2, "how many prompts today?", client_id="c-lost"),
            _echo(3),
        ]
    )
    mirror = _mirror(tmp_path, transcript)
    with caplog.at_level(logging.INFO, logger="alkera_cli.cloud.mirror"):
        assert await mirror.catch_up() == 1
    queued = _queued(mirror)
    assert [q["text"] for q in queued] == ["how many prompts today?"]
    assert queued[0]["client_id"] == "c-lost"
    assert queued[0]["user_id"] == MEMBER, "the turn belongs to the member who asked"
    assert queued[0]["seq"] == 2
    assert mirror._consumed_seq == 3, "the pass still records where the transcript stands"


async def test_a_chat_holding_only_its_first_question_runs_it_when_a_box_adopts_it(
    tmp_path: Path,
) -> None:
    """The chat opened while the org had no machine: one recorded question
    (the chat row says ``last_seq = 1``), nothing published, no box has ever
    held it. The box the backend binds it to opens the mirror and the first
    pass must run that question — it is the whole reason the chat was bound."""
    transcript = _Transcript([_asked(1, "what did the box miss?", client_id="c-first")])
    mirror = _mirror(tmp_path, transcript)
    assert await mirror.catch_up(last_seq=1) == 1
    queued = _queued(mirror)
    assert [(q["text"], q["client_id"], q["seq"]) for q in queued] == [
        ("what did the box miss?", "c-first", 1)
    ]
    assert mirror._consumed_seq == 1
    # The row's counter has not moved since: the next pass runs nothing again.
    assert await mirror.catch_up(last_seq=1) == 0
    assert _queued(mirror) == []


async def test_a_question_the_machine_answered_is_not_re_run_because_of_the_echo(
    tmp_path: Path,
) -> None:
    """The other direction. The same echo sits above the watermark, but the
    question below it was answered — nothing is outstanding, and a pass that
    took the echo for a question would ask the harness to answer itself."""
    transcript = _Transcript(
        [_asked(1, "already answered", client_id="c-old"), _answer(2), _echo(3, "m-old")]
    )
    mirror = _mirror(tmp_path, transcript)
    assert await mirror.catch_up() == 0
    assert _queued(mirror) == []
    assert mirror._consumed_seq == 3


async def test_the_live_boxs_tail_is_read_without_taking_the_pass_down(tmp_path: Path) -> None:
    """The exact shape the demo box crashed on, twelve times over: the
    machine's own session rows for the turn, then the harness echo, then
    nothing. The question below them is under the watermark (the machine
    published after it), so nothing is outstanding — but the read itself must
    survive, because a pass that raises here can never catch that chat up
    again, not even on the next message."""
    transcript = _Transcript(
        [
            _asked(1, "what happened here?", client_id="c-demo"),
            _machine_note(2, "running"),
            _machine_note(3, "running"),
            _echo(4, "m-demo"),
        ]
    )
    mirror = _mirror(tmp_path, transcript)
    assert await mirror.catch_up() == 0
    assert _queued(mirror) == []
    assert mirror._consumed_seq == 4


async def test_one_unreadable_row_never_takes_the_whole_pass_down(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A row that claims to be a prompt and is not — a writer this box does not
    know, or a corrupted payload — is logged and stepped over. The question
    beside it still runs: one bad row must never cost a chat its catch-up."""
    transcript = _Transcript(
        [
            _machine_note(1, "idle"),
            _row(2, role="user", payload={"kind": "prompt", "text": ["not", "a", "string"]}),
            _asked(3, "and this one still runs", client_id="c-good"),
        ]
    )
    mirror = _mirror(tmp_path, transcript)
    with caplog.at_level(logging.INFO, logger="alkera_cli.cloud.mirror"):
        assert await mirror.catch_up() == 1
    assert [q["text"] for q in _queued(mirror)] == ["and this one still runs"]
    assert any("could not be read" in record.getMessage() for record in caplog.records)


async def test_a_transcript_longer_than_any_page_cap_is_read_to_its_end(
    tmp_path: Path,
) -> None:
    """A turn may run for days, and a chat that ran one says far more than a
    handful of pages' worth of rows. The catch-up follows the cursor to the
    END: the question waiting at the bottom of a six-thousand-row transcript
    is run, not silently left there because the read stopped paging.
    """
    rows = [_machine_note(seq, "running") for seq in range(1, 6_000)]
    rows.append(_asked(6_000, "did you ever finish?", client_id="c-tail"))
    transcript = _Transcript(rows)
    mirror = _mirror(tmp_path, transcript)

    assert await mirror.catch_up() == 1

    assert [q["text"] for q in _queued(mirror)] == ["did you ever finish?"]
    assert mirror._consumed_seq == 6_000
    # The last read came back short, which is how the walk knows it is done.
    assert transcript.reads[-1] == 6_000


async def test_a_long_transcript_is_walked_to_the_end_without_being_held(
    tmp_path: Path,
) -> None:
    """A turn that publishes every part, every tool call and every status change
    for days is exactly the chat a restart has to resume, so the walk runs to
    the very end. What it HOLDS is the bounded thing: the reader's own rows and
    the rows an ask is read out of, never the transcript.

    Twenty thousand rows, of which four are a reader's messages. The pass must
    see the last one and keep a couple of dozen rows, not twenty thousand.
    """
    rows: list[dict[str, Any]] = []
    for seq in range(1, 20_001):
        if seq % 5_000 == 0:
            rows.append(_asked(seq, f"question {seq}", client_id=f"c-{seq}"))
        else:
            rows.append(_answer(seq))
    transcript = _Transcript(rows)
    mirror = _mirror(tmp_path, transcript)

    read = await mirror._transcript_since(0)

    assert read.rows == 20_000, "the walk stopped short of the end of the transcript"
    assert read.highest == 20_000
    assert read.answered_to == 19_999, "the last row is the reader's, not the machine's"
    assert len(read.users) == 4, "only a reader's own rows are worth keeping whole"
    assert not read.asks, "nothing here is an ask, so nothing is kept for one"
    assert len(read.users) + len(read.asks) < 50, "the read held the transcript, not the fold"


async def test_the_last_question_of_a_long_transcript_is_still_the_one_that_runs(
    tmp_path: Path,
) -> None:
    """The fold is not allowed to change the answer. A question below the
    machine's watermark is answered and must not re-run; one above it is
    outstanding and must — whichever page of a long transcript it sits on."""
    rows: list[dict[str, Any]] = [_asked(1, "answered long ago", client_id="c-old")]
    rows.extend(_answer(seq) for seq in range(2, 12_000))
    rows.append(_asked(12_000, "and this one nobody answered", client_id="c-new"))
    transcript = _Transcript(rows)
    mirror = _mirror(tmp_path, transcript)

    assert await mirror.catch_up() == 1
    assert [(q["text"], q["seq"]) for q in _queued(mirror)] == [
        ("and this one nobody answered", 12_000)
    ]
    assert mirror._consumed_seq == 12_000


# -- what a long turn costs the box -----------------------------------------


def _gated_call(seq: int, call_id: str, *, status: str = "running") -> dict[str, Any]:
    """A tool call announced on the transcript, with its input."""
    return _published(
        seq,
        ToolCall(
            event_id=f"call-{call_id}",
            time=_T,
            session_id=CHAT_ID,
            tool_call_id=call_id,
            provider_call_id=f"prov-{call_id}",
            message_id="m-assistant",
            tool_name="write",
            input={"path": f"/w/{call_id}.txt", "content": "x" * 512},
            status=status,
        ),
    )


def _call_finished(seq: int, call_id: str) -> dict[str, Any]:
    """The update that ends a call: whoever it was waiting on is done."""
    return _published(
        seq,
        ToolCallUpdate(
            event_id=f"done-{call_id}",
            time=_T,
            session_id=CHAT_ID,
            tool_call_id=call_id,
            status="completed",
            output="ok",
        ),
    )


def _ask(seq: int, request_id: str, call_id: str) -> dict[str, Any]:
    """A permission ask for the write ``call_id`` makes, already on a card."""
    return _published(
        seq,
        PermissionRequest.model_validate(
            {
                "event_id": f"ask-{request_id}",
                "time": _T,
                "session_id": CHAT_ID,
                "request_id": request_id,
                "tool_call_id": call_id,
                "permission_kind": "edit",
                "canonical_kind": "edit",
                "prompting": True,
                "subject": {
                    "capability": "fs",
                    "effect": "write",
                    "operation": "edit",
                    "raw": f"/w/{call_id}.txt",
                    "targets": [{"kind": "file", "name": f"/w/{call_id}.txt"}],
                    "classifier": "opencode-tool",
                },
                "options": [
                    PermissionOption(option_id="allow_once", name="Allow").model_dump(mode="json"),
                    PermissionOption(option_id="reject_once", name="Reject").model_dump(
                        mode="json"
                    ),
                ],
            }
        ),
    )


def _settled(seq: int, request_id: str) -> dict[str, Any]:
    """The machine's own resolution: its harness has acted, the ask is done."""
    return _published(
        seq,
        PermissionResolved(
            event_id=f"resolved-{request_id}",
            time=_T,
            session_id=CHAT_ID,
            request_id=request_id,
            option_id="allow_once",
            decided_by="policy",
        ),
    )


async def test_a_turn_of_fifty_thousand_calls_folds_to_the_asks_still_open(
    tmp_path: Path,
) -> None:
    """A turn is unbounded — days long, hundreds of thousands of tool calls,
    each row as large as the entry cap allows — and a box resumes one exactly
    when it must not die. Every one of those rows is ask-shaped, so a fold that
    keeps them all keeps the transcript under another name, on every mirror at
    once.

    Twelve and a half thousand writes, each announced, asked about, allowed and
    finished, and two at the end still waiting on a person. What the walk holds
    is those two.
    """
    rows: list[dict[str, Any]] = []
    seq = 0
    for index in range(12_500):
        call = f"c-{index}"
        for row in (
            _gated_call(seq + 1, call),
            _ask(seq + 2, f"req-{index}", call),
            _settled(seq + 3, f"req-{index}"),
            _call_finished(seq + 4, call),
        ):
            rows.append(row)
        seq += 4
    for open_index in (99_001, 99_002):
        rows.append(_gated_call(seq + 1, f"c-{open_index}"))
        rows.append(_ask(seq + 2, f"req-{open_index}", f"c-{open_index}"))
        seq += 2
    transcript = _Transcript(rows)
    mirror = _mirror(tmp_path, transcript)

    read = await mirror._transcript_since(0)

    assert read.rows == 50_004, "the walk stopped short of the end of the transcript"
    # O(open), not O(turn): two asks, the two calls they gate, and the provider
    # ids that alias onto those calls.
    assert sorted(read.asks) == ["req-99001", "req-99002"]
    assert sorted(read.calls) == ["c-99001", "c-99002"]
    assert sorted(read.aliases) == ["prov-c-99001", "prov-c-99002"]
    assert sorted(read.announced) == ["req-99001", "req-99002"]
    assert read.recorded == {}
    held = len(read.asks) + len(read.calls) + len(read.aliases) + len(read.users)
    assert held <= 8, f"the fold held {held} entries out of {read.rows} rows"

    # And the fold did not change the answer: the two open asks are re-offered,
    # each under the id its card carries, and nothing else is.
    assert sorted(mirror._rearm_asks(read)) == ["req-99001", "req-99002"]
    assert sorted(mirror._interrupts) == ["req-99001", "req-99002"]


async def test_an_ask_its_own_harness_settled_is_not_re_offered(tmp_path: Path) -> None:
    """The asymmetric case the drop must get right: a resolution the MACHINE
    published retires the ask, so a walk that reaches both re-offers nothing —
    while a reader's recorded answer is carried to the end and applied."""
    transcript = _Transcript(
        [
            _gated_call(1, "c-1"),
            _ask(2, "req-1", "c-1"),
            _settled(3, "req-1"),
            _call_finished(4, "c-1"),
        ]
    )
    mirror = _mirror(tmp_path, transcript)

    read = await mirror._transcript_since(0)

    assert read.asks == {}
    assert read.calls == {}
    assert read.announced == set()
    assert mirror._rearm_asks(read) == []
    assert mirror._interrupts == {}


async def test_a_call_still_running_is_kept_for_the_ask_that_names_it(
    tmp_path: Path,
) -> None:
    """The negative of the drop: a call is forgotten because it FINISHED, not
    because an update went by. An ask whose call is still running keeps the
    name and input its card is drawn from."""
    transcript = _Transcript(
        [
            _gated_call(1, "c-1"),
            _published(
                2,
                ToolCallUpdate(
                    event_id="running-c-1",
                    time=_T,
                    session_id=CHAT_ID,
                    tool_call_id="c-1",
                    status="running",
                    input={"path": "/w/c-1.txt", "content": "filled in later"},
                ),
            ),
            _ask(3, "req-1", "c-1"),
        ]
    )
    mirror = _mirror(tmp_path, transcript)

    read = await mirror._transcript_since(0)

    assert list(read.asks) == ["req-1"]
    assert read.calls["c-1"][0] == "write"
    assert read.calls["c-1"][1]["content"] == "filled in later"
    assert read.aliases == {"prov-c-1": "c-1"}


# ---------------------------------------------------------------------------
# A note the box wrote about ITSELF answers nothing — and the server agrees
# ---------------------------------------------------------------------------


def _aside(
    seq: int, text: str = "The workspace restarted while it was answering."
) -> list[dict[str, Any]]:
    """The note the box writes about itself after a restart: three rows whose
    event ids come from an aside id. The same rows drive the server's side of
    this in ``apps/backend/tests/test_chats_api.py``."""
    note = aside_note_id("restart1")
    return [
        _entry(
            seq,
            MessageCreated(
                event_id=f"{note}-created",
                time=_T,
                session_id=CHAT_ID,
                message_id=note,
                role="system",
            ),
        ),
        _entry(
            seq + 1,
            PartCreated(
                event_id=f"{note}-text",
                time=_T,
                session_id=CHAT_ID,
                part=TextPart(part_id=f"{note}-part", message_id=note, text=text, synthetic=True),
            ),
        ),
        _entry(
            seq + 2,
            MessageCompleted(
                event_id=f"{note}-done",
                time=_T,
                session_id=CHAT_ID,
                message_id=note,
                finish_reason="error",
            ),
        ),
    ]


def _entry(seq: int, event: Any) -> dict[str, Any]:
    """A published row that keeps the event's OWN id, which is what tells a
    reader whose note it is — ``_published`` stamps a synthetic one."""
    row = _published(seq, event)
    row["event_id"] = str(event.event_id)
    return row


def _server_cancelled(seq: int, message_id: str, client_id: str) -> dict[str, Any]:
    """The row the SERVER writes for a message a Stop means was never run."""
    return _entry(
        seq,
        PromptCancelled(
            event_id=prompt_cancelled_event_id(message_id),
            time=_T,
            session_id=CHAT_ID,
            message_id=message_id,
            client_id=client_id,
            reason=PROMPT_CANCELLED_STOPPED,
        ),
    )


async def test_the_boxs_own_restart_note_does_not_answer_the_message_below_it(
    tmp_path: Path,
) -> None:
    """The premise the server's rule is aligned to, proved on the box's side:
    a note the box wrote ABOUT ITSELF reports on the box, not on the question.
    The question is still owed a turn, and the box that came back runs it —
    which is exactly why a Stop has to be able to reach it."""
    transcript = _Transcript([_asked(1, "count the mentions", client_id="c-restart"), *_aside(2)])
    mirror = _mirror(tmp_path, transcript)

    assert await mirror.catch_up() == 1
    assert [q["client_id"] for q in _queued(mirror)] == ["c-restart"]


async def test_a_message_the_server_says_was_never_run_is_not_re_run_after_a_restart(
    tmp_path: Path,
) -> None:
    """The fix, end to end on the box's side. The same transcript, with the
    Stop the reader pressed on it: the server's "never run" row is not an
    aside, so it answers the message under it and the box that comes back
    leaves it alone. Before the two rules agreed, the restart note suppressed
    the server's row entirely and this message ran after it was stopped."""
    transcript = _Transcript(
        [
            _asked(1, "count the mentions", client_id="c-restart"),
            *_aside(2),
            _server_cancelled(5, "usr:c-restart", "c-restart"),
        ]
    )
    mirror = _mirror(tmp_path, transcript)

    assert await mirror.catch_up() == 0
    assert _queued(mirror) == [], "a stopped message is not re-run by the box that comes back"
    assert mirror._consumed_seq == 5


# -- a catch-up whose read failed tries again --------------------------------


class _Refusing(_Transcript):
    """The transcript route answering 429 to the first ``refusals`` reads."""

    def __init__(self, rows: list[dict[str, Any]], *, refusals: int) -> None:
        super().__init__(rows)
        self.refusals = refusals

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.refusals > 0:
            self.refusals -= 1
            self.reads.append(-1)
            return httpx.Response(429, json={"code": "rate_limited", "message": "slow down"})
        return super().__call__(request)


def _retrying_mirror(tmp_path: Path, transcript: _Transcript, waits: list[float]) -> ChatMirror:
    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=FakeAdapterFactory(FakeAdapter)
    )
    rest = CloudRestClient(
        api_url="http://transcript.test",
        token="device-jwt",
        agent_id=CHAT_ID,
        transport=httpx.MockTransport(transcript),
    )
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=rest,
        user_id=MEMBER,
        owner_user_id=MEMBER,
        sleep=_sleep,
    )
    mirror._ready.set()
    mirror._session = cast(ChatSession, object())
    return mirror


async def _settle(mirror: ChatMirror) -> None:
    while mirror._relay_tasks:
        await asyncio.gather(*list(mirror._relay_tasks))


@pytest.mark.parametrize("refusals", [1, 3], ids=["one-refusal", "three-refusals"])
async def test_a_first_question_whose_catch_up_read_was_refused_still_runs(
    tmp_path: Path, refusals: int
) -> None:
    """A new chat's first question reaches the box only through the catch-up
    read, and the read was answered 429. Nothing else names the chat again
    until the person types a second message, so the pass itself must read
    again — on a widening wait — and run the question."""
    transcript = _Refusing([_asked(1, "first question", client_id="c-first")], refusals=refusals)
    waits: list[float] = []
    mirror = _retrying_mirror(tmp_path, transcript, waits)
    mirror.request_catch_up()
    await _settle(mirror)
    assert [q["text"] for q in _queued(mirror)] == ["first question"]
    assert waits == list(CATCH_UP_RETRY_SECONDS[:refusals])
    assert transcript.reads.count(-1) == refusals


async def test_a_read_that_never_succeeds_gives_up_after_the_last_wait(tmp_path: Path) -> None:
    transcript = _Refusing([_asked(1, "q", client_id="c")], refusals=10_000)
    waits: list[float] = []
    mirror = _retrying_mirror(tmp_path, transcript, waits)
    mirror.request_catch_up()
    await _settle(mirror)
    assert waits == list(CATCH_UP_RETRY_SECONDS)
    assert _queued(mirror) == []


async def test_a_read_that_succeeds_first_time_waits_for_nothing(tmp_path: Path) -> None:
    transcript = _Refusing([_asked(1, "q", client_id="c")], refusals=0)
    waits: list[float] = []
    mirror = _retrying_mirror(tmp_path, transcript, waits)
    mirror.request_catch_up()
    await _settle(mirror)
    assert waits == []
    assert [q["text"] for q in _queued(mirror)] == ["q"]
