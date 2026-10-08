"""A pending ask is state of the chat, not of the process that raised it.

A web chat's permission and question asks wait on a person who may not be
watching — for an hour, overnight, over a weekend. Three things follow, and
each is pinned here against the real mirror with a ``FakeAdapter`` behind it:

* no clock kills a turn for waiting: the budget meter is held while an ask is
  parked and the default budget has no wall clock at all;
* the ask survives the process: a mirror that opens the chat after a kill, a
  sleep or a move reads the transcript back and re-offers the SAME ask (same
  request id, the ``prompting`` copy the browser draws controls from), and the
  answer resolves it — an allow continues the turn on the restored session, a
  reject ends it with the decision on the transcript;
* a chat parked on a person may sleep, because nothing a sleep destroys is
  needed to answer it.

The transcript is served by a mock of ``GET /chats/{id}/messages`` whose rows
are exactly what the first mirror put on its outbound lane — the shape the
cloud persists — so the second mirror reads what the first one wrote.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.activity import ChatActivity
from alkera_cli.cloud.budget import TurnBudget
from alkera_cli.cloud.mirror import (
    CONTINUE_TEXT,
    DENIED_BY_USER,
    NOT_RUN_BEFORE_RESTART,
    TRANSCRIPT_MISMATCH,
    TURN_LOST_TO_RESTART,
    ChatMirror,
    turn_stopped_after_tools,
)
from alkera_cli.cloud.publish import RoleIndex, append_entry
from alkera_cli.cloud.transport import DocHandle
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.harness import HarnessRuntime, PermissionBroker, QuestionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.host import paths
from alkera_cli.plugins.plugin_base.permissions.config import (
    PermissionRule,
    PermissionsConfig,
    save_permissions,
)
from alkera_core.authz.headers import agent_headers
from alkera_core.project.chats.trace import pin_trace
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    MessageCompleted,
    PermissionOption,
    PermissionRequest,
    PermissionResolved,
    QuestionAnswered,
    QuestionPrompt,
    QuestionRejected,
    QuestionRequest,
    SessionStatusChanged,
    ToolCall,
    ToolCallUpdate,
)
from alkera_core.schemas.objects.transcript import (
    RECORDED_ANSWER_ROLE,
    recorded_answer_entry,
    recorded_answer_event_id,
)
from tests._live_window import Window, live_window

_T = datetime(2026, 9, 15, tzinfo=UTC)
CHAT_ID = "chat-durable"
OWNER = "00000000-0000-4000-8000-000000000001"

#: The backstop on a wait for something the mirror has yet to publish.
#:
#: What such a wait is on is one task's next turn on this loop — the mirror's
#: pump, moving an event the harness emitted onto the outbound lane. The work
#: is microseconds; the wait for the turn is the runner's to give, and a shard
#: running thirty-two workers on one box withholds it for longer than any fixed
#: window a reader would think to write. The wait ends the moment the entry
#: lands, so the allowance here only says how much of the host's load the
#: backstop absorbs before it calls the claim broken.
_WIRE_WINDOW = live_window(10.0)


class _Clock:
    def __init__(self) -> None:
        self.at = 0.0

    def __call__(self) -> float:
        return self.at


class _Transcript:
    """The cloud's durable record of one chat, as ``GET /chats/{id}/messages``
    serves it: every entry a mirror published, in order, each with its seq."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        #: What ``GET /chats/{id}/send-admission`` answers; ``None`` is a
        #: server from before the check (404).
        #: An ``int`` is an error status with no decision in its body.
        self.admission: dict[str, Any] | int | None = None
        self.admission_asked: list[str] = []
        #: The headers each admission ask carried: whose identity it spoke as.
        self.admission_headers: list[dict[str, str]] = []

    def append(self, entry: dict[str, Any], *, role: str | None = None) -> None:
        seq = len(self.rows) + 1
        self.rows.append(
            {
                "id": f"row-{seq}",
                "seq": seq,
                "role": role or entry.get("role") or "assistant",
                "kind": entry.get("kind"),
                "payload": entry,
            }
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/messages"):
            after = int(request.url.params.get("after_seq", "0"))
            items = [r for r in self.rows if r["seq"] > after]
            return httpx.Response(200, json={"items": items, "next_after_seq": None})
        if request.method == "GET" and request.url.path.endswith("/send-admission"):
            self.admission_asked.append(request.url.params.get("user_id", ""))
            self.admission_headers.append(dict(request.headers))
            if isinstance(self.admission, int):
                return httpx.Response(self.admission, json={"detail": "Refused"})
            if self.admission is not None:
                return httpx.Response(200, json=self.admission)
        return httpx.Response(404, json={})

    def kinds(self, request_id: str) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        for row in self.rows:
            payload = row["payload"].get("payload", {})
            if payload.get("request_id") == request_id:
                out.append((str(row["kind"]), payload))
        return out


class _Box:
    """One life of a box serving the chat."""

    def __init__(
        self,
        mirror: ChatMirror,
        adapter: FakeAdapter,
        transcript: _Transcript,
        clock: _Clock,
        pump: asyncio.Task[None],
        runtime: HarnessRuntime,
    ) -> None:
        self.mirror = mirror
        self.adapter = adapter
        self.transcript = transcript
        self.clock = clock
        self._pump = pump
        self._runtime = runtime

    def _record(self, entry: dict[str, Any], out: list[dict[str, Any]]) -> None:
        out.append(entry)
        if "__meta__" not in entry:
            self.transcript.append(entry)

    def _take(self, out: list[dict[str, Any]]) -> None:
        """Everything on the lane right now, appended to the transcript the way
        the gateway persists it (meta frames excepted)."""
        while not self.mirror._outbound.empty():
            self._record(self.mirror._outbound.get_nowait(), out)

    async def drain(self, *, seconds: float = 0.5) -> list[dict[str, Any]]:
        """Everything the mirror put on its outbound lane over ``seconds``.

        The lane is read once more after the window closes. A sleep returns
        when the runner gets around to it, not when it was asked to: on a
        loaded box a twenty-millisecond one comes back past the whole window,
        and whatever the mirror published while it was away would otherwise
        never be read at all.
        """
        out: list[dict[str, Any]] = []
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            self._take(out)
            await asyncio.sleep(0.02)
        self._take(out)
        return out

    async def drain_until(
        self,
        satisfied: Callable[[list[dict[str, Any]]], bool],
        *,
        window: Window = _WIRE_WINDOW,
    ) -> list[dict[str, Any]]:
        """Everything the mirror published, up to the entry that satisfies the
        caller — waiting on the lane itself rather than on a clock.

        The wait blocks on the queue, so it ends on the put that satisfies it
        and never one poll later. ``window`` is only the backstop: a claim that
        never comes true fails on the caller's assertion rather than on the
        suite's timeout.
        """
        out: list[dict[str, Any]] = []
        deadline = asyncio.get_running_loop().time() + window.seconds
        while True:
            self._take(out)
            if satisfied(out):
                return out
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return out
            try:
                entry = await asyncio.wait_for(self.mirror._outbound.get(), remaining)
            except TimeoutError:
                self._take(out)
                return out
            self._record(entry, out)

    async def park(self, request: PermissionRequest | QuestionRequest) -> None:
        """Feed the ask and wait for the mirror to park it on a reader.

        The first ask a process decides imports the permission engine and the
        gate behind it — some forty modules, loaded on the event loop — so on a
        loaded runner the first park of a worker trails the feed by seconds
        while every later one takes milliseconds. Nothing here is a claim about
        how fast an ask parks; the window is only the backstop for one that
        never does.
        """
        await self.adapter.feed(request)
        deadline = asyncio.get_running_loop().time() + _WIRE_WINDOW.seconds
        while request.request_id not in self.mirror.pending_interrupts:
            assert asyncio.get_running_loop().time() < deadline, (
                f"the ask was never parked within {_WIRE_WINDOW}"
            )
            await asyncio.sleep(0.02)

    async def kill(self) -> None:
        """The process goes: nothing more is said to the cloud."""
        self._pump.cancel()
        with contextlib.suppress(BaseException):
            await self._pump
        await self.mirror.stop()
        await self._runtime.close_all()


def _narrow_the_sandbox(mirror: ChatMirror) -> None:
    """A write into the chat's working directory is the sandbox's to admit in
    every mode, and the write fence is that same directory — so on a real box no
    file write is ever parked. These tests are about what happens to an ask that
    IS parked, so the sandbox is narrowed to a subdirectory: a write elsewhere in
    the working directory is inside the fence and the mode's business."""
    assert mirror._session is not None and mirror._session.tool_binding is not None
    mirror._session.tool_binding.sandbox_dir = mirror.working_dir / "plans"


async def _boot(
    workspace: Path,
    transcript: _Transcript,
    *,
    budget: TurnBudget | None = None,
    machine_id: str | None = None,
) -> _Box:
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
    clock = _Clock()
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://objects.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(transcript.handler),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
        budget=budget,
        clock=clock,
        machine_id=machine_id,
    )
    broker = PermissionBroker(mirror._resolve_permission, default_timeout_seconds=None)
    questions = QuestionBroker(mirror._resolve_question, default_timeout_seconds=None)
    mirror._session = await mirror.open_session(broker, questions)
    mirror._session._path_fence = None
    _narrow_the_sandbox(mirror)
    mirror._session.set_permission_mode("default")
    mirror._mode = "default"
    pump = asyncio.get_running_loop().create_task(mirror._pump(mirror._session.subscribe()))
    mirror._outbound = asyncio.Queue()
    return _Box(mirror, factory.adapters[0], transcript, clock, pump, runtime)


@pytest.fixture
def _turn_admitted() -> None:
    """This module pins the per-turn send admission, so it meets the real REST
    call through the scripted transport (which answers 404, an old server,
    unless a test sets ``_Transcript.admission``)."""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    home = tmp_path / "alkera-home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    save_permissions(
        work / ".alkera",
        PermissionsConfig(
            rules=[
                PermissionRule(capability="fs", effect=Effect.READ, decision="ask"),
                PermissionRule(capability="fs", effect=Effect.WRITE, decision="ask"),
            ]
        ),
    )
    ProjectDirectory(work / ".alkera").chats().create(
        session_id=CHAT_ID, title="t", harness_type="agent"
    ).close()
    return work


