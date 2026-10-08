"""The headless driver (`alkera_cli.chat.headless`) over a real `HarnessRuntime` and a
`FakeAdapter`: the contract a scripted caller (`alkera run`, the experiments rig)
relies on, from turn completion and stop reasons to analyst-mode verification."""

from __future__ import annotations

import asyncio
import json
import types
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory, fake_runtime, finished_sql_job
from alkera_cli.chat.headless import (
    QUESTION_DECLINE_REASON,
    HeadlessError,
    run_headless,
)
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.gateway.client import GatewayUnavailableError, resolve_model_selection
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter, ScriptedMessage, ScriptedTurn
from alkera_cli.harness.adapter import PromptInput
from alkera_cli.harness.system_prompt import VERIFICATION_TURN_PROMPT
from alkera_core.schemas.chat import (
    Event,
    PartCreated,
    PermissionOption,
    PermissionRequest,
    QuestionPrompt,
    QuestionRequest,
    SessionStatusChanged,
    TextPart,
    ToolCall,
    ToolCallUpdate,
)

_T = datetime(2026, 7, 3, tzinfo=UTC)


# --- Adapters + factory


def _status(status: str, event_id: str = "st", **fields: Any) -> SessionStatusChanged:
    return SessionStatusChanged(
        event_id=event_id, time=_T, session_id="fake", status=status, **fields
    )


_PERMISSION = PermissionRequest(
    event_id="pr-1",
    time=_T,
    session_id="s",
    request_id="pr-1",
    permission_kind="edit",
    canonical_kind="edit",
    options=[
        PermissionOption(option_id="allow_once", name="Allow once"),
        PermissionOption(option_id="reject_once", name="Reject once"),
    ],
)
_QUESTION = QuestionRequest(
    event_id="q-1",
    time=_T,
    session_id="s",
    request_id="q-1",
    questions=[QuestionPrompt(question="which flavor?", options=[])],
)


class _AskingAdapter(FakeAdapter):
    """The first prompt starts a turn and asks `ask` (a permission, a question, or
    nothing at all, which leaves the turn hanging); the turn completes once the
    broker's decision lands, with text naming it."""

    def __init__(self, ask: Event | None = None) -> None:
        super().__init__()
        self._ask = ask

    async def send_prompt(self, prompt: PromptInput) -> None:
        self._sent_prompts.append(prompt)
        await self._bus.publish(self._status("run", "running", prompt.turn_id))
        if self._ask is not None:
            await self._bus.publish(self._ask)

    async def resolve_permission(self, request_id, option_id, *, reason=None) -> None:
        await super().resolve_permission(request_id, option_id, reason=reason)
        await self._emit_scripted_turn(
            "allowed" if str(option_id).startswith("allow") else "rejected",
            self._sent_prompts[-1].turn_id,
        )

    async def reject_question(self, request_id, reason=None) -> None:
        await super().reject_question(request_id, reason)
        await self._emit_scripted_turn(
            "proceeded without an answer", self._sent_prompts[-1].turn_id
        )


# --- Happy path + multi-turn


async def test_single_prompt_completes_with_final_text(tmp_path: Path) -> None:
    runtime, factory = fake_runtime(tmp_path, lambda: FakeAdapter(reply_text="Hello world"))

    result = await run_headless(tmp_path, ["say hello"], runtime=runtime)

    assert result.stop_reason == "completed"
    assert [t.final_text for t in result.turns] == ["Hello world"]
    assert result.final_text == "Hello world"
    assert result.session_id
    assert result.duration_seconds >= 0
    assert result.error_detail is None
    assert factory.adapters[0].sent_prompts[0].text == "say hello"


async def test_multi_prompt_runs_sequential_turns_in_one_session(tmp_path: Path) -> None:
    runtime, factory = fake_runtime(tmp_path, lambda: FakeAdapter(reply_text="pong"))

    result = await run_headless(tmp_path, ["ping one", "ping two"], runtime=runtime)

    assert [p.text for p in factory.adapters[0].sent_prompts] == ["ping one", "ping two"]
    assert [(t.prompt, t.final_text) for t in result.turns] == [
        ("ping one", "pong"),
        ("ping two", "pong"),
    ]
    assert result.stop_reason == "completed"
    assert len(factory.adapters) == 1  # one chat backed the whole run


async def test_injected_runtime_session_closed_but_runtime_left_alive(tmp_path: Path) -> None:
    runtime, _factory = fake_runtime(tmp_path, lambda: FakeAdapter(reply_text="ok"))

    result = await run_headless(tmp_path, ["go"], runtime=runtime)

    # The run closed ITS session (lock released, adapter stopped) …
    assert runtime.open_session(result.session_id) is None
    # … and the persisted chat survives for inspection / resume.
    assert result.session_id in [c.session_id for c in runtime.list_chats()]


# --- Permissions


@pytest.mark.parametrize("on_permission", ["allow", "reject"], ids=["allow", "reject"])
async def test_permission_prompt_resolved_and_recorded(tmp_path: Path, on_permission: str) -> None:
    runtime, factory = fake_runtime(tmp_path, lambda: _AskingAdapter(_PERMISSION))
    option, text = (
        f"{on_permission}_once",
        {"allow": "allowed", "reject": "rejected"}[on_permission],
    )

    result = await run_headless(
        tmp_path,
        ["edit something"],
        runtime=runtime,
        permission_mode="default",  # an edit PROMPTS in default mode → hits the resolver
        on_permission=on_permission,  # type: ignore[arg-type]
        timeout_seconds=10.0,
    )

    assert factory.adapters[0].permission_replies == [("pr-1", option)]
    recorded = {"request_id": "pr-1", "permission_kind": "edit", "canonical_kind": "edit"}
    recorded |= {"patterns": [], "subject": None, "decision": option}
    assert result.permission_prompts == [recorded]
    assert any(text in t.final_text for t in result.turns)


# --- Questions


async def test_question_auto_declined_with_continueable_reason(tmp_path: Path) -> None:
    runtime, factory = fake_runtime(tmp_path, lambda: _AskingAdapter(_QUESTION))

    result = await run_headless(tmp_path, ["ask me"], runtime=runtime, timeout_seconds=10.0)

    adapter = factory.adapters[0]
    # A REASONED reject — the harness steers on it instead of ending the turn.
    assert adapter.question_rejects == [("q-1", QUESTION_DECLINE_REASON)]
    assert adapter.question_replies == []  # never answered, only declined
    assert result.turns[0].final_text == "proceeded without an answer"
    assert result.stop_reason == "completed"


# --- Failure modes


async def test_timeout_cancels_and_reports_timeout(tmp_path: Path) -> None:
    runtime, factory = fake_runtime(tmp_path, _AskingAdapter)

    result = await run_headless(tmp_path, ["hang"], runtime=runtime, timeout_seconds=0.5)

    assert result.stop_reason == "timeout"
    assert result.error_detail is not None and "0.5" in result.error_detail
    assert factory.adapters[0].cancel_count >= 1
    # The hung turn never produced a completed HeadlessTurn.
    assert all(t.stop_reason != "completed" for t in result.turns)