@pytest.fixture
async def box(workspace: Path) -> AsyncIterator[_Box]:
    handle = await _boot(workspace, _Transcript())
    try:
        yield handle
    finally:
        await handle.kill()


def _write_ask(
    workspace: Path,
    *,
    request_id: str = "req-w",
    name: str = "notes.txt",
    tool_call_id: str | None = None,
    provider_call_id: str | None = None,
) -> PermissionRequest:
    """An ask for a write. By default it names the call the way opencode's ask
    does — by the PROVIDER's call id (``call-…``), which is not the id the
    transcript keys the call by (its part, ``prt-…``)."""
    target = str(workspace / ".alkera" / "chats" / CHAT_ID / "scratch" / name)
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_T,
        session_id=CHAT_ID,
        request_id=request_id,
        tool_call_id=tool_call_id if tool_call_id is not None else f"call-{request_id}",
        provider_call_id=provider_call_id,
        permission_kind="edit",
        canonical_kind="edit",
        subject={
            "capability": "fs",
            "effect": "write",
            "operation": "edit",
            "raw": target,
            "targets": [{"kind": "file", "name": target}],
            "classifier": "opencode-tool",
        },
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="allow_always", name="Always"),
            PermissionOption(option_id="reject_once", name="Reject"),
        ],
    )


def _question(request_id: str = "req-q") -> QuestionRequest:
    return QuestionRequest(
        event_id=f"ev-{request_id}",
        time=_T,
        session_id=CHAT_ID,
        request_id=request_id,
        questions=[QuestionPrompt(question="Which region?", custom=True)],
    )