@pytest.mark.parametrize("terminal", [True, False], ids=["aborted terminal", "no terminal"])
async def test_a_cancelled_turn_reports_cancelled_and_stops_the_run(
    tmp_path: Path, terminal: bool
) -> None:
    """Whichever shape the cancel arrives in, the run reports `cancelled` and the
    prompts behind it never run."""

    class _CancelledAdapter(FakeAdapter):
        async def send_prompt(self, prompt: PromptInput) -> None:
            self._sent_prompts.append(prompt)
            await self._bus.publish(self._status("run", "running", prompt.turn_id))
            if terminal:
                await self._bus.publish(self._status("ab", "aborted", prompt.turn_id))
            else:
                session = next(iter(runtime._sessions.values()))
                self._cancelling = asyncio.create_task(session.cancel())

    runtime, factory = fake_runtime(tmp_path, _CancelledAdapter)

    result = await run_headless(
        tmp_path, ["stop", "never sent"], runtime=runtime, timeout_seconds=10.0
    )

    assert [t.stop_reason for t in result.turns] == ["cancelled"]
    assert result.stop_reason == "cancelled"
    assert [p.text for p in factory.adapters[0].sent_prompts] == ["stop"]


@pytest.mark.parametrize("recovers", [True, False], ids=["recovered", "gave up"])
async def test_an_overflow_reports_its_outcome_and_never_the_suppressed_error(
    tmp_path: Path, recovers: bool
) -> None:
    """An overflowed turn reports the recovery's outcome and never the suppressed
    provider error."""
    raw = "the provider error the runtime suppresses"

    class _OverflowingAdapter(FakeAdapter):
        async def send_prompt(self, prompt: PromptInput) -> None:
            self._sent_prompts.append(prompt)
            await self._bus.publish(self._status("run", "running", prompt.turn_id))
            if recovers and len(self._sent_prompts) > 1:
                await self._emit_scripted_turn("the answer, once it fit", prompt.turn_id)
                return
            await self._bus.publish(
                SessionStatusChanged(
                    event_id=f"of-{len(self._sent_prompts)}",
                    time=_T,
                    session_id="s",
                    status="error",
                    phase="error",
                    detail=raw,
                    overflow=True,
                    turn_id=prompt.turn_id,
                )
            )

    runtime, _factory = fake_runtime(tmp_path, _OverflowingAdapter)

    result = await run_headless(tmp_path, ["say hello"], runtime=runtime, timeout_seconds=20.0)

    turn = result.turns[0]
    assert turn.error_detail != raw
    if recovers:
        assert (turn.stop_reason, turn.final_text) == ("completed", "the answer, once it fit")
        assert turn.error_detail is None
    else:
        assert turn.stop_reason == "error"
        assert turn.error_detail is not None


async def test_a_failing_background_wake_leaves_the_users_turn_alone(tmp_path: Path) -> None:
    """A background wake that speaks and then fails lands as its own continuation,
    so the user's turns keep their own text and their own outcomes."""

    # What the wake says before it fails. It belongs to no user prompt.
    wake_text = "the wake turn talking, not the user's answer"

    class _BackgroundThenFailingWake(FakeAdapter):
        async def send_prompt(self, prompt: PromptInput) -> None:
            self._sent_prompts.append(prompt)
            if len(self._sent_prompts) == 2:  # the wake the flush injected
                await self._bus.publish(self._status("wr", "running", prompt.turn_id))
                await self._bus.publish(
                    PartCreated(
                        event_id="wake-part",
                        time=_T,
                        session_id="s",
                        part=TextPart(part_id="wp", message_id="wm", text=wake_text),
                    )
                )
                await self._bus.publish(self._status("we", "error", prompt.turn_id))
                return
            await self._bus.publish(self._status("r", "running", prompt.turn_id))
            if len(self._sent_prompts) == 1:
                session = next(iter(runtime._sessions.values()))
                session._background.submit(
                    finished_sql_job, kind="sql", title="q", input={"mode": "sql"}
                )
                for _ in range(400):  # let it finish while the turn is still live
                    if session._pending_background:
                        break
                    await asyncio.sleep(0.005)
            await self._emit_scripted_turn(f"answer to {prompt.text}", prompt.turn_id)

    runtime, _factory = fake_runtime(tmp_path, _BackgroundThenFailingWake)

    result = await run_headless(
        tmp_path, ["ask", "and again"], runtime=runtime, timeout_seconds=20.0
    )

    assert [(t.prompt, t.stop_reason, t.origin) for t in result.turns] == [
        ("ask", "completed", "caller"),
        ("[background continuation]", "error", "continuation"),
        ("and again", "completed", "caller"),
    ]
    # The wake's text and its failure are the continuation's alone.
    assert result.turns[1].final_text == wake_text
    assert [t.final_text for t in result.turns if t.origin == "caller"] == [
        "answer to ask",
        "answer to and again",
    ]


async def test_a_stream_that_closes_mid_turn_ends_the_run(tmp_path: Path) -> None:
    """A stream that closes mid-turn ends the run as an error carrying what was
    collected."""

    class _ClosesTheStream(FakeAdapter):
        async def send_prompt(self, prompt: PromptInput) -> None:
            self._sent_prompts.append(prompt)
            await self._bus.publish(self._status("r", "running", prompt.turn_id))
            await self._bus.publish(
                PartCreated(
                    event_id="p",
                    time=_T,
                    session_id="s",
                    part=TextPart(part_id="p1", message_id="m1", text="half an answer"),
                )
            )
            await self._bus.close()

    runtime, factory = fake_runtime(tmp_path, _ClosesTheStream)

    result = await run_headless(
        tmp_path, ["ask", "never sent"], runtime=runtime, timeout_seconds=2.0
    )

    # The harness died under the turn. That is an error with a reason, not a
    # cancel (nobody asked) and not a timeout (the run did not overrun).
    assert [t.stop_reason for t in result.turns] == ["error"]
    assert result.stop_reason == "error"
    assert result.error_detail is not None
    # What it managed to say is still reported, and the queue does not go on.
    assert result.turns[0].final_text == "half an answer"
    assert [p.text for p in factory.adapters[0].sent_prompts] == ["ask"]


async def test_adapter_crash_reports_error_not_raise(tmp_path: Path) -> None:
    runtime, _factory = fake_runtime(tmp_path, lambda: FakeAdapter(crash_after_prompt=True))

    result = await run_headless(tmp_path, ["boom", "never sent"], runtime=runtime)

    assert result.stop_reason == "error"
    assert result.turns[0].stop_reason == "error"
    assert result.turns[0].error_detail is not None
    assert "crash" in result.turns[0].error_detail
    # The crash aborts the remaining prompts.
    assert len(result.turns) == 1


# --- Stop reason: what the caller learns about how each turn ended
# same-author-ok: driven from the stop-reason contract by a context that did not read
# `headless.py`; the turn event shapes come from `FakeAdapter`.

_CUT = ScriptedMessage(text="half an answer", finish_reason="length")


def _turn(*messages: ScriptedMessage, **fields: Any) -> ScriptedTurn:
    return ScriptedTurn(messages, **fields)


def _m(text: str = "x", **fields: Any) -> ScriptedMessage:
    return ScriptedMessage(text=text, **fields)


_TWO_STEP = _turn(
    _m("tool step", finish_reason="stop", message_id="m1"),
    _m("final", finish_reason="length", message_id="m2"),
)
_USER_CUT = _turn(
    _m("the answer", finish_reason="stop", message_id="a"),
    ScriptedMessage(finish_reason="length", message_id="u", role="user"),
)
# (turns, expected stop reasons, final_text)
_STOP_REASONS = [
    ([_turn(_CUT)], ["max_tokens"], "half an answer"),
    ([_turn(_m(finish_reason="max_tokens"))], ["max_tokens"], "x"),
    ([_turn(_m(finish_reason="stop"))], ["completed"], "x"),
    ([_turn(_m(finish_reason=""))], ["completed"], "x"),
    ([_turn(_m(finish_reason="length", role=None))], ["max_tokens"], "x"),
    ([_TWO_STEP], ["max_tokens"], "final"),
    ([_USER_CUT], ["completed"], "the answer"),
    ([_turn(_CUT, turn_stop_reason="refusal")], ["refusal"], "half an answer"),
    ([_turn(_CUT, turn_stop_reason="end_turn")], ["completed"], "half an answer"),
    ([_turn(_CUT), _turn(_m("ok"))], ["max_tokens", "completed"], "ok"),
]
_STOP_REASON_IDS = [
    "cap-hit-length",
    "cap-hit-max_tokens",
    "clean-stop",
    "unset-finish-reason",
    "cut-before-role-announced",
    "last-message-decides",
    "user-message-finish-reason-ignored",
    "typed-reason-wins",
    "typed-end_turn-aliases-to-completed",
    "truncation-does-not-bleed-into-next-turn",
]


@pytest.mark.parametrize(("turns", "expected", "final_text"), _STOP_REASONS, ids=_STOP_REASON_IDS)
async def test_stop_reason_per_turn(
    tmp_path: Path, turns: list[ScriptedTurn], expected: list[str], final_text: str
) -> None:
    # A typed `TurnFinished` wins; without one, the last assistant message's finish
    # reason tells a truncated turn from a settled one, since both go idle. Text that
    # streamed before a cut is still the run's text, flagged partial by the stop reason.
    runtime, _factory = fake_runtime(tmp_path, lambda: FakeAdapter(scripted_turns=turns))
    prompts = [f"p{i}" for i in range(len(turns))]
    result = await run_headless(tmp_path, prompts, runtime=runtime, timeout_seconds=10.0)

    assert [t.stop_reason for t in result.turns] == expected
    assert result.stop_reason == expected[-1]
    assert result.final_text == final_text


async def test_session_error_status_without_a_turn_finished_is_error(tmp_path: Path) -> None:
    turns = [ScriptedTurn((ScriptedMessage(text="partial"),), error_detail="upstream refused")]
    runtime, _factory = fake_runtime(tmp_path, lambda: FakeAdapter(scripted_turns=turns))

    result = await run_headless(tmp_path, ["go"], runtime=runtime, timeout_seconds=10.0)

    assert result.stop_reason == "error"
    assert result.error_detail is not None and "upstream refused" in result.error_detail


# --- Analyst mode: one verification turn after an answer computed from data
# same-author-ok: driven from the analyst-mode contract by a context that did not write
# `runtime.py`; the fake emits the tool events a real data read does.

_V = VERIFICATION_TURN_PROMPT
_WAKE_TAG = "<backgrounded_tool_finished"
_DEADLINE_NOTE = "not re-verified: cancelled at the verification deadline"


def _ev(kind: type[Event], call_id: str = "call", **fields: Any) -> Event:
    return kind(
        event_id=f"{kind.__name__}-{call_id}",
        time=_T,
        session_id="fake",
        tool_call_id=call_id,
        **fields,
    )


def _tool_call(name: str, call_id: str = "call") -> Event:
    return _ev(ToolCall, call_id, message_id="m1", tool_name=name)


def _dispatch(name: str, call_id: str = "call") -> Event:
    """A `call_tool` dispatch update, the opencode shape for a discovered tool."""
    return _ev(ToolCallUpdate, call_id, status="running", input={"name": name})


def _one_shot(name: str, call_id: str = "call") -> Event:
    """The claude shape: one `ToolCall` through the dispatcher naming the tool."""
    return _ev(
        ToolCall,
        call_id,
        message_id="m1",
        tool_name="mcp__alkera__call_tool",
        input={"name": name, "args": {}},
    )


def _completed(call_id: str = "call", error_text: str | None = None) -> Event:
    return _ev(ToolCallUpdate, call_id, status="completed", error_text=error_text)


def _msg(text: str, message_id: str = "m1", **fields: Any) -> ScriptedTurn:
    return ScriptedTurn((ScriptedMessage(text=text, message_id=message_id, **fields),))


_SQL_READ = [_dispatch("sql.query"), _completed()]
_ANSWER = _msg("rows=42")
_RESTATED = _msg("restated rows=42", "m2")
_WAKE_REPLY = _msg("wake reply", "m9")
_Q2 = _msg("rows=7", "m3")
_Q2_RESTATED = _msg("restated rows=7", "m4")
_VERIFY_ERRORS = _turn(_m("re-checking", message_id="m2"), error_detail="verification fell over")