async def _until(condition: Callable[[], bool], *, seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# No clock kills a turn for waiting
# ---------------------------------------------------------------------------


async def test_an_ask_pending_for_an_hour_is_still_alive_and_answerable(
    workspace: Path, box: _Box
) -> None:
    """The five-minute stop that ended every unattended ask is gone: the turn
    is held while the ask waits, the hour costs it nothing, and the answer the
    reader gives when they come back reaches the harness."""
    box.mirror._begin_turn("t1")
    ask = _write_ask(workspace)
    await box.park(ask)
    assert box.mirror.turn_running and box.mirror._meter.waiting

    box.clock.at += 3600.0
    assert box.mirror._meter.check() is None, "an hour parked on a reader is not a stop"
    assert box.mirror.pending_interrupts == [ask.request_id]
    assert not box.mirror.quiescent, "and a chat holding an unanswered ask is not idle"

    box.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_once"})
    await box.adapter.wait_for_permission_reply(ask.request_id, "allow_once")
    assert not box.mirror._meter.waiting
    assert box.mirror._meter.elapsed() < 1.0, "the wait was never the turn's"


async def test_an_answer_from_a_member_who_lost_the_chat_is_not_applied(
    workspace: Path, box: _Box
) -> None:
    """An answer drives the chat as much as a message does: a relayed answer
    whose author the server no longer lets send is refused and said, and the
    ask stays open for someone who may answer it."""
    box.transcript.admission = {"allowed": False, "message": "Your access was removed."}
    box.mirror._begin_turn("t1")
    ask = _write_ask(workspace)
    await box.park(ask)

    await box.mirror._on_prompt(
        {"interrupt_id": ask.request_id, "option_id": "allow_once", "user_id": "member-2"}
    )

    said = await box.drain_until(lambda out: bool(_notes(out)))
    assert _notes(said) == ["This message was not run: Your access was removed."]
    assert box.mirror.pending_interrupts == [ask.request_id]
    assert box.transcript.admission_asked == ["member-2"]


async def test_a_chat_whose_only_work_is_a_parked_ask_says_so(workspace: Path, box: _Box) -> None:
    """An ask parked on a person is the ONE kind of busy the box may put a
    window against: nothing else is in flight, and the ask itself survives a
    sleep in the transcript. The mirror reports that fact; the service is what
    decides how long the window is. A held meter is the turn waiting on the
    reader, not the agent working — so it does not make the chat something
    else."""
    assert not box.mirror.parked_only, "nothing is parked yet"
    assert box.mirror.activity is ChatActivity.IDLE
    box.mirror._begin_turn("t1")
    assert not box.mirror.parked_only, "a turn nobody is waiting on is real work"
    assert box.mirror.activity is ChatActivity.WORKING

    ask = _write_ask(workspace)
    await box.park(ask)
    assert not box.mirror.quiescent
    assert box.mirror.parked_only
    assert box.mirror.activity is ChatActivity.AWAITING_USER

    box.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_once"})
    await box.adapter.wait_for_permission_reply(ask.request_id, "allow_once")
    assert not box.mirror.parked_only, "answered: the agent is working again"
    assert box.mirror.activity is ChatActivity.WORKING


async def test_an_opted_in_wall_clock_still_stops_while_no_ask_is_pending(
    workspace: Path,
) -> None:
    """The negative: a deployment that sets a wall clock gets one — a turn that
    is actually running (no ask parked) still trips it."""
    handle = await _boot(workspace, _Transcript(), budget=TurnBudget(wall_clock_seconds=30))
    try:
        handle.mirror._begin_turn("t1")
        handle.clock.at += 31.0
        exceeded = handle.mirror._meter.check()
        assert exceeded is not None and exceeded.cap == "wall_clock"
    finally:
        await handle.kill()


# ---------------------------------------------------------------------------
# The ask survives the process
# ---------------------------------------------------------------------------


async def test_a_box_killed_with_an_ask_pending_re_offers_the_same_ask_when_it_returns(
    workspace: Path,
) -> None:
    """Kill → restart across a pending ask. The second life reads the
    transcript the first one wrote and parks the SAME request id, tagged for
    the reader — never a retired card, never a fresh ask nobody raised."""
    transcript = _Transcript()
    first = await _boot(workspace, transcript)
    ask = _write_ask(workspace)
    first.mirror._begin_turn("t1")
    await first.park(ask)
    on_the_wire = await first.drain()
    tagged = [
        e
        for e in on_the_wire
        if e.get("kind") == "permission.request" and e["payload"].get("prompting") is True
    ]
    assert len(tagged) == 1, "the first life announced the ask once"
    await first.kill()
    assert [k for k, _ in transcript.kinds(ask.request_id)] == [
        "permission.request",
        "permission.request",
    ], "the kill leaves the ask unresolved in the transcript"

    second = await _boot(workspace, transcript)
    try:
        assert second.mirror.pending_interrupts == []
        assert await second.mirror.catch_up() == 0, "no new question — but the ask is re-armed"
        assert second.mirror.pending_interrupts == [ask.request_id]
        assert second.mirror.waiting_on_a_person
        assert second.mirror.parked_only, (
            "the waiter the re-arm spawned is the ask itself, not other work the chat owes"
        )
        again = await second.drain(seconds=0.2)
        assert not [e for e in again if e.get("kind") == "permission.request"], (
            "an ask the transcript already carries tagged is not announced twice"
        )
    finally:
        await second.kill()


async def test_an_untagged_pending_ask_is_announced_when_re_offered(workspace: Path) -> None:
    """A transcript holding only the harness's own untagged append (the box
    died between the append and the announcement) still comes back as a card
    the reader can answer."""
    transcript = _Transcript()
    ask = _write_ask(workspace, request_id="req-untagged")
    transcript.append(
        {
            "event_id": ask.event_id,
            "role": "system",
            "kind": "permission.request",
            "payload": ask.model_dump(mode="json"),
        }
    )
    handle = await _boot(workspace, transcript)
    try:
        await handle.mirror.catch_up()
        assert handle.mirror.pending_interrupts == [ask.request_id]
        wire = await handle.drain(seconds=0.2)
        announced = [e for e in wire if e.get("kind") == "permission.request"]
        assert len(announced) == 1
        assert announced[0]["payload"]["prompting"] is True
        assert announced[0]["payload"]["request_id"] == ask.request_id
    finally:
        await handle.kill()


@pytest.mark.parametrize(
    "resolution",
    [
        pytest.param(
            {
                "event_type": "permission.resolved",
                "event_id": "ev-res",
                "time": _T.isoformat(),
                "session_id": CHAT_ID,
                "request_id": "req-done",
                "option_id": "allow_once",
                "decided_by": "user",
            },
            id="answered",
        ),
    ],
)
async def test_an_ask_the_transcript_resolves_is_not_re_offered(
    workspace: Path, resolution: dict[str, Any]
) -> None:
    transcript = _Transcript()
    ask = _write_ask(workspace, request_id="req-done")
    for event in (ask.model_dump(mode="json"), resolution):
        transcript.append(
            {
                "event_id": event["event_id"],
                "role": "system",
                "kind": event["event_type"],
                "payload": event,
            }
        )
    handle = await _boot(workspace, transcript)
    try:
        await handle.mirror.catch_up()
        assert handle.mirror.pending_interrupts == []
        assert not [
            e for e in await handle.drain(seconds=0.1) if e.get("kind") == "permission.request"
        ]
    finally:
        await handle.kill()


async def test_allowing_a_restored_ask_records_it_and_continues_the_turn(
    workspace: Path,
) -> None:
    """Sleep with an ask pending → resume → Allow. The decision lands on the
    transcript (so every reader's card retires and no later open re-offers
    it), the restored session is told what was allowed and to carry on, and
    the identical ask the agent raises to do so is granted from that answer —
    the reader is not asked twice for one allow."""
    transcript = _Transcript()
    first = await _boot(workspace, transcript)
    ask = _write_ask(workspace)
    first.mirror._begin_turn("t1")
    await first.park(ask)
    await first.drain()
    await first.kill()

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.catch_up()
        assert second.mirror.pending_interrupts == [ask.request_id]

        second.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_once"})
        await _until(lambda: bool(second.adapter.sent_prompts))
        assert second.mirror.pending_interrupts == []
        wire = await second.drain()
        resolved = [e for e in wire if e.get("kind") == "permission.resolved"]
        assert len(resolved) == 1
        assert resolved[0]["payload"]["request_id"] == ask.request_id
        assert resolved[0]["payload"]["option_id"] == "allow_once"
        assert resolved[0]["payload"]["decided_by"] == "user"

        prompt = second.adapter.sent_prompts[0]
        assert prompt.text == CONTINUE_TEXT
        assert prompt.system is not None and "ALLOWED the edit action" in prompt.system
        assert (
            str(workspace / ".alkera" / "chats" / CHAT_ID / "scratch" / "notes.txt")
            in prompt.system
        )
        assert second.mirror.turn_running, "the continuation is a turn like any other"
        # The agent, told to carry on, raises the very same ask again: granted
        # from the reader's answer, never parked on them a second time.
        again = _write_ask(workspace, request_id="req-w-again")
        await second.adapter.feed(again)
        await second.adapter.wait_for_permission_reply("req-w-again", "allow_once")
        assert second.mirror.pending_interrupts == []
        # …once. A third identical ask is the reader's to decide again.
        third = _write_ask(workspace, request_id="req-w-third")
        await second.park(third)
    finally:
        await second.kill()


async def test_rejecting_a_restored_ask_ends_the_turn_with_the_decision_on_record(
    workspace: Path,
) -> None:
    transcript = _Transcript()
    first = await _boot(workspace, transcript)
    ask = _write_ask(workspace)
    first.mirror._begin_turn("t1")
    await first.park(ask)
    await first.drain()
    await first.kill()

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.catch_up()
        second.mirror._answer_interrupt(ask.request_id, {"option_id": "reject_once"})
        await _until(lambda: ask.request_id not in second.mirror.pending_interrupts)
        wire = await second.drain()
        resolved = [e for e in wire if e.get("kind") == "permission.resolved"]
        assert [r["payload"]["option_id"] for r in resolved] == ["reject_once"]
        idle = [e for e in wire if "__meta__" in e]
        assert idle and idle[-1]["__meta__"]["turn_state"]["state"] == "idle", (
            "the composer is the reader's again"
        )
        assert second.adapter.sent_prompts == [], "a reject continues nothing"
        assert not second.mirror.turn_running
    finally:
        await second.kill()


async def test_a_restored_question_answered_continues_with_the_answer(workspace: Path) -> None:
    transcript = _Transcript()
    first = await _boot(workspace, transcript)
    question = _question()
    first.mirror._begin_turn("t1")
    await first.park(question)
    await first.drain()
    await first.kill()
    assert [k for k, _ in transcript.kinds(question.request_id)] == ["question.request"]

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.catch_up()
        assert second.mirror.pending_interrupts == [question.request_id]
        second.mirror._answer_interrupt(question.request_id, {"answers": [["eu-west"]]})
        await _until(lambda: bool(second.adapter.sent_prompts))
        wire = await second.drain()
        answered = [e for e in wire if e.get("kind") == "question.answered"]
        assert len(answered) == 1 and answered[0]["payload"]["answers"] == [["eu-west"]]
        prompt = second.adapter.sent_prompts[0]
        assert prompt.system is not None and "Which region? -> eu-west" in prompt.system
    finally:
        await second.kill()


async def test_a_restored_question_rejected_ends_the_turn(workspace: Path) -> None:
    transcript = _Transcript()
    question = _question("req-q2")
    transcript.append(
        {
            "event_id": question.event_id,
            "role": "system",
            "kind": "question.request",
            "payload": question.model_dump(mode="json"),
        }
    )
    handle = await _boot(workspace, transcript)
    try:
        await handle.mirror.catch_up()
        handle.mirror._answer_interrupt("req-q2", {"reject": True, "reason": "never mind"})
        await _until(lambda: "req-q2" not in handle.mirror.pending_interrupts)
        wire = await handle.drain()
        rejected = [e for e in wire if e.get("kind") == "question.rejected"]
        assert len(rejected) == 1 and rejected[0]["payload"]["reason"] == "never mind"
        assert handle.adapter.sent_prompts == []
    finally:
        await handle.kill()


async def test_a_live_ask_is_not_re_armed_over_itself_by_a_catch_up(
    workspace: Path, box: _Box
) -> None:
    """The reload case: the box is alive and holding the ask; a catch-up read
    (a reconnect, a discovery tick) sees the same unresolved ask in the
    transcript and must leave the live one alone — one card, one future."""
    ask = _write_ask(workspace)
    await box.park(ask)
    await box.drain()
    live = box.mirror._interrupts[ask.request_id]
    await box.mirror.catch_up()
    assert box.mirror._interrupts[ask.request_id] is live
    assert not live.restored
    assert not [e for e in await box.drain(seconds=0.1) if e.get("kind") == "permission.request"]


@pytest.mark.parametrize("mode", ["read_only", "plan"])
async def test_an_ask_the_sessions_mode_refused_is_not_re_offered_before_its_resolution_lands(
    workspace: Path, box: _Box, mode: str
) -> None:
    """A write ask in an analyst's mode is refused by the harness's own policy,
    here, before any reader is asked. The harness appends the request first and
    the resolution after, so a catch-up read that lands between the two rows
    reaching the transcript sees the ask unresolved. It is still this process's
    ask, already answered: it is never parked for a reader, never tagged
    ``prompting``, and no relayed allow can reach it."""
    assert box.mirror._session is not None
    box.mirror._session.set_permission_mode(cast(Any, mode))
    box.mirror._mode = cast(Any, mode)
    ask = _write_ask(workspace)
    await box.adapter.feed(ask)
    await box.adapter.wait_for_permission_reply(ask.request_id, "reject_once")
    # The fake harness echoes no resolution, so the transcript holds the
    # request alone: exactly what a read between the two rows landing sees.
    published = await box.drain_until(
        lambda out: any(e.get("kind") == "permission.request" for e in out)
    )
    assert [e.get("kind") for e in published if e.get("kind", "").startswith("permission")] == [
        "permission.request"
    ]
    await box.mirror.catch_up()
    assert box.mirror.pending_interrupts == []
    assert not [e for e in await box.drain(seconds=0.1) if e.get("kind") == "permission.request"]
    box.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_once"})
    assert box.adapter.permission_replies == [(ask.request_id, "reject_once")]


async def test_a_record_for_a_live_ask_not_yet_parked_is_held_for_the_park(
    workspace: Path, box: _Box
) -> None:
    """The harness raised the ask here and its policy has not reached the
    broker yet; a reader's answer is already recorded. A catch-up in that
    window neither re-offers the ask nor drops the answer: the answer settles
    the ask the moment the harness parks it, and the tool proceeds once."""
    assert box.mirror._session is not None
    broker = box.mirror._session._broker
    assert broker is not None
    release = asyncio.Event()
    resolve = broker._resolver

    async def gated(request: PermissionRequest) -> Any:
        await release.wait()
        return await resolve(request)

    broker._resolver = gated
    ask = _write_ask(workspace)
    await box.adapter.feed(ask)
    await box.drain_until(lambda out: any(e.get("kind") == "permission.request" for e in out))
    _recorded(box.transcript, _decision(ask.request_id, "allow_once"))

    await box.mirror.catch_up()
    assert box.mirror.pending_interrupts == []
    assert not [e for e in await box.drain(seconds=0.1) if e.get("kind") == "permission.request"]

    release.set()
    await box.adapter.wait_for_permission_reply(ask.request_id, "allow_once")
    assert box.adapter.permission_replies == [(ask.request_id, "allow_once")]
    assert box.mirror.pending_interrupts == []
    assert box.mirror._ignored_relays == 0


async def test_an_ask_just_answered_is_not_rebuilt_before_its_echo_lands(
    workspace: Path, box: _Box
) -> None:
    ask = _write_ask(workspace)
    await box.park(ask)
    await box.drain()
    box.mirror._answer_interrupt(ask.request_id, {"option_id": "reject_once"})
    await box.adapter.wait_for_permission_reply(ask.request_id, "reject_once")
    # The transcript still shows the ask unresolved (the fake harness echoes
    # nothing), exactly the window between an answer and its echo.
    await box.mirror.catch_up()
    assert box.mirror.pending_interrupts == []


def test_the_re_arm_reads_the_persisted_entry_shape_and_a_flat_one() -> None:
    """The row shape is the cloud's, not this test's: the mirror reads a
    machine row (event one level in) and a flat row alike, and ignores the
    rest."""
    from alkera_cli.cloud.mirror import _harness_event_of

    nested = {"payload": {"event_id": "e", "kind": "x", "payload": {"event_type": "x", "a": 1}}}
    flat = {"payload": {"event_type": "y", "b": 2}}
    assert _harness_event_of(nested) == {"event_type": "x", "a": 1}
    assert _harness_event_of(flat) == {"event_type": "y", "b": 2}
    assert _harness_event_of({"payload": {"text": "hi"}}) is None
    assert _harness_event_of({"payload": json.dumps({"event_type": "x"})}) is None


# ---------------------------------------------------------------------------
# An answer recorded while no box held the ask
# ---------------------------------------------------------------------------
#
# ``POST /chats/{id}/answer`` records the reader's decision on the transcript
# as the ask's resolution, under the reader's role, whether or not a box is
# holding the ask. The rows below are that record, spelled by the same
# api-core helper the route uses.


def _recorded(
    transcript: _Transcript, event: PermissionResolved | QuestionAnswered | QuestionRejected
) -> None:
    transcript.append(recorded_answer_entry(event))


def _decision(request_id: str, option: str) -> PermissionResolved:
    return PermissionResolved.model_validate(
        {
            "event_id": recorded_answer_event_id(request_id),
            "time": _T,
            "session_id": CHAT_ID,
            "request_id": request_id,
            "option_id": option,
            "decided_by": "user",
        }
    )


async def _killed_mid_ask(
    workspace: Path, transcript: _Transcript, ask: PermissionRequest | QuestionRequest
) -> None:
    first = await _boot(workspace, transcript)
    first.mirror._begin_turn("t1")
    await first.park(ask)
    await first.drain()
    await first.kill()


async def test_an_allow_recorded_while_the_box_was_away_continues_the_turn_on_resume(
    workspace: Path,
) -> None:
    """Kill mid-ask → the reader allows it while nothing holds it (the route
    records the decision) → restart. The box acts on the record: the ask is
    NOT re-offered or announced again, the restored session is told what was
    allowed and to carry on, the identical ask the agent raises is granted
    from that answer, and the machine's own echo of the resolution lands so
    every later open reads the ask as done."""
    transcript = _Transcript()
    ask = _write_ask(workspace)
    await _killed_mid_ask(workspace, transcript, ask)
    _recorded(transcript, _decision(ask.request_id, "allow_once"))

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.catch_up()
        await _until(lambda: bool(second.adapter.sent_prompts))
        assert second.mirror.pending_interrupts == [], "a recorded answer is not re-offered"
        wire = await second.drain()
        assert not [e for e in wire if e.get("kind") == "permission.request"], (
            "and the card is not announced a second time"
        )
        resolved = [e for e in wire if e.get("kind") == "permission.resolved"]
        assert len(resolved) == 1
        assert resolved[0]["payload"]["request_id"] == ask.request_id
        assert resolved[0]["payload"]["option_id"] == "allow_once"
        assert resolved[0]["payload"]["decided_by"] == "user"
        assert resolved[0]["role"] != RECORDED_ANSWER_ROLE, (
            "the echo is the machine's, not a record"
        )
        prompt = second.adapter.sent_prompts[0]
        assert prompt.text == CONTINUE_TEXT
        assert prompt.system is not None and "ALLOWED the edit action" in prompt.system
        assert second.mirror.turn_running
        again = _write_ask(workspace, request_id="req-w-again")
        await second.adapter.feed(again)
        await second.adapter.wait_for_permission_reply("req-w-again", "allow_once")
        assert second.mirror.pending_interrupts == []
    finally:
        await second.kill()


async def test_a_reject_recorded_while_the_box_was_away_ends_the_turn_on_resume(
    workspace: Path,
) -> None:
    transcript = _Transcript()
    ask = _write_ask(workspace)
    await _killed_mid_ask(workspace, transcript, ask)
    _recorded(transcript, _decision(ask.request_id, "reject_once"))

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.catch_up()
        wire = await second.drain()
        assert second.mirror.pending_interrupts == []
        assert not [e for e in wire if e.get("kind") == "permission.request"]
        resolved = [e for e in wire if e.get("kind") == "permission.resolved"]
        assert [r["payload"]["option_id"] for r in resolved] == ["reject_once"]
        assert resolved[0]["payload"]["decided_by"] == "user"
        idle = [e for e in wire if "__meta__" in e]
        assert idle and idle[-1]["__meta__"]["turn_state"]["state"] == "idle"
        assert second.adapter.sent_prompts == [], "a reject continues nothing"
        assert not second.mirror.turn_running
    finally:
        await second.kill()


async def test_a_question_answered_while_the_box_was_away_continues_with_the_answer(
    workspace: Path,
) -> None:
    transcript = _Transcript()
    question = _question()
    await _killed_mid_ask(workspace, transcript, question)
    _recorded(
        transcript,
        QuestionAnswered(
            event_id=recorded_answer_event_id(question.request_id),
            time=_T,
            session_id=CHAT_ID,
            request_id=question.request_id,
            answers=[["eu-west"]],
            decided_by="user",
        ),
    )

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.catch_up()
        await _until(lambda: bool(second.adapter.sent_prompts))
        assert second.mirror.pending_interrupts == []
        wire = await second.drain()
        answered = [e for e in wire if e.get("kind") == "question.answered"]
        assert len(answered) == 1 and answered[0]["payload"]["answers"] == [["eu-west"]]
        prompt = second.adapter.sent_prompts[0]
        assert prompt.system is not None and "Which region? -> eu-west" in prompt.system
    finally:
        await second.kill()


async def test_a_question_declined_while_the_box_was_away_ends_the_turn(
    workspace: Path,
) -> None:
    transcript = _Transcript()
    question = _question("req-q3")
    await _killed_mid_ask(workspace, transcript, question)
    _recorded(
        transcript,
        QuestionRejected(
            event_id=recorded_answer_event_id("req-q3"),
            time=_T,
            session_id=CHAT_ID,
            request_id="req-q3",
            reason="never mind",
        ),
    )

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.catch_up()
        wire = await second.drain()
        assert second.mirror.pending_interrupts == []
        rejected = [e for e in wire if e.get("kind") == "question.rejected"]
        assert len(rejected) == 1 and rejected[0]["payload"]["reason"] == "never mind"
        assert second.adapter.sent_prompts == []
        assert not second.mirror.turn_running
    finally:
        await second.kill()


async def test_a_live_box_that_also_received_the_relay_applies_the_answer_once(
    workspace: Path, box: _Box
) -> None:
    """The box is alive and holding the ask; the reader answers; the route
    both relays the answer and records it. Whichever arrives first settles
    the ask, and the other is applied by nobody — one reply to the harness,
    no continuation of a turn that never stopped, no stray-relay count."""
    ask = _write_ask(workspace)
    await box.park(ask)
    await box.drain()
    box.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_once"})
    await box.adapter.wait_for_permission_reply(ask.request_id, "allow_once")

    _recorded(box.transcript, _decision(ask.request_id, "allow_once"))
    await box.mirror.catch_up()
    await asyncio.sleep(0.1)
    assert box.adapter.permission_replies == [(ask.request_id, "allow_once")]
    assert box.adapter.sent_prompts == []
    assert box.mirror.pending_interrupts == []
    assert box.mirror._ignored_relays == 0


async def test_a_record_that_reaches_a_live_box_before_the_relay_settles_the_parked_ask(
    workspace: Path, box: _Box
) -> None:
    """The relay was lost (the socket was down for the moment it was sent)
    but the record is in the transcript: the next catch-up settles the ask
    the box is holding from the record, and the late relay is a no-op."""
    ask = _write_ask(workspace)
    await box.park(ask)
    await box.drain()

    _recorded(box.transcript, _decision(ask.request_id, "allow_once"))
    await box.mirror.catch_up()
    await box.adapter.wait_for_permission_reply(ask.request_id, "allow_once")
    assert box.mirror.pending_interrupts == []
    assert box.adapter.sent_prompts == [], "a live turn is continued by the harness, not a prompt"

    box.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_once"})
    await asyncio.sleep(0.05)
    assert box.adapter.permission_replies == [(ask.request_id, "allow_once")]
    assert box.mirror._ignored_relays == 0


async def test_an_answer_the_machine_already_acted_on_is_not_acted_on_again(
    workspace: Path,
) -> None:
    """The transcript holds the reader's record AND the machine's own echo of
    the resolution (a box applied the record, then died): the next box reads
    the ask as done — no continuation, nothing published, nothing parked."""
    transcript = _Transcript()
    ask = _write_ask(workspace)
    await _killed_mid_ask(workspace, transcript, ask)
    _recorded(transcript, _decision(ask.request_id, "allow_once"))
    echo = PermissionResolved(
        event_id=f"restored-{ask.request_id}-resolved",
        time=_T,
        session_id=CHAT_ID,
        request_id=ask.request_id,
        option_id="allow_once",
        decided_by="user",
    )
    transcript.append(
        {
            "event_id": echo.event_id,
            "role": "assistant",
            "kind": echo.event_type,
            "payload": echo.model_dump(mode="json"),
        }
    )

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.catch_up()
        wire = await second.drain(seconds=0.2)
        assert second.mirror.pending_interrupts == []
        assert second.adapter.sent_prompts == []
        assert not [e for e in wire if e.get("kind", "").startswith("permission.")]
        assert not second.mirror.turn_running
    finally:
        await second.kill()


async def test_a_recorded_allow_the_chats_mode_refuses_is_closed_as_the_policys_decision(
    workspace: Path,
) -> None:
    """A reader allowed a write, but by the time a box acts on the record the
    chat is read-only: the allow is not applied (the fence is the box's), the
    ask is closed on the transcript as the policy's refusal so no later open
    tries it again, and the composer comes back."""
    transcript = _Transcript()
    ask = _write_ask(workspace)
    await _killed_mid_ask(workspace, transcript, ask)
    _recorded(transcript, _decision(ask.request_id, "allow_once"))

    second = await _boot(workspace, transcript)
    try:
        assert second.mirror._session is not None
        second.mirror._session.set_permission_mode("read_only")
        second.mirror._mode = "read_only"
        await second.mirror.catch_up()
        wire = await second.drain()
        assert second.mirror.pending_interrupts == []
        assert second.adapter.sent_prompts == []
        resolved = [e for e in wire if e.get("kind") == "permission.resolved"]
        assert [(r["payload"]["option_id"], r["payload"]["decided_by"]) for r in resolved] == [
            ("reject_once", "policy")
        ]
        idle = [e for e in wire if "__meta__" in e]
        assert idle and idle[-1]["__meta__"]["turn_state"]["state"] == "idle"
        assert not second.mirror.turn_running
    finally:
        await second.kill()


# ---------------------------------------------------------------------------
# An allowed ask's tool call is RE-RUN on resume; its real result closes it
# ---------------------------------------------------------------------------


ASK_SHAPES = ["provider-only", "part-and-provider"]
"""How an ask names the call it gates. ``provider-only``: the ask carries the
provider's call id alone (an opencode ask raised before the translator learned
the part, or one an older writer recorded). ``part-and-provider``: the ask
carries the transcript's key with the provider id alongside (what the
translator writes once it has seen the part). The transcript's ``tool.call``
row is keyed by the part in both, with the provider id on it."""


def _call_for(
    ask: PermissionRequest,
    *,
    event_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> ToolCall:
    """The call an ask gated, as the transcript records it: keyed by the
    harness's PART id — never the provider id the ask may carry — with the
    provider's id on the row."""
    assert ask.tool_call_id is not None
    provider_id = ask.provider_call_id or ask.tool_call_id
    part_id = ask.tool_call_id if ask.tool_call_id != provider_id else f"prt-{ask.request_id}"
    return ToolCall(
        event_id=event_id,
        time=_T,
        session_id=CHAT_ID,
        tool_call_id=part_id,
        provider_call_id=provider_id,
        message_id="m-assistant",
        tool_name=tool_name,
        input=tool_input,
        status="running",
    )


def _gated_write(
    workspace: Path,
    *,
    request_id: str = "req-w",
    name: str = "sleepy.txt",
    content: str = "resumed\n",
    ask_shape: str = "provider-only",
) -> tuple[ToolCall, PermissionRequest]:
    """The write the harness announced, and the ask that gated it — the two
    naming the call by DIFFERENT ids, as opencode does."""
    assert ask_shape in ASK_SHAPES
    if ask_shape == "provider-only":
        ask = _write_ask(workspace, request_id=request_id, name=name)
    else:
        ask = _write_ask(
            workspace,
            request_id=request_id,
            name=name,
            tool_call_id=f"prt-{request_id}",
            provider_call_id=f"call-{request_id}",
        )
    call = _call_for(
        ask,
        event_id=f"ev-call-{request_id}",
        tool_name="write",
        tool_input={
            "filePath": str(workspace / ".alkera" / "chats" / CHAT_ID / "scratch" / name),
            "content": content,
        },
    )
    assert call.tool_call_id != f"call-{request_id}", "the part is not the provider call"
    return call, ask


async def _killed_mid_gated_call(
    workspace: Path, transcript: _Transcript, call: ToolCall, ask: PermissionRequest
) -> None:
    first = await _boot(workspace, transcript)
    first.mirror._begin_turn("t1")
    await first.adapter.feed(call)
    await first.park(ask)
    await first.drain()
    await first.kill()


def _closures(wire: list[dict[str, Any]], call_id: str) -> list[dict[str, Any]]:
    return [
        e["payload"]
        for e in wire
        if e.get("kind") == "tool.call_update" and e["payload"].get("tool_call_id") == call_id
    ]


def _closed_ids(wire: list[dict[str, Any]]) -> set[str]:
    """Every call id the box published a closure for."""
    return {
        str(e["payload"].get("tool_call_id")) for e in wire if e.get("kind") == "tool.call_update"
    }


def _notes(wire: list[dict[str, Any]]) -> list[str]:
    return [
        str(e["payload"]["part"].get("text"))
        for e in wire
        if e.get("kind") == "part.created" and isinstance(e["payload"].get("part"), dict)
    ]


async def _resumed_with_allow(
    workspace: Path, transcript: _Transcript, *, recorded: bool
) -> tuple[_Box, list[dict[str, Any]]]:
    """The second life: the allow was recorded while the box was away, or the
    re-offered ask is answered after the resume. Returns the box and the wire."""
    second = await _boot(workspace, transcript)
    await second.mirror.catch_up()
    if not recorded:
        assert second.mirror.pending_interrupts == ["req-w"], "re-offered first"
        second.mirror._answer_interrupt("req-w", {"option_id": "allow_once"})
    await _until(lambda: bool(second.adapter.sent_prompts))
    wire = await second.drain()
    return second, wire


@pytest.mark.parametrize("ask_shape", ASK_SHAPES)
@pytest.mark.parametrize(
    "recorded", [True, False], ids=["recorded-while-away", "answered-after-resume"]
)
async def test_an_allowed_write_is_re_run_and_its_real_result_closes_the_original_call(
    workspace: Path, recorded: bool, ask_shape: str
) -> None:
    """Kill with a write ask pending → Allow (recorded while asleep, or given
    after the resume) → the workspace performs the write itself, the file is
    in the chat folder, the ORIGINAL call — the transcript's ``prt-…`` row,
    whichever id the ask named it by — closes completed with that result, no
    closure lands on a call the transcript never had, the agent is told it was
    done (not to redo it), and nothing says the turn was lost."""
    transcript = _Transcript()
    call, ask = _gated_write(workspace, ask_shape=ask_shape)
    await _killed_mid_gated_call(workspace, transcript, call, ask)
    target = workspace / ".alkera" / "chats" / CHAT_ID / "scratch" / "sleepy.txt"
    assert not target.exists(), "the harness never ran it"
    if recorded:
        _recorded(transcript, _decision(ask.request_id, "allow_once"))

    second, wire = await _resumed_with_allow(workspace, transcript, recorded=recorded)
    try:
        assert target.read_text(encoding="utf-8") == "resumed\n", (
            "the write the reader allowed landed"
        )
        assert second.mirror.pending_interrupts == []
        assert _closed_ids(wire) == {call.tool_call_id}, "one closure, on the part the reader sees"
        closures = _closures(wire, call.tool_call_id)
        assert len(closures) == 1, closures
        assert closures[0]["status"] == "completed"
        assert closures[0]["error_text"] is None
        assert closures[0]["output"]["replayed"] is True
        assert closures[0]["output"]["path"] == str(target)
        assert closures[0]["input"] == call.input
        resolved = [e for e in wire if e.get("kind") == "permission.resolved"]
        assert [(r["payload"]["option_id"], r["payload"]["decided_by"]) for r in resolved] == [
            ("allow_once", "user")
        ]
        assert TURN_LOST_TO_RESTART not in _notes(wire)
        prompt = second.adapter.sent_prompts[0]
        assert prompt.text == CONTINUE_TEXT
        assert prompt.system is not None
        assert "ALLOWED the edit action" in prompt.system
        assert "ALREADY performed" in prompt.system and "Do not perform it again" in prompt.system
        assert "written (8 bytes)" in prompt.system
        assert "did NOT run" not in prompt.system
    finally:
        await second.kill()


async def test_the_call_is_read_from_this_boxs_own_copy_when_the_transcript_rows_lack_it(
    workspace: Path,
) -> None:
    """The cloud's rows read back stop short of the call (a page boundary, a
    row the server dropped); the box's own chat log still has it, so the
    write is re-run from there."""
    transcript = _Transcript()
    call, ask = _gated_write(workspace)
    await _killed_mid_gated_call(workspace, transcript, call, ask)
    transcript.rows = [
        r for r in transcript.rows if r["kind"] not in ("tool.call", "tool.call_update")
    ]
    _recorded(transcript, _decision(ask.request_id, "allow_once"))

    second, wire = await _resumed_with_allow(workspace, transcript, recorded=True)
    try:
        target = workspace / ".alkera" / "chats" / CHAT_ID / "scratch" / "sleepy.txt"
        assert target.read_text(encoding="utf-8") == "resumed\n"
        assert [c["status"] for c in _closures(wire, call.tool_call_id)] == ["completed"]
    finally:
        await second.kill()


def _rewrite_the_transcript(chat_dir: Path) -> None:
    """What a person with write on the shared chat could once do to the copy
    on the drive: change the input of the call the next box will re-run."""
    log = chat_dir / "chat.jsonl"
    text = log.read_text(encoding="utf-8")
    assert "resumed" in text, "the gated write's content is in the transcript"
    log.write_text(text.replace("resumed", "hijacked"), encoding="utf-8")


@pytest.mark.parametrize(
    "tampered",
    [False, True],
    ids=["the-transcript-as-the-box-pinned-it", "the-transcript-rewritten-after-the-pin"],
)
async def test_a_call_is_rebuilt_from_this_boxs_copy_only_when_it_matches_its_pinned_digest(
    workspace: Path, tampered: bool
) -> None:
    """The box pins its logs before the folder leaves it. The next box, with
    rows that stop short of the call, reads its own copy back — and first
    checks that copy against the digest that travelled with it. A transcript
    that still starts with the pinned bytes (whatever this box appended since)
    is trusted and the write is re-run from it. One that does not is refused:
    nothing is written, the original call closes with the mismatch as its
    reason, and the agent is told the call did not run."""
    transcript = _Transcript()
    call, ask = _gated_write(workspace)
    await _killed_mid_gated_call(workspace, transcript, call, ask)
    chat_dir = workspace / ".alkera" / "chats" / CHAT_ID
    pin_trace(chat_dir, CHAT_ID)
    if tampered:
        _rewrite_the_transcript(chat_dir)
    transcript.rows = [
        r for r in transcript.rows if r["kind"] not in ("tool.call", "tool.call_update")
    ]
    _recorded(transcript, _decision(ask.request_id, "allow_once"))

    second, wire = await _resumed_with_allow(workspace, transcript, recorded=True)
    try:
        target = chat_dir / "scratch" / "sleepy.txt"
        prompt = second.adapter.sent_prompts[0]
        assert prompt.system is not None
        if tampered:
            assert not target.exists(), "nothing runs from a transcript that failed its check"
            # With the rows short of the call and its own copy refused, the box
            # cannot learn the part id; the closure lands on the id the ask
            # named, which is the one the reader's card was drawn from.
            assert ask.tool_call_id is not None
            closures = _closures(wire, ask.tool_call_id)
            assert [c["status"] for c in closures] == ["error"]
            assert closures[0]["error_text"] == TRANSCRIPT_MISMATCH
            assert closures[0]["output"] == {"error": TRANSCRIPT_MISMATCH}
            assert "did NOT run" in prompt.system
            assert "hijacked" not in prompt.system
        else:
            assert target.read_text(encoding="utf-8") == "resumed\n"
            closures = _closures(wire, call.tool_call_id)
            assert [c["status"] for c in closures] == ["completed"]
            assert "ALREADY performed" in prompt.system
        assert second.mirror.pending_interrupts == []
    finally:
        await second.kill()


async def test_a_reject_closes_the_original_call_as_denied_and_writes_nothing(
    workspace: Path,
) -> None:
    transcript = _Transcript()
    call, ask = _gated_write(workspace)
    await _killed_mid_gated_call(workspace, transcript, call, ask)
    _recorded(transcript, _decision(ask.request_id, "reject_once"))

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.catch_up()
        wire = await second.drain()
        assert not (workspace / ".alkera" / "chats" / CHAT_ID / "scratch" / "sleepy.txt").exists()
        assert _closed_ids(wire) == {call.tool_call_id}
        closures = _closures(wire, call.tool_call_id)
        assert [(c["status"], c["error_text"]) for c in closures] == [("error", DENIED_BY_USER)]
        assert closures[0]["output"] == {"error": DENIED_BY_USER}
        assert second.adapter.sent_prompts == [], "a reject continues nothing"
        assert TURN_LOST_TO_RESTART not in _notes(wire)
        idle = [e for e in wire if "__meta__" in e]
        assert idle and idle[-1]["__meta__"]["turn_state"]["state"] == "idle"
    finally:
        await second.kill()


async def test_a_call_the_workspace_cannot_re_run_closes_as_not_run_and_the_agent_retries_it(
    workspace: Path,
) -> None:
    """A shell command's effect is not a function of its input: it is not
    replayed. The original call closes as not run — never as a result nobody
    produced — and the continuation says so and asks for the retry."""
    transcript = _Transcript()
    ask = _write_ask(workspace, request_id="req-w")
    call = _call_for(
        ask,
        event_id="ev-call-sh",
        tool_name="bash",
        tool_input={"command": "echo resumed > sleepy.txt"},
    )
    await _killed_mid_gated_call(workspace, transcript, call, ask)
    _recorded(transcript, _decision(ask.request_id, "allow_once"))

    second, wire = await _resumed_with_allow(workspace, transcript, recorded=True)
    try:
        assert not (workspace / ".alkera" / "chats" / CHAT_ID / "scratch" / "sleepy.txt").exists()
        # The not-run closure lands on the transcript's call, not on the
        # provider id the ask named — one failed call in the turn, not two.
        assert _closed_ids(wire) == {call.tool_call_id}
        closures = _closures(wire, call.tool_call_id)
        assert [(c["status"], c["error_text"]) for c in closures] == [
            ("error", NOT_RUN_BEFORE_RESTART)
        ]
        prompt = second.adapter.sent_prompts[0]
        assert prompt.system is not None
        assert "did NOT run" in prompt.system and "must be retried" in prompt.system
        assert "ALREADY performed" not in prompt.system
        # The retry the agent raises is granted from the reader's answer.
        again = _write_ask(workspace, request_id="req-w-again")
        await second.adapter.feed(again)
        await second.adapter.wait_for_permission_reply("req-w-again", "allow_once")
    finally:
        await second.kill()


async def test_a_replay_that_fails_closes_the_call_with_the_reason_and_the_agent_retries(
    workspace: Path,
) -> None:
    transcript = _Transcript()
    ask = _write_ask(workspace, request_id="req-w", name="f.txt")
    target = workspace / ".alkera" / "chats" / CHAT_ID / "scratch" / "f.txt"
    call = _call_for(
        ask,
        event_id="ev-call-edit",
        tool_name="edit",
        tool_input={"filePath": str(target), "oldString": "gone", "newString": "here"},
    )
    await _killed_mid_gated_call(workspace, transcript, call, ask)
    target.write_text("something else")
    _recorded(transcript, _decision(ask.request_id, "allow_once"))

    second, wire = await _resumed_with_allow(workspace, transcript, recorded=True)
    try:
        assert target.read_text(encoding="utf-8") == "something else"
        assert _closed_ids(wire) == {call.tool_call_id}
        closures = _closures(wire, call.tool_call_id)
        assert len(closures) == 1 and closures[0]["status"] == "error"
        assert "not in the file" in str(closures[0]["error_text"])
        prompt = second.adapter.sent_prompts[0]
        assert prompt.system is not None and "FAILED" in prompt.system
        assert "Retry that action" in prompt.system
    finally:
        await second.kill()


async def test_the_harness_closing_a_re_run_call_as_aborted_does_not_overwrite_its_result(
    workspace: Path,
) -> None:
    """opencode aborts the part it finds pending when the continuation
    arrives: that late error for the SAME call is not the result and never
    reaches the transcript; an error for another call still does."""
    transcript = _Transcript()
    call, ask = _gated_write(workspace)
    await _killed_mid_gated_call(workspace, transcript, call, ask)
    _recorded(transcript, _decision(ask.request_id, "allow_once"))

    second, wire = await _resumed_with_allow(workspace, transcript, recorded=True)
    try:
        assert [c["status"] for c in _closures(wire, call.tool_call_id)] == ["completed"]
        await second.adapter.feed(
            ToolCallUpdate(
                event_id="oc-abort",
                time=_T,
                session_id=CHAT_ID,
                tool_call_id=call.tool_call_id,
                status="error",
                error_text="Tool execution aborted",
            )
        )
        await second.adapter.feed(
            ToolCallUpdate(
                event_id="oc-other",
                time=_T,
                session_id=CHAT_ID,
                tool_call_id="call-other",
                status="error",
                error_text="boom",
            )
        )
        # The other call's error is the barrier, not a window: the pump takes
        # the two in the order they were fed, so the moment this one is on the
        # wire the abort ahead of it has already been decided — dropped, or
        # published for this same drain to have collected.
        later = await second.drain_until(lambda out: bool(_closures(out, "call-other")))
        assert [c["error_text"] for c in _closures(later, "call-other")] == ["boom"], (
            f"an error for another call never reached the wire within {_WIRE_WINDOW}"
        )
        assert _closures(later, call.tool_call_id) == []
    finally:
        await second.kill()


# ---------------------------------------------------------------------------
# "The workspace restarted… send the question again" is for a LOST turn only
# ---------------------------------------------------------------------------


def _working_doc() -> DocHandle:
    return cast(DocHandle, SimpleNamespace(state={"meta": {"turn_state": {"state": "working"}}}))


async def test_a_turn_parked_on_an_ask_is_not_reported_lost_when_the_box_comes_back(
    workspace: Path,
) -> None:
    transcript = _Transcript()
    call, ask = _gated_write(workspace)
    await _killed_mid_gated_call(workspace, transcript, call, ask)
    _recorded(transcript, _decision(ask.request_id, "allow_once"))

    second = await _boot(workspace, transcript)
    try:
        second.mirror._doc = _working_doc()
        await second.mirror._settle_interrupted_turn()
        await second.mirror.catch_up()
        await _until(lambda: bool(second.adapter.sent_prompts))
        wire = await second.drain()
        assert TURN_LOST_TO_RESTART not in _notes(wire), "the mirror is continuing the turn itself"
        # A later catch-up does not say it either.
        await second.mirror.catch_up()
        assert TURN_LOST_TO_RESTART not in _notes(await second.drain())
    finally:
        second.mirror._doc = None
        await second.kill()


async def test_a_turn_nothing_picks_back_up_is_reported_lost_once(workspace: Path) -> None:
    transcript = _Transcript()
    first = await _boot(workspace, transcript)
    first.mirror._begin_turn("t1")
    await first.drain()
    await first.kill()

    second = await _boot(workspace, transcript)
    try:
        second.mirror._doc = _working_doc()
        await second.mirror._settle_interrupted_turn()
        assert TURN_LOST_TO_RESTART not in _notes(await second.drain()), (
            "not before the transcript has been read"
        )
        await second.mirror.catch_up()
        said = await second.drain_until(lambda out: TURN_LOST_TO_RESTART in _notes(out))
        assert _notes(said).count(TURN_LOST_TO_RESTART) == 1
        await second.mirror.catch_up()
        assert TURN_LOST_TO_RESTART not in _notes(await second.drain()), "once"
    finally:
        second.mirror._doc = None
        await second.kill()


async def test_an_answer_that_arrives_before_the_ask_is_parked_is_applied_when_it_is(
    workspace: Path, box: _Box
) -> None:
    """The harness publishes the ask before the policy runs, so a reader can
    answer — and the server record — an ask the mirror has not parked yet. That
    answer used to be dropped as a stray; the reader's retry then met the
    server's "already answered" refusal, and the ask could never be answered.
    Now the early answer is held and settles the ask the moment it is parked,
    and the tool proceeds exactly once."""
    box.mirror._begin_turn("t1")
    ask = _write_ask(workspace)
    # The ask is on the wire (the pump noted it) but the policy has not parked it.
    box.mirror._note_ask_published(ask)
    box.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_once"})
    assert box.mirror._ignored_relays == 0, "an early answer is held, not ignored"
    assert box.mirror.pending_interrupts == []

    parked = asyncio.get_running_loop().create_task(box.mirror._resolve_permission(ask))
    assert await asyncio.wait_for(parked, 5.0) == "allow_once"
    assert box.mirror.pending_interrupts == []
    assert not box.mirror._meter.waiting
    assert box.mirror._ignored_relays == 0
    assert ask.request_id in box.mirror._settled_asks
    assert ask.request_id not in box.mirror._early_answers
    # The same answer relayed again is the duplicate it always was, not a stray.
    box.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_once"})
    assert box.mirror._ignored_relays == 0
    assert ask.request_id not in box.mirror._early_answers


async def test_an_answer_for_an_ask_never_put_on_the_wire_is_still_a_stray(
    workspace: Path, box: _Box
) -> None:
    """Holding is for an ask the cloud has been shown and the policy has not
    parked; an id nothing published is the stray the relay guard counts, and
    nothing is kept for it."""
    ask = _write_ask(workspace, request_id="req-never")
    box.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_once"})
    assert box.mirror._ignored_relays == 1
    assert ask.request_id not in box.mirror._early_answers


async def test_an_early_answer_is_refused_at_relay_time_when_the_ask_offers_nothing_for_it(
    workspace: Path, box: _Box
) -> None:
    """The guards that refuse an answer for a parked ask fire for a held one
    too, the moment the relay lands — an option the ask never offered is not
    kept to be refused later."""
    ask = _write_ask(workspace, request_id="req-narrow")
    ask = ask.model_copy(
        update={"options": [o for o in ask.options if o.option_id != "allow_always"]}
    )
    box.mirror._note_ask_published(ask)
    box.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_always"})
    assert box.mirror._ignored_relays == 1
    assert ask.request_id not in box.mirror._early_answers


async def test_a_held_answer_is_dropped_once_the_harness_publishes_the_resolution(
    workspace: Path, box: _Box
) -> None:
    ask = _write_ask(workspace, request_id="req-policy")
    box.mirror._note_ask_published(ask)
    box.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_once"})
    assert ask.request_id in box.mirror._early_answers
    box.mirror._note_ask_published(
        PermissionResolved(
            event_id="res-policy",
            time=datetime.now(UTC),
            session_id=CHAT_ID,
            request_id=ask.request_id,
            option_id="reject_once",
        )
    )
    assert ask.request_id not in box.mirror._early_answers
    box.mirror._answer_interrupt(ask.request_id, {"option_id": "allow_once"})
    assert box.mirror._ignored_relays == 1


async def test_an_early_answer_for_a_question_settles_it_when_parked(box: _Box) -> None:
    question = _question("req-q-early")
    box.mirror._note_ask_published(question)
    box.mirror._answer_interrupt(question.request_id, {"answers": [["eu-west"]]})
    parked = asyncio.get_running_loop().create_task(box.mirror._resolve_question(question))
    assert await asyncio.wait_for(parked, 5.0) == ("answer", [["eu-west"]])
    assert box.mirror._ignored_relays == 0


async def _stream(*events: Any) -> AsyncIterator[Any]:
    for event in events:
        yield event


def _policy_resolution(ask: PermissionRequest, *, event_id: str = "res-1") -> PermissionResolved:
    return PermissionResolved(
        event_id=event_id,
        time=datetime.now(UTC),
        session_id=CHAT_ID,
        request_id=ask.request_id,
        option_id="reject_once",
        decided_by="policy",
    )


async def test_no_request_is_published_for_an_ask_a_resolution_already_closed(
    workspace: Path, box: _Box
) -> None:
    """The sequence seen live: the harness's ``permission.request``, the
    policy's ``permission.resolved`` fifteen milliseconds later, and then a
    SECOND ``permission.request`` under the same id — a card the server has
    already closed, whose every answer it refuses, and the chat is stuck on
    it. A request for a resolved id is never published, and the resolution
    once."""
    ask = _write_ask(workspace, request_id="per_live")
    again = ask.model_copy(update={"event_id": "perm-again"})
    await box.mirror._pump(
        _stream(ask, _policy_resolution(ask), again, _policy_resolution(ask, event_id="res-2"))
    )
    kinds = [
        entry["kind"]
        for entry in await box.drain(seconds=0.1)
        if str(entry.get("kind", "")).startswith("permission")
    ]
    assert kinds == ["permission.request", "permission.resolved"]


async def test_the_re_announce_of_a_resolved_ask_publishes_nothing(
    workspace: Path, box: _Box
) -> None:
    ask = _write_ask(workspace, request_id="per_late")
    await box.mirror._pump(_stream(ask, _policy_resolution(ask)))
    await box.drain(seconds=0.1)
    box.mirror._announce_ask(ask)
    assert await box.drain(seconds=0.1) == []


async def test_a_resolution_read_back_from_the_transcript_closes_the_id_for_a_later_request(
    workspace: Path,
) -> None:
    """After a restart the resolution is in the transcript, not in this
    process: a request for that id arriving later is still not published."""
    transcript = _Transcript()
    first = await _boot(workspace, transcript)
    ask = _write_ask(workspace, request_id="per_restart")
    await first.mirror._pump(_stream(ask, _policy_resolution(ask)))
    await first.drain(seconds=0.1)
    await first.kill()
    assert [k for k, _ in transcript.kinds(ask.request_id)] == [
        "permission.request",
        "permission.resolved",
    ]

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.catch_up()
        await second.mirror._pump(_stream(ask.model_copy(update={"event_id": "perm-replay"})))
        assert [
            e["kind"]
            for e in await second.drain(seconds=0.2)
            if e.get("kind", "").startswith("permission")
        ] == []
    finally:
        await second.kill()


# ---------------------------------------------------------------------------
# a turn the restart cut short is run again from the person's message
# ---------------------------------------------------------------------------


def _asked(transcript: _Transcript, text: str) -> None:
    from alkera_core.schemas.objects.transcript import ChatPromptRecord

    transcript.append(
        ChatPromptRecord(text=text, client_id="c-asked", user_id=OWNER).model_dump(mode="json"),
        role="user",
    )


def _started_answering(transcript: _Transcript) -> None:
    """What the box published of the turn before it died: its first words."""
    transcript.append({"role": "assistant", "event_id": "oc-partial", "text": "Counting…"})


async def test_a_turn_the_restart_cut_short_is_restarted_from_its_message(
    workspace: Path,
) -> None:
    from alkera_cli.harness.turn_restart import TURN_RESTART_NOTE

    transcript = _Transcript()
    _asked(transcript, "count the rows in orders")
    _started_answering(transcript)
    # A process on THIS disk was mid-turn when it died: its log says running,
    # and nothing ever closed it. That is the mark a restart needs.
    first = await _boot(workspace, transcript)
    await first.adapter.feed(
        SessionStatusChanged(event_id="oc-run", time=_T, session_id=CHAT_ID, status="running")
    )
    await first.drain()
    await first.kill()

    second = await _boot(workspace, transcript)
    lane = asyncio.get_running_loop().create_task(second.mirror._prompt_lane())
    try:
        second.mirror._state = "running"
        second.mirror._ready.set()
        second.mirror._doc = _working_doc()
        await second.mirror._settle_interrupted_turn()
        await second.mirror.catch_up()
        await _until(lambda: bool(second.adapter.sent_prompts))
        wire = await second.drain()
        prompt = second.adapter.sent_prompts[0]
        assert prompt.text == "count the rows in orders"
        assert TURN_RESTART_NOTE in (prompt.system or "")
        assert TURN_LOST_TO_RESTART not in _notes(wire)
        working = [e for e in wire if "__meta__" in e]
        assert any(e["__meta__"]["turn_state"]["state"] == "working" for e in working)
        # The interrupted attempt's words are still on the record.
        assert any(r["payload"].get("event_id") == "oc-partial" for r in transcript.rows)
        # Once: a later catch-up does not run it again.
        await second.mirror.catch_up()
        await second.drain()
        assert len(second.adapter.sent_prompts) == 1
    finally:
        lane.cancel()
        with contextlib.suppress(BaseException):
            await lane
        second.mirror._doc = None
        await second.kill()


async def test_a_message_sent_while_no_box_held_the_chat_starts_its_turn_on_take(
    workspace: Path,
) -> None:
    """The box that takes the chat waits until the message is handed to the
    harness — not merely read — before it goes on to the next chat."""
    transcript = _Transcript()
    _asked(transcript, "are you there?")
    box = await _boot(workspace, transcript)
    lane = asyncio.get_running_loop().create_task(box.mirror._prompt_lane())
    try:
        box.mirror._state = "running"
        box.mirror._ready.set()
        box.mirror.request_catch_up()
        assert await box.mirror.wait_for_owed_turn(10.0) is True
        assert [p.text for p in box.adapter.sent_prompts] == ["are you there?"]
    finally:
        lane.cancel()
        with contextlib.suppress(BaseException):
            await lane
        await box.kill()


@pytest.mark.parametrize(
    ("admission", "note"),
    [
        pytest.param({"allowed": True}, None, id="the-author-may-still-send"),
        pytest.param(None, None, id="a-server-from-before-the-check"),
        pytest.param(
            503,
            "This message was not run: the server could not confirm its author may still "
            "send here. Send it again",
            id="a-server-that-could-not-decide-holds-the-message",
        ),
        pytest.param(
            {
                "allowed": False,
                "code": "chat_send_requires_workspace_edit",
                "message": "You need edit access to this workspace to send a message.",
            },
            "This message was not run: You need edit access to this workspace to send a message.",
            id="the-share-was-revoked-before-pickup",
        ),
        pytest.param(
            401,
            "This message was not run: this machine's credential was refused",
            id="the-box-credential-was-refused",
        ),
        pytest.param(
            403,
            "This message was not run: the server says this chat is not this machine's to run",
            id="the-chat-is-not-this-box-s",
        ),
    ],
)
async def test_a_message_whose_author_lost_the_chat_before_pickup_is_not_run(
    workspace: Path, admission: dict[str, Any] | int | None, note: str | None
) -> None:
    """The server admitted the message, then the author's share was revoked
    before the box picked it up. The box asks the server again, by the send
    rule, before the turn starts: a refusal runs nothing and says so in the
    transcript, and so does a refusal of the box itself (its credential
    refused, or the chat not its own). A server that failed without deciding
    is asked again and, still undecided, the message is held: a slow backend
    is never why a revoked member's message runs. An allow, or a server that
    does not know the question, runs the message as before."""
    transcript = _Transcript()
    transcript.admission = admission
    _asked(transcript, "delete the staging rows")
    box = await _boot(workspace, transcript)
    lane = asyncio.get_running_loop().create_task(box.mirror._prompt_lane())
    try:
        box.mirror._state = "running"
        box.mirror._ready.set()
        box.mirror.request_catch_up()
        if note is None:
            assert await box.mirror.wait_for_owed_turn(10.0) is True
            assert [p.text for p in box.adapter.sent_prompts] == ["delete the staging rows"]
        else:
            said = await box.drain_until(lambda out: bool(_notes(out)))
            assert box.adapter.sent_prompts == [], "the turn never starts"
            assert _notes(said) == [note]
        # A 503 is asked again; every ask names the author.
        assert transcript.admission_asked, "the box asked before the turn"
        assert set(transcript.admission_asked) == {OWNER}, "asked about the message's author"
    finally:
        lane.cancel()
        with contextlib.suppress(BaseException):
            await lane
        await box.kill()


@pytest.mark.parametrize(
    ("admission", "runs"),
    [
        pytest.param({"allowed": True}, True, id="the-owner-may-send"),
        pytest.param(
            {"allowed": False, "message": "The owner's access was removed."},
            False,
            id="the-owner-may-not",
        ),
    ],
)
async def test_a_caught_up_message_with_no_author_is_asked_about_the_chats_owner(
    workspace: Path, admission: dict[str, Any], runs: bool
) -> None:
    """A recorded prompt whose author is empty is never let through unasked:
    the box asks about the person it runs the chat for, and runs the message
    only on that answer."""
    from alkera_core.schemas.objects.transcript import ChatPromptRecord

    transcript = _Transcript()
    transcript.admission = admission
    transcript.append(
        ChatPromptRecord(text="drop the table", client_id="c-anon", user_id="").model_dump(
            mode="json"
        ),
        role="user",
    )
    box = await _boot(workspace, transcript)
    lane = asyncio.get_running_loop().create_task(box.mirror._prompt_lane())
    try:
        box.mirror._state = "running"
        box.mirror._ready.set()
        box.mirror.request_catch_up()
        if runs:
            assert await box.mirror.wait_for_owed_turn(10.0) is True
            assert [p.text for p in box.adapter.sent_prompts] == ["drop the table"]
        else:
            said = await box.drain_until(lambda out: bool(_notes(out)))
            assert box.adapter.sent_prompts == [], "the turn never starts"
            assert _notes(said) == ["This message was not run: The owner's access was removed."]
        assert set(transcript.admission_asked) == {OWNER}
    finally:
        lane.cancel()
        with contextlib.suppress(BaseException):
            await lane
        await box.kill()


async def test_the_turn_admission_is_asked_as_the_machine_that_publishes_the_chat(
    workspace: Path,
) -> None:
    """The backend answers the admission only to the chat's publisher, the
    same gate as the gateway credential, so the box must ask as its machine.
    Asked as the chat's own agent id it was refused with a 403 on every real
    box, which the check then read as allowed: it never refused anything."""
    machine = "11111111-2222-4333-8444-555555555555"
    transcript = _Transcript()
    transcript.admission = {"allowed": True}
    _asked(transcript, "delete the staging rows")
    box = await _boot(workspace, transcript, machine_id=machine)
    lane = asyncio.get_running_loop().create_task(box.mirror._prompt_lane())
    try:
        box.mirror._state = "running"
        box.mirror._ready.set()
        box.mirror.request_catch_up()
        assert await box.mirror.wait_for_owed_turn(10.0) is True
        assert transcript.admission_headers, "the box asked before the turn"
        as_machine = {k.lower(): v for k, v in agent_headers(machine).items()}
        for headers in transcript.admission_headers:
            assert as_machine.items() <= headers.items(), "asked as the publishing machine"
    finally:
        lane.cancel()
        with contextlib.suppress(BaseException):
            await lane
        await box.kill()


async def test_waiting_for_an_owed_turn_is_bounded(workspace: Path) -> None:
    transcript = _Transcript()
    box = await _boot(workspace, transcript)
    try:
        # No catch-up ever runs: the wait gives up on its bound.
        assert await box.mirror.wait_for_owed_turn(0.1) is False
    finally:
        await box.kill()


async def test_an_answered_message_is_never_restarted_by_a_box_with_no_record_of_the_turn(
    workspace: Path,
) -> None:
    """The exact ordering that raced: the first box's answer is ON the record,
    the box was closed before any closing mark was published, and the chat's
    document still says working. A box coming up in a fresh workspace — nothing
    on its disk says a turn died there — must read the answer as the answer
    and ask nothing."""
    from alkera_cli.harness.turn_restart import TURN_RESTART_NOTE

    transcript = _Transcript()
    _asked(transcript, "answered already")
    transcript.append({"role": "assistant", "event_id": "oc-answer", "text": "answered"})

    box = await _boot(workspace, transcript)
    lane = asyncio.get_running_loop().create_task(box.mirror._prompt_lane())
    try:
        box.mirror._state = "running"
        box.mirror._ready.set()
        box.mirror._doc = _working_doc()
        await box.mirror._settle_interrupted_turn()
        assert await box.mirror.catch_up() == 0
        await box.drain()
        assert box.adapter.sent_prompts == []
        assert not any(TURN_RESTART_NOTE in (p.system or "") for p in box.adapter.sent_prompts)
    finally:
        lane.cancel()
        with contextlib.suppress(BaseException):
            await lane
        box.mirror._doc = None
        await box.kill()


# ---------------------------------------------------------------------------
# a restart never runs again what the turn it cut short already ran
# ---------------------------------------------------------------------------


def _machine_published(transcript: _Transcript, event: Any) -> None:
    """A harness event the machine put on the record, in the entry it wears."""
    transcript.append(append_entry(event, RoleIndex()))


def _the_live_turn_carried_on(transcript: _Transcript, call_id: str) -> None:
    """What a box holding the ask publishes after the reader answers it live:
    the call it allowed finishing, the answer, and the turn going idle. The
    machine's own resolution of the ask is NOT among them — in the saved chat
    only the reader's record of the answer reached the transcript."""
    _machine_published(
        transcript,
        ToolCallUpdate(
            event_id=f"done-{call_id}",
            time=_T,
            session_id=CHAT_ID,
            tool_call_id=call_id,
            status="completed",
            output={"stdout": "done-A-42"},
        ),
    )
    _machine_published(
        transcript,
        MessageCompleted(
            event_id="msg-done", time=_T, session_id=CHAT_ID, message_id="m", finish_reason="stop"
        ),
    )
    _machine_published(
        transcript,
        SessionStatusChanged(
            event_id="went-idle", time=_T, session_id=CHAT_ID, status="idle", phase="idle"
        ),
    )


async def test_an_answer_the_live_turn_acted_on_is_not_acted_on_again_after_a_restart(
    workspace: Path,
) -> None:
    """The saved chat's second run of an allowed command: the reader answered
    the ask while a box held it, the harness ran the call and finished the
    turn, and the process later restarted. The new box found the reader's
    record of the answer with no machine resolution beside it, read it as an
    answer nobody had acted on, and continued the turn — so the agent ran the
    command again under the old allow. A turn the machine ended after the
    answer is the proof it was acted on: nothing is resolved, re-run or
    continued."""
    transcript = _Transcript()
    ask = _write_ask(workspace)
    await _killed_mid_ask(workspace, transcript, ask)
    _recorded(transcript, _decision(ask.request_id, "allow_once"))
    _the_live_turn_carried_on(transcript, ask.tool_call_id or "")

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.catch_up()
        await asyncio.gather(*list(second.mirror._relay_tasks), return_exceptions=True)
        wire = await second.drain()
        assert second.adapter.sent_prompts == [], "the turn is over; nothing continues it"
        assert not [e for e in wire if e.get("kind") == "permission.resolved"]
        assert not [e for e in wire if e.get("kind") == "tool.call_update"]
        assert second.mirror.pending_interrupts == [], "and the ask is not re-offered"
        assert not second.mirror.turn_running
    finally:
        await second.kill()


@pytest.mark.parametrize(
    "status",
    [
        pytest.param("completed", id="a-tool-it-finished"),
        pytest.param("running", id="a-tool-the-kill-cut-mid-run"),
        pytest.param("pending", id="a-tool-it-had-just-started"),
    ],
)
async def test_a_turn_cut_short_after_it_started_a_tool_is_not_run_again(
    workspace: Path, status: str
) -> None:
    """A restart used to re-send the person's message to a fresh harness
    session, which runs every tool of the lost turn a second time — harmless
    for ``mkdir``, not for ``rm`` or an ``INSERT``. Once the record shows the
    turn finished a tool, the box does not run the message again: it says the
    turn was interrupted after N tools and leaves the person to send it. A tool
    the kill cut part way has no result on the record, and used to count as
    none, so the turn was run again and the tool with it (a file appended to
    twice): a started tool counts."""
    from alkera_cli.harness.turn_restart import TURN_RESTART_NOTE

    transcript = _Transcript()
    _asked(transcript, "delete the staging rows, then count what is left")
    _started_answering(transcript)
    _machine_published(
        transcript,
        ToolCallUpdate(
            event_id="rm-done",
            time=_T,
            session_id=CHAT_ID,
            tool_call_id="call-rm",
            status=status,
            output={"rows": 12} if status == "completed" else None,
        ),
    )
    first = await _boot(workspace, transcript)
    await first.adapter.feed(
        SessionStatusChanged(event_id="oc-run", time=_T, session_id=CHAT_ID, status="running")
    )
    await first.drain()
    await first.kill()

    second = await _boot(workspace, transcript)
    lane = asyncio.get_running_loop().create_task(second.mirror._prompt_lane())
    note = turn_stopped_after_tools(1)
    try:
        second.mirror._state = "running"
        second.mirror._ready.set()
        second.mirror._doc = _working_doc()
        await second.mirror._settle_interrupted_turn()
        await second.mirror.catch_up()
        said = await second.drain_until(lambda out: note in _notes(out))
        assert _notes(said).count(note) == 1
        assert TURN_LOST_TO_RESTART not in _notes(said)
        # Once, and nothing is handed to the harness — not now, not on the
        # next catch-up.
        await second.mirror.catch_up()
        await second.drain()
        assert second.adapter.sent_prompts == []
        assert not any(TURN_RESTART_NOTE in (p.system or "") for p in second.adapter.sent_prompts)
    finally:
        lane.cancel()
        with contextlib.suppress(BaseException):
            await lane
        second.mirror._doc = None
        await second.kill()


async def test_a_pressure_sleep_note_that_did_not_publish_is_posted_at_the_next_start(
    workspace: Path,
) -> None:
    """The note a sleep for room owes the chat goes on its transcript before
    the release; one whose publish did not land in time stays in the chat's
    records and is posted when the chat next starts on this box, once."""
    from alkera_cli.cloud.pressure_notice import pending_notes

    sentence = (
        "This chat was put to sleep because its box was short on memory, "
        "which stopped 1 process still running in it: sleep."
    )
    transcript = _Transcript()
    first = await _boot(workspace, transcript)
    try:
        # This rig runs no publisher, so the server never acknowledges the note.
        published = await first.mirror.note_pressure_sleep(sentence, within=0.2)
        said = await first.drain_until(lambda out: bool(_notes(out)))
        assert published is False
        assert _notes(said) == [sentence]
        assert pending_notes(first.mirror.chat_folder) == [sentence]
    finally:
        await first.kill()

    second = await _boot(workspace, transcript)
    try:
        await second.mirror.post_owed_notes()
        said = await second.drain_until(lambda out: bool(_notes(out)))
        assert _notes(said) == [sentence]
        assert pending_notes(second.mirror.chat_folder) == []
        await second.mirror.post_owed_notes()
        assert _notes(await second.drain(seconds=0.2)) == []
    finally:
        await second.kill()