class _Scripted(FakeAdapter):
    """The one analyst fake: `turns` replay in the real adapter's order (running,
    tool events, messages, closing status). The knobs name the deviations a test
    drives: where the tool events go (`tool_turns`, `tools_before_running`,
    `read_between_turns`), a refused verification (`refuse_verification`, or
    `refuse_after_first` for the adapter raising on the next prompt), which turns
    never end (`hang_turns`) with how `cancel` settles them (aborts, silent, hangs),
    an `overflow_first` root, a `slow_root`, and `on_turn` inside a live turn."""

    def __init__(
        self, turns: list[ScriptedTurn], tool_events: list[Event] | None = None, **knobs: Any
    ) -> None:
        super().__init__(scripted_turns=turns)
        self._tool_events = tool_events or []
        self._k = {
            "tool_turns": None,
            "cancel": "aborts",
            "slow_root": 0.0,
            "hang_turns": (),
        } | knobs
        self._tools_emitted_for: int | None = None
        self.on_turn: Callable[[int], Awaitable[None]] | None = None

    def _tools_due(self, turn: int) -> bool:
        return self._k["tool_turns"] is None or turn in self._k["tool_turns"]

    async def _publish_tools(self) -> None:
        for event in self._tool_events:
            await self._bus.publish(event)

    async def send_prompt(self, prompt: PromptInput) -> None:
        from alkera_cli.harness.adapter import HarnessCrashError

        k, n = self._k, len(self._sent_prompts)
        if n == 0 and k["slow_root"]:
            await asyncio.sleep(k["slow_root"])
        if k.get("refuse_verification") and prompt.text == _V:
            raise HarnessCrashError("verifier refused")
        if n in k["hang_turns"]:
            self._sent_prompts.append(prompt)
            await self._bus.publish(self._status(f"hang-{n}", "running", prompt.turn_id))
            return
        if k.get("overflow_first") and n == 0:
            # The root reads data, then overflows; the runtime resends the same prompt.
            self._sent_prompts.append(prompt)
            await self._bus.publish(self._status("root-running", "running", prompt.turn_id))
            await self._publish_tools()
            await self._bus.publish(
                _status(
                    "error",
                    "root-overflow",
                    phase="error",
                    detail="prompt is too long",
                    overflow=True,
                    turn_id=prompt.turn_id,
                )
            )
            return
        await super().send_prompt(prompt)
        if k.get("refuse_after_first") and n == 0:
            self.fail_next_prompt = True
        if k.get("read_between_turns") and n == 0:
            await self._publish_tools()

    async def _replay_turn(self, turn: ScriptedTurn, turn_id: str) -> None:
        if self._k.get("tools_before_running") and self._tools_due(self._turn_no):
            await self._publish_tools()
        await super()._replay_turn(turn, turn_id)

    async def _emit_message(self, message: ScriptedMessage, tag: str, now: datetime) -> None:
        turn = self._turn_no - 1
        if self._tools_emitted_for != turn:
            self._tools_emitted_for = turn
            if not self._k.get("tools_before_running") and self._tools_due(turn):
                await self._publish_tools()
            if self.on_turn is not None:
                await self.on_turn(turn)
        await super()._emit_message(message, tag, now)

    async def cancel(self) -> None:
        await super().cancel()
        if self._k["cancel"] == "hangs":
            await asyncio.Event().wait()
        if self._k["cancel"] == "aborts":
            await self._bus.publish(self._status("v-ab", "aborted", self._last_turn_id()))


def _data(turns: list[ScriptedTurn], **kw: Any) -> Callable[[], _Scripted]:
    return lambda: _Scripted(turns, _SQL_READ, **kw)


def _hanging(**kw: Any) -> Callable[[], _Scripted]:
    return _data([_ANSWER], hang_turns={1}, **kw)


async def _analyst_run(tmp_path: Path, make: Any, prompts: list[str], **kw: Any) -> Any:
    runtime, factory = fake_runtime(tmp_path, make)
    kw.setdefault("timeout_seconds", 10.0)
    result = await run_headless(
        tmp_path, prompts, runtime=runtime, analysis_pipeline="analyst", **kw
    )
    return result, factory.adapters[0]


def _sent(adapter: Any) -> list[str]:
    return [p.text for p in adapter.sent_prompts]


def _turns(result: Any) -> list[tuple[str, str]]:
    return [(t.prompt, t.final_text) for t in result.turns]


def _check(result: Any, final: str, verification: str | None, stop: str = "completed") -> None:
    assert (result.final_text, result.stop_reason, result.verification) == (
        final,
        stop,
        verification,
    )


_Q = ("q-rowcount", "rows=42")
_R = ("[verification]", "restated rows=42")
_Q2T = ("q-second", "rows=7")
_R2 = ("[verification]", "restated rows=7")


async def _settled(session: Any) -> None:
    for _ in range(200):
        busy = (
            session.turn_active
            or session.analysis_verification_pending
            or session.background_jobs_running
        )
        if not busy:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)


def _submit_sql_job_on_turn(runtime: HarnessRuntime, job_turn: int) -> Any:
    """An `on_turn` hook: inside turn `job_turn`, finish a real background
    `sql.query` job on the live session and wait for its card and queued wake."""
    from alkera_cli.plugins.plugin_base.sql_tools import SqlQueryResult

    async def _job() -> SqlQueryResult:
        return SqlQueryResult(columns=["a"], preview_rows=[[1]], row_count=1, result_name="r")

    async def _hook(turn: int) -> None:
        if turn != job_turn:
            return
        (sid,) = runtime.open_session_ids
        session = runtime.open_session(sid)
        assert session is not None
        session._background.submit(_job, kind="sql", title="q", input={"mode": "sql", "sql": "x"})
        for _ in range(100):
            if not session.background_jobs_running:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # the finish card and the queued wake land

    return _hook


def _hooked_runtime(
    tmp_path: Path, make: Any, job_turn: int
) -> tuple[HarnessRuntime, FakeAdapterFactory]:
    runtime, factory = fake_runtime(tmp_path, make)
    hook = _submit_sql_job_on_turn(runtime, job_turn)
    inner = factory._make

    def _make() -> FakeAdapter:
        adapter = inner()
        adapter.on_turn = hook  # type: ignore[attr-defined]
        return adapter

    factory._make = _make
    return runtime, factory


# --- the verification turn: earned, issued once, delivered ------------------


async def test_analyst_data_answer_gets_exactly_one_verification_turn(tmp_path: Path) -> None:
    # The fake reads data on every turn, the verification included: a gate that let a
    # verifier earn another would show as extra prompts (the runaway-loop regression).
    result, adapter = await _analyst_run(tmp_path, _data([_ANSWER, _RESTATED]), ["q-rowcount"])

    assert _sent(adapter) == ["q-rowcount", _V]
    assert _turns(result) == [_Q, _R]
    _check(result, "restated rows=42", "verified")


async def test_overflow_resend_keeps_the_data_read_on_its_logical_root_turn(tmp_path: Path) -> None:
    make = _data([_ANSWER, _RESTATED], tool_turns={1}, overflow_first=True)
    result, adapter = await _analyst_run(tmp_path, make, ["q-overflow"])

    assert adapter.compact_count == 1
    assert _sent(adapter) == ["q-overflow", "q-overflow", _V]
    assert _turns(result) == [("q-overflow", "rows=42"), _R]
    _check(result, "restated rows=42", "verified")


async def test_off_mode_sends_only_the_callers_prompts(tmp_path: Path) -> None:
    runtime, factory = fake_runtime(tmp_path, _data([_ANSWER, _RESTATED]))
    result = await run_headless(tmp_path, ["q-rowcount"], runtime=runtime, timeout_seconds=10.0)

    assert _sent(factory.adapters[0]) == ["q-rowcount"]
    assert _turns(result) == [_Q]
    _check(result, "rows=42", None)


_ERRORED_ANSWER = ScriptedTurn((ScriptedMessage(text="rows=42"),), error_detail="answer fell over")
_NO_READ: list[Event] = []


@pytest.mark.parametrize(
    ("events", "kw"),
    [
        pytest.param(_NO_READ, {}, id="no-data-read"),
        pytest.param([_tool_call("sql.query")], {}, id="call-never-completes"),
        pytest.param([_dispatch("sql.query")], {}, id="dispatch-still-running"),
        pytest.param(
            [_dispatch("sql.query"), _completed(error_text="syntax")],
            {},
            id="read-completed-with-error",
        ),
        pytest.param([_dispatch("read"), _completed()], {}, id="non-data-tool"),
        pytest.param([_one_shot("read"), _completed()], {}, id="non-data-one-shot"),
        pytest.param([_one_shot("sql.query")], {}, id="one-shot-never-completes"),
        pytest.param(_SQL_READ, {"tools_before_running": True}, id="read-completed-before-running"),
        pytest.param(
            [_tool_call("sql.query", "bgjob:1"), _completed("bgjob:1")],
            {},
            id="background-start-card",
        ),
        pytest.param(
            [_tool_call("sql.query", "bgdone:1"), _completed("bgdone:1")],
            {},
            id="background-finish-card",
        ),
        pytest.param(_SQL_READ, {"turns": [_ERRORED_ANSWER, _RESTATED]}, id="answer-turn-errored"),
    ],
)
async def test_no_verification_turn_without_a_completed_data_read_on_a_clean_turn(
    tmp_path: Path, events: list[Event], kw: dict[str, Any]
) -> None:
    turns = kw.pop("turns", [_ANSWER, _RESTATED])
    result, adapter = await _analyst_run(
        tmp_path, lambda: _Scripted(turns, events, **kw), ["q-rowcount"]
    )

    assert _sent(adapter) == ["q-rowcount"]
    assert _turns(result) == [_Q]
    assert (result.verification, result.verification_fired) == (None, False)


@pytest.mark.parametrize(
    ("root", "stop_reason"),
    [
        pytest.param(
            ScriptedTurn((ScriptedMessage(text="rows=42"),), turn_stop_reason="refusal"),
            "refusal",
            id="refused-root",
        ),
        pytest.param(_msg("rows=42", finish_reason="length"), "max_tokens", id="truncated-root"),
    ],
)
async def test_a_failed_root_earns_no_verification_and_keeps_its_stop_reason(
    tmp_path: Path, root: ScriptedTurn, stop_reason: str
) -> None:
    # `alkera run` maps the root's stop reason to exit 1 (test_run_command owns that).
    result, adapter = await _analyst_run(tmp_path, _data([root, _RESTATED]), ["q-rowcount"])

    assert _sent(adapter) == ["q-rowcount"]
    assert (result.stop_reason, result.verification, result.verification_fired) == (
        stop_reason,
        None,
        False,
    )


@pytest.mark.parametrize(
    "events",
    [
        # opencode: a dispatch update names the tool; claude: the call itself does.
        pytest.param([_dispatch("data.join"), _completed()], id="dispatch-update-data.join"),
        pytest.param([_dispatch("blob.query"), _completed()], id="dispatch-update-blob.query"),
        pytest.param([_one_shot("sql.query"), _completed()], id="one-shot-call-sql.query"),
        pytest.param([_one_shot("data.join"), _completed()], id="one-shot-call-data.join"),
    ],
)
async def test_a_completed_data_read_is_recognized_in_both_dispatch_shapes(
    tmp_path: Path, events: list[Event]
) -> None:
    result, adapter = await _analyst_run(
        tmp_path, lambda: _Scripted([_ANSWER, _RESTATED], events), ["q-rowcount"]
    )

    assert len(adapter.sent_prompts) == 2
    assert result.verification == "verified"


async def test_a_read_landing_between_turns_does_not_mark_the_next_turn(tmp_path: Path) -> None:
    result, adapter = await _analyst_run(
        tmp_path,
        _data([_ANSWER, _RESTATED], tool_turns=set(), read_between_turns=True),
        ["q-rowcount", "q-second"],
    )

    assert _sent(adapter) == ["q-rowcount", "q-second"]
    assert result.verification is None


# --- several caller prompts: each owns its verdict and its delivery ----------


_Q2_ERRORS = _turn(_m("partial", message_id="m3"), error_detail="q2 fell over")


_BOTH_VERIFY = [_ANSWER, _RESTATED, _Q2, _Q2_RESTATED]
_THEN_NO_DATA = [_ANSWER, _RESTATED, _Q2]
_THEN_ERROR = [_ANSWER, _RESTATED, _Q2_ERRORS]
# (turns, tool_turns, expected, stop, verification, fired)
_TWO_PROMPTS = [
    (_BOTH_VERIFY, None, [_Q, _R, _Q2T, _R2], "completed", "verified", True),
    (_THEN_NO_DATA, {0}, [_Q, _R, _Q2T], "completed", None, False),
    (_THEN_ERROR, {0, 2}, [_Q, _R, ("q-second", "partial")], "error", None, False),
]
_TWO_PROMPTS_IDS = [
    "every-data-answer-is-verified-before-the-next-prompt-starts",
    "verified-then-a-non-data-prompt-reports-no-verification",
    "verified-then-an-erroring-prompt-reports-that-prompt",
]


@pytest.mark.parametrize(
    ("turns", "tool_turns", "expected", "stop", "verification", "fired"),
    _TWO_PROMPTS,
    ids=_TWO_PROMPTS_IDS,
)
async def test_each_caller_prompt_owns_its_verdict(
    tmp_path: Path,
    turns: list[ScriptedTurn],
    tool_turns: set[int] | None,
    expected: list[tuple[str, str]],
    stop: str,
    verification: str | None,
    fired: bool,
) -> None:
    # One outstanding turn: q2 cannot start until q1's verification ended, so nothing
    # interleaves; a verdict is never carried from q1 into q2's report. Exactly one
    # delivered turn per verified prompt, and it is the restatement.
    result, adapter = await _analyst_run(
        tmp_path, _data(turns, tool_turns=tool_turns), ["q-rowcount", "q-second"]
    )

    # The verification prompt follows each verified caller prompt, in order.
    assert _sent(adapter) == [_V if p == _R[0] else p for p, _ in expected]
    assert _turns(result) == expected
    assert [(t.origin, t.delivered) for t in result.turns] == [
        ("verification", True) if p == _R[0] else ("caller", False) for p, _ in expected
    ]
    _check(result, expected[-1][1], verification, stop)
    assert result.verification_fired is fired


# --- failed verifications: the first answer stands ---------------------------


_DECLINED = ScriptedTurn(
    (ScriptedMessage(text="declined", message_id="m2"),), turn_stop_reason="refusal"
)
_EMPTY = "empty restatement"
# (turns after the answer, refuse_after_first, reason, fired)
_FAIL_OPEN = [
    ([_VERIFY_ERRORS], False, "error", True),
    ([_msg("restated rows=4", "m2", finish_reason="length")], False, "max_tokens", True),
    ([_DECLINED], False, "refusal", True),
    ([ScriptedTurn(())], False, _EMPTY, True),
    ([_msg("", "m2")], False, _EMPTY, True),
    ([_msg("   \n  ", "m2")], False, _EMPTY, True),
    ([], True, "could not fire", False),
]
_FAIL_OPEN_IDS = [
    "errors-after-text",
    "truncated",
    "refused",
    "empty-restatement",
    "no-text",
    "whitespace-only",
    "cannot-fire",
]


@pytest.mark.parametrize(("after", "refuse", "reason", "fired"), _FAIL_OPEN, ids=_FAIL_OPEN_IDS)
async def test_failed_verification_leaves_the_first_answer_standing(
    tmp_path: Path, after: list[ScriptedTurn], refuse: bool, reason: str, fired: bool
) -> None:
    make = _data([_ANSWER, *after], refuse_after_first=refuse)
    result, _adapter = await _analyst_run(tmp_path, make, ["q-rowcount"])

    _check(result, "rows=42", f"not re-verified: {reason}")
    assert _turns(result) == [_Q]
    # `verification_fired` is the harness's account of whether the verifier ran at
    # all; a verifier the adapter refused never left the harness.
    assert result.verification_fired is fired


async def test_a_failure_inside_settlement_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alkera_cli.chat import headless as headless_mod

    def _boom(*_a: Any, **_k: Any) -> tuple[float, float]:
        raise RuntimeError("budget arithmetic broke")

    monkeypatch.setattr(headless_mod, "_verification_budget", _boom)
    result, _adapter = await _analyst_run(tmp_path, _data([_ANSWER, _RESTATED]), ["q-rowcount"])

    assert (result.stop_reason, result.verification) == (
        "completed",
        "not re-verified: settlement failed",
    )
    assert (result.turns[0].prompt, result.turns[0].delivered, result.final_text) == (
        "q-rowcount",
        True,
        "rows=42",
    )


@pytest.mark.parametrize(
    ("after", "seen"),
    [(_RESTATED, ["rows=42", "restated rows=42"]), (_VERIFY_ERRORS, ["rows=42"])],
    ids=["clean-restatement-is-delivered", "rejected-restatement-is-withheld"],
)
async def test_observers_see_the_verification_text_only_when_it_is_the_delivery(
    tmp_path: Path, after: ScriptedTurn, seen: list[str]
) -> None:
    texts: list[str] = []

    def _cb(event: Any) -> None:
        part = getattr(event, "part", None)
        if isinstance(part, TextPart) and part.text and not part.synthetic:
            texts.append(part.text)

    await _analyst_run(tmp_path, _data([_ANSWER, after]), ["q-rowcount"], on_event=_cb)
    assert texts == seen


# --- a verification turn that never ends -------------------------------------


# The default 900s budget is capped to the run deadline less two grace periods; the
# drain polls once a second and anchors the deadline on its first poll after the idle
# edge, so the grace leaves room for both. The shipped 30s grace shrinks to a quarter
# of the run budget; a slow root leaves only a tiny budget. (cancel, slow_root,
# budget, run_timeout, grace)
#
# The cases that let the grace shrink (grace=None) have to keep it above that 1s
# drain poll, or the invariant the line above states is not the one being tested.
# Grace is a QUARTER of what is left after the root, so `run_timeout - slow_root`
# has to clear ~4s once harness startup is paid out of it -- and startup is the
# part that varies, which is why the margin is stated here rather than tuned to
# whatever a warm process happens to manage. `slow-root-under-a-short-run` used
# 5.5s under a 6.0s run: a 0.5s remainder, a 0.125s grace, and a 1s poll to fit
# inside it. It passed only when the process was already warm from the cases
# above it, and failed alone or first in a shard.
_NEVER_ENDS = [
    ("aborts", 0.0, 0.2, 5.0, 0.5),
    ("silent", 0.0, 0.2, 5.0, 0.5),
    ("hangs", 0.0, 0.2, 5.0, 0.5),
    ("aborts", 0.0, None, 6.0, 1.5),
    ("aborts", 0.0, None, 8.0, None),
    ("aborts", 2.0, None, 8.0, None),
]
_NEVER_ENDS_IDS = [
    "cancel-settles-the-turn",
    "cancel-is-silent-gives-up",
    "cancel-never-returns-gives-up",
    "run-deadline-caps-the-default-budget",
    "production-grace-under-a-short-run",
    "slow-root-under-a-short-run",
]


@pytest.mark.parametrize(
    ("cancel", "slow_root", "budget", "run_timeout", "grace"), _NEVER_ENDS, ids=_NEVER_ENDS_IDS
)
async def test_a_verification_turn_that_never_ends_is_cancelled_at_the_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: str,
    slow_root: float,
    budget: float | None,
    run_timeout: float,
    grace: float | None,
) -> None:
    from alkera_cli.chat import headless as headless_mod

    if grace is not None:
        monkeypatch.setattr(headless_mod, "_VERIFICATION_GIVE_UP_SECONDS", grace)
    make = _hanging(cancel=cancel, slow_root=slow_root)
    result, adapter = await _analyst_run(
        tmp_path,
        make,
        ["q-rowcount"],
        verification_timeout_seconds=budget,
        timeout_seconds=run_timeout,
    )

    assert adapter.cancel_count >= 1
    _check(result, "rows=42", _DEADLINE_NOTE)
    assert _turns(result) == [_Q]


async def test_a_cancelled_verification_does_not_abandon_the_prompts_behind_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A silent cancel still settles the turn, so the session is free and the queue
    # goes on. Only a cancel that never returns wedges the run (its own case below).
    from alkera_cli.chat import headless as headless_mod

    monkeypatch.setattr(headless_mod, "_VERIFICATION_GIVE_UP_SECONDS", 0.5)
    make = _data([_ANSWER], hang_turns={1}, cancel="silent")
    result, adapter = await _analyst_run(
        tmp_path,
        make,
        ["q-rowcount", "q-after-the-cancel"],
        verification_timeout_seconds=0.2,
        timeout_seconds=8.0,
    )

    assert _sent(adapter) == ["q-rowcount", _V, "q-after-the-cancel", _V]
    assert _turns(result) == [_Q, ("q-after-the-cancel", "rows=42"), (_R[0], "rows=42")]
    # The cancelled prompt's own answer is its delivery; the prompt behind it earns
    # its own verdict rather than inheriting the cancel.
    assert [t.delivered for t in result.turns] == [True, False, True]
    _check(result, "rows=42", "verified")
    assert result.error_detail is None


async def test_a_verification_cancelled_on_the_last_prompt_still_completes_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Identical prompt texts: each prompt's verdict is read by position, never
    # matched by text, so the verified first one can't stand in for the cancelled one.
    from alkera_cli.chat import headless as headless_mod

    monkeypatch.setattr(headless_mod, "_VERIFICATION_GIVE_UP_SECONDS", 0.5)
    make = _data([_ANSWER, _RESTATED, _ANSWER], hang_turns={3}, cancel="silent")
    result, adapter = await _analyst_run(
        tmp_path, make, ["q-a", "q-a"], verification_timeout_seconds=0.2, timeout_seconds=8.0
    )

    assert _sent(adapter) == ["q-a", _V, "q-a", _V]
    assert [t.prompt for t in result.turns] == ["q-a", _R[0], "q-a"]
    _check(result, "rows=42", _DEADLINE_NOTE)
    assert result.error_detail is None


async def test_an_analyst_run_without_a_run_deadline_still_settles(tmp_path: Path) -> None:
    result, _adapter = await _analyst_run(
        tmp_path, _data([_ANSWER, _RESTATED]), ["q-rowcount"], timeout_seconds=None
    )

    _check(result, "restated rows=42", "verified")


# --- background jobs finishing while a turn is live ---------------------------

_BG = "[background continuation]"

_W = (_BG, "wake reply")
_RB = (_BG, "restated rows=42")
_WAKE_RUN = [_ANSWER, _RESTATED, _WAKE_REPLY]
_SEEN = ("wake reply", "re-checking")
_ERR_THEN_WAKE = [_ANSWER, _VERIFY_ERRORS, _WAKE_REPLY]
_NO_VERIFY_WAKE = [_ANSWER, _WAKE_REPLY]
_NRV_ERROR = "not re-verified: error"
_NRV_NO_FIRE = "not re-verified: could not fire"
# A real background `sql.query` finishes while a turn is live: its finish card is
# never the turn's own read; its wake runs after the verification the root earned,
# never in its place, never erasing the verdict; it lands as a continuation whatever
# the mode. A wake turn reading data earns nothing (it is not a caller prompt). A wake
# queued behind a failed or never-fired verification still runs, lands as a
# continuation, and reaches observers; the rejected restatement does not.
# (mode, turns, tool_turns, refuse, job_turn, expected, verification, fired, seen)
_MID_TURN = [
    ("analyst", _WAKE_RUN, {0}, False, 1, [_Q, _R, _W], "verified", True, None),
    ("analyst", _WAKE_RUN, {0}, False, 0, [_Q, _R, _W], "verified", True, None),
    ("analyst", _WAKE_RUN, {1}, False, 0, [_Q, _RB], None, False, None),
    ("off", _WAKE_RUN, {0}, False, 0, [_Q, _RB], None, False, None),
    ("analyst", _ERR_THEN_WAKE, {0}, False, 0, [_Q, _W], _NRV_ERROR, True, _SEEN),
    ("analyst", _NO_VERIFY_WAKE, {0}, True, 0, [_Q, _W], _NRV_NO_FIRE, False, _SEEN),
]
_MID_TURN_IDS = [
    "job-finishing-during-the-verification-keeps-the-verdict",
    "job-during-the-root-turn",
    "wake-reading-data-after-a-no-data-root-earns-nothing",
    "off-mode-reports-the-same-continuation",
    "verification-errors-then-a-wake",
    "fire-raises-then-a-wake",
]


@pytest.mark.parametrize(
    (
        "mode",
        "turns",
        "tool_turns",
        "refuse",
        "job_turn",
        "expected",
        "verification",
        "fired",
        "seen",
    ),
    _MID_TURN,
    ids=_MID_TURN_IDS,
)
async def test_a_background_sql_job_finishing_mid_turn(
    tmp_path: Path,
    mode: str,
    turns: list[ScriptedTurn],
    tool_turns: set[int],
    refuse: bool,
    job_turn: int,
    expected: list[tuple[str, str]],
    verification: str | None,
    fired: bool,
    seen: tuple[str, str] | None,
) -> None:
    texts: list[str] = []

    def _cb(event: Any) -> None:
        part = getattr(event, "part", None)
        if isinstance(part, TextPart) and part.text and not part.synthetic:
            texts.append(part.text)

    make = _data(turns, tool_turns=tool_turns, refuse_verification=refuse)
    runtime, factory = _hooked_runtime(tmp_path, make, job_turn)
    result = await run_headless(
        tmp_path,
        ["q-rowcount"],
        runtime=runtime,
        analysis_pipeline=mode,
        timeout_seconds=10.0,
        on_event=_cb,
    )

    assert _turns(result) == expected
    # The delivery: the verified restatement; else the caller's answer when a
    # verification was owed but failed or never fired; else the last turn.
    if verification == "verified":
        delivered = _R[1]
    elif verification is not None:
        delivered = expected[0][1]
        assert result.turns[0].delivered is True
    else:
        delivered = expected[-1][1]
        assert not any(t.delivered for t in result.turns)
    _check(result, delivered, verification)
    assert result.verification_fired is fired
    sent = _sent(factory.adapters[0])
    assert sent[0] == "q-rowcount" and any(_WAKE_TAG in s for s in sent)
    if seen is not None:
        assert seen[0] in texts and seen[1] not in texts


_C = "caller"
_ANALYST_FLOW = [
    (*_Q, _C, False),
    (*_R, "verification", True),
    (_BG, "WAKE REPLY", "continuation", False),
]
_ANALYST_FLOW += [(*_Q2T, _C, False), (*_R2, "verification", True)]
_OFF_FLOW = [(*_Q, _C, False), (*_RB, "continuation", False), ("q-second", "WAKE REPLY", _C, False)]
_BETWEEN = [("analyst", 1, _ANALYST_FLOW, "verified"), ("off", 0, _OFF_FLOW, None)]


@pytest.mark.parametrize(
    ("mode", "job_turn", "expected", "verification"), _BETWEEN, ids=["analyst", "off"]
)
async def test_a_wake_between_prompts_never_becomes_the_next_prompts_answer(
    tmp_path: Path,
    mode: str,
    job_turn: int,
    expected: list[tuple[str, str, str, bool]],
    verification: str | None,
) -> None:
    # The wake is drained as a continuation before q2 is sent, so q2 owns its answer.
    wake = _msg("WAKE REPLY", "m5")
    turns = (
        [_ANSWER, _RESTATED, wake, _Q2, _Q2_RESTATED]
        if mode == "analyst"
        else [_ANSWER, _RESTATED, wake]
    )
    runtime, _factory = _hooked_runtime(tmp_path, _data(turns, tool_turns={0, 3}), job_turn)
    result = await run_headless(
        tmp_path,
        ["q-rowcount", "q-second"],
        runtime=runtime,
        analysis_pipeline=mode,
        timeout_seconds=15.0,
    )

    assert [(t.prompt, t.final_text, t.origin, t.delivered) for t in result.turns] == expected
    _check(result, expected[-1][1], verification)


async def test_turn_origin_names_who_issued_the_turn_in_flight(tmp_path: Path) -> None:
    runtime, factory = fake_runtime(
        tmp_path, _data([_ANSWER, _RESTATED, _WAKE_REPLY], tool_turns={0})
    )
    session = await runtime.open_chat(create=True, harness_type="agent")
    origins: list[str | None] = []
    hook = _submit_sql_job_on_turn(runtime, 1)

    async def _observe(turn: int) -> None:
        origins.append(session.turn_origin)
        await hook(turn)

    factory.adapters[0].on_turn = _observe
    try:
        assert session.turn_origin is None
        session.set_analysis_pipeline("analyst")
        await session.send_prompt("q-rowcount")
        await _settled(session)
        assert origins == ["caller", "verification", "wake"]
        assert session.turn_origin is None
    finally:
        await runtime.close_chat(session.session_id)


# --- the selector, the guidance, who may verify --------------------------------


async def test_a_resumed_run_keeps_the_persisted_selector_unless_told_otherwise(
    tmp_path: Path,
) -> None:
    # The selector lives in the manifest; its truth is read through `run_headless`.
    runtime, _factory = fake_runtime(tmp_path, _data([_ANSWER, _RESTATED]))

    async def _run(**kw: Any) -> Any:
        return await run_headless(
            tmp_path, ["q-rowcount"], runtime=runtime, timeout_seconds=10.0, **kw
        )

    first = await _run(analysis_pipeline="analyst")
    sid = first.session_id
    resumed = await _run(resume_session_id=sid)
    forced_off = await _run(resume_session_id=sid, analysis_pipeline="off")
    after = await _run(resume_session_id=sid)

    assert (first.verification, resumed.session_id, resumed.verification) == (
        "verified",
        sid,
        "verified",
    )
    assert (forced_off.verification, forced_off.verification_fired) == (None, False)
    assert after.verification is None  # the explicit off persisted


@pytest.mark.parametrize(
    "mode", [pytest.param("analyst", id="analyst"), pytest.param("off", id="off")]
)
async def test_analyst_guidance_reaches_the_adapter_only_in_analyst_mode(
    tmp_path: Path, mode: str
) -> None:
    from alkera_cli.harness.system_prompt import ANALYST_BLOCK

    runtime, factory = fake_runtime(tmp_path, _data([_ANSWER, _RESTATED]))
    await run_headless(
        tmp_path, ["q-rowcount"], runtime=runtime, analysis_pipeline=mode, timeout_seconds=10.0
    )

    prompts = factory.adapters[0].sent_prompts
    assert [ANALYST_BLOCK.strip() in (p.system or "") for p in prompts] == [
        mode == "analyst"
    ] * len(prompts)
    assert len({p.agent for p in prompts}) == 1  # the verification keeps the session's mode


async def test_set_analysis_pipeline_rejects_unknown_modes(tmp_path: Path) -> None:
    runtime, _factory = fake_runtime(tmp_path, lambda: FakeAdapter(reply_text="x"))
    session = await runtime.open_chat(create=True, harness_type="agent")
    try:
        with pytest.raises(ValueError, match="off"):
            session.set_analysis_pipeline("deep")  # type: ignore[arg-type]
        assert session.analysis_pipeline == "off"
    finally:
        await runtime.close_chat(session.session_id)


async def test_subagent_session_never_verifies(tmp_path: Path) -> None:
    runtime, factory = fake_runtime(tmp_path, _data([_ANSWER, _RESTATED]))
    child_manifest = await runtime.new_chat(harness_type="agent", parent_session_id="parent-sid")
    child = await runtime.open_chat(child_manifest.session_id, harness_type="agent")
    try:
        child.set_analysis_pipeline("analyst")
        await child.send_prompt("q-rowcount")
        await _settled(child)
        assert (_sent(factory.adapters[0]), child.verification_outcome) == (["q-rowcount"], None)
    finally:
        await runtime.close_chat(child.session_id)


# --- Input validation (caller errors raise HeadlessError)


async def _unavailable(**_kwargs: Any) -> list[Any]:
    raise GatewayUnavailableError("connection refused")


_SIGNED_IN = {
    "profile_for_project": lambda _project: types.SimpleNamespace(token="tok"),
    "fetch_catalog": _unavailable,
}
# (run kwargs, patches on alkera_cli.chat.headless, the error the caller must fix)
_CALLER_ERRORS = [
    ({"prompts": []}, {}, "No prompt"),
    ({"harness": "nope"}, {}, "Unknown harness"),
    ({"model": "some-model", "runtime": "injected"}, {}, "injected runtime"),
    ({}, {"profile_for_project": lambda _project: None}, "alkera login"),
    ({}, _SIGNED_IN, "gateway unavailable"),
]
_CALLER_ERROR_IDS = ["no-prompts", "unknown-harness", "model-with-injected-runtime"]
_CALLER_ERROR_IDS += ["not-signed-in", "gateway-unavailable"]


@pytest.mark.parametrize(("kw", "patches", "match"), _CALLER_ERRORS, ids=_CALLER_ERROR_IDS)
async def test_caller_errors_raise_headless_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kw: dict[str, Any],
    patches: dict[str, Any],
    match: str,
) -> None:
    for name, value in patches.items():
        monkeypatch.setattr(f"alkera_cli.chat.headless.{name}", value)
    if kw.get("runtime") == "injected":
        kw["runtime"], _factory = fake_runtime(tmp_path, lambda: FakeAdapter(reply_text="x"))
    prompts = kw.pop("prompts", ["hi"])
    with pytest.raises(HeadlessError, match=match):
        await run_headless(tmp_path, prompts, **kw)


# --- Events capture


async def test_events_jsonl_captures_the_stream(tmp_path: Path) -> None:
    runtime, _factory = fake_runtime(tmp_path, lambda: FakeAdapter(reply_text="hey"))
    events_path = tmp_path / "out" / "events.jsonl"

    await run_headless(tmp_path, ["go"], runtime=runtime, events_path=events_path)

    lines = events_path.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines]
    assert len(parsed) >= 3
    types_seen = {p["event_type"] for p in parsed}
    assert "session.status_changed" in types_seen
    assert "part.created" in types_seen


async def test_on_event_callback_sees_events_and_survives_raising(tmp_path: Path) -> None:
    runtime, _factory = fake_runtime(tmp_path, lambda: FakeAdapter(reply_text="hey"))
    seen: list[str] = []

    def _cb(event: Any) -> None:
        seen.append(event.event_type)
        raise RuntimeError("callback bug must not kill the run")

    result = await run_headless(tmp_path, ["go"], runtime=runtime, on_event=_cb)

    assert result.stop_reason == "completed"
    assert "part.created" in seen


# --- resolve_model_selection (pure catalog resolution shared by every CLI chat)


def _gm(model_id: str, efforts: tuple[str, ...] = ()) -> GatewayModel:
    return GatewayModel(id=model_id, display_name=model_id, wire="anthropic", efforts=efforts)


class TestResolveModelSelection:
    def test_finds_model_by_id(self) -> None:
        models = [_gm("a"), _gm("b")]
        assert resolve_model_selection(models, "b", None) is models[1]

    def test_unknown_model_lists_available(self) -> None:
        with pytest.raises(ValueError, match=r"Unknown model 'zz'.*a, b"):
            resolve_model_selection([_gm("a"), _gm("b")], "zz", None)

    def test_unknown_model_empty_catalog(self) -> None:
        with pytest.raises(ValueError, match=r"\(none\)"):
            resolve_model_selection([], "zz", None)

    def test_effort_offered_ok(self) -> None:
        model = _gm("a", efforts=("low", "xhigh"))
        assert resolve_model_selection([model], "a", "xhigh") is model

    def test_effort_not_offered_raises(self) -> None:
        with pytest.raises(ValueError, match="doesn't offer effort 'xhigh'"):
            resolve_model_selection([_gm("a", efforts=("low",))], "a", "xhigh")

    def test_none_effort_skips_validation(self) -> None:
        assert resolve_model_selection([_gm("a")], "a", None).id == "a"
