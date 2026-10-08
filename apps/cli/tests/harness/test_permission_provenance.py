"""Who decided a permission ask is on the record honestly, in every branch.

A cloud chat's reader sees one thing about a resolved ask: the transcript's
``permission.resolved`` and its ``decided_by``. Both harness backends emit that
event from their own reply path, which knows the option and nothing about WHO
chose it — so every resolution used to read ``decided_by: "user"``, including
the fence's refusal of a write to ``/`` that no person ever saw. The session is
the one place that knows, so it stamps the provenance on the event on its way
to the bus: ``user`` only for a person's answer, ``timeout`` when the prompt
ran out, ``policy`` for everything the session decided itself.

The other half is the resolver that refuses on its own grounds: a mirror's
write fence used to answer the broker with a bare ``reject_once``, which the
session recorded as the human's choice and sent to the model reason-less —
ending the turn. A resolver now raises ``ResolverRefusedError``: the session records
its provenance and hands the model the reason, so the turn keeps going.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import fence
from alkera_cli.harness import ChatSession, HarnessRuntime, PathFence, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.permission_broker import ResolverRefusedError
from alkera_cli.harness.permission_mode import PermissionMode
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    Event,
    PermissionOption,
    PermissionOptionId,
    PermissionRequest,
    PermissionResolved,
)

_T = datetime(2026, 9, 15, tzinfo=UTC)

#: How long a wait on the session's own signal may take before it is a failure
#: rather than a slow machine. Only a bound: every wait below returns the moment
#: its event lands. The first write ask in a fresh test worker goes through the
#: file-edit evidence lookup, whose deferred imports and manifest read run on a
#: worker thread; on a loaded Windows gate runner that alone has taken over five
#: seconds. Kept well inside the suite's per-test timeout so a lost ask still
#: fails here, by name, instead of as a timeout.
SETTLE_BOUND = 30.0
FOLDER_ONLY = "I can only save files inside this chat's own folder."
OUTSIDE = "I can only read files inside this workspace."


def _ask(request_id: str = "req-1", *, raw: str | None = "notes.md") -> PermissionRequest:
    """A write ask. With ``raw`` it carries a typed subject and is decided by the
    engine; without one it takes the coarse kind-only path. Both must prompt in
    ``default`` and refuse in ``read_only``."""
    subject = (
        {
            "capability": "fs",
            "effect": "write",
            "operation": "edit",
            "raw": raw,
            "targets": [{"kind": "file", "name": raw}],
            "classifier": "opencode-tool",
        }
        if raw is not None
        else None
    )
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_T,
        session_id="prov-chat",
        request_id=request_id,
        tool_call_id=f"call-{request_id}",
        permission_kind="edit",
        canonical_kind="edit",
        patterns=[raw] if raw else [],
        subject=subject,
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="reject_once", name="Reject"),
        ],
    )


def _replied(request_id: str, option_id: str) -> PermissionResolved:
    """What a harness backend emits once it has been answered — opencode's
    ``permission.replied`` and the Claude adapter's own echo both carry only the
    option, so the event arrives with the schema's default provenance."""
    return PermissionResolved(
        event_id=f"done-{request_id}",
        time=_T,
        session_id="prov-chat",
        request_id=request_id,
        option_id=option_id,  # type: ignore[arg-type]
    )


@dataclass
class _Driver:
    session: ChatSession
    factory: FakeAdapterFactory
    answers: dict[str, asyncio.Future[PermissionOptionId]]
    events: list[Event] = field(default_factory=list)

    @property
    def adapter(self) -> FakeAdapter:
        return self.factory.adapters[0]

    def reply_for(self, request_id: str) -> tuple[str, str | None] | None:
        for (replied, option), (_, reason) in zip(
            self.adapter.permission_replies, self.adapter.permission_reply_reasons, strict=True
        ):
            if replied == request_id:
                return str(option), reason
        return None

    async def ask(self, request: PermissionRequest) -> tuple[str, str | None]:
        await self.adapter.feed(request)
        return await self.reply(request.request_id)

    async def parked(self, request_id: str) -> asyncio.Future[PermissionOptionId]:
        """Wait until the ask has reached the person, and hand back what they
        answer through.

        The resolver files its future the moment the session hands it the ask,
        so this is the session's own signal that it is prompting rather than
        deciding for itself. A fixed sleep in its place asserts whatever the
        event loop happened to have run by then, which on a machine running
        sixty other test processes can be none of it.
        """
        deadline = asyncio.get_running_loop().time() + SETTLE_BOUND
        while asyncio.get_running_loop().time() < deadline:
            pending = self.answers.get(request_id)
            if pending is not None:
                return pending
            await asyncio.sleep(0.01)
        raise AssertionError(f"{request_id} never reached the person")

    async def reply(self, request_id: str) -> tuple[str, str | None]:
        deadline = asyncio.get_running_loop().time() + SETTLE_BOUND
        while asyncio.get_running_loop().time() < deadline:
            found = self.reply_for(request_id)
            if found is not None:
                return found
            await asyncio.sleep(0.01)
        raise AssertionError(f"no answer for {request_id}")

    async def resolved(self, request_id: str, option_id: str) -> PermissionResolved:
        """Let the harness echo its reply, and return the event the session's
        subscribers — the transcript, a cloud mirror — actually received."""
        await self.adapter.feed(_replied(request_id, option_id))
        deadline = asyncio.get_running_loop().time() + SETTLE_BOUND
        while asyncio.get_running_loop().time() < deadline:
            for event in self.events:
                if isinstance(event, PermissionResolved) and event.request_id == request_id:
                    return event
            await asyncio.sleep(0.01)
        raise AssertionError(f"permission.resolved for {request_id} never reached a subscriber")


@pytest.fixture
async def open_session(tmp_path: Path) -> AsyncIterator[Callable[..., Awaitable[_Driver]]]:
    runtimes: list[HarnessRuntime] = []
    pumps: list[asyncio.Task[None]] = []

    async def _open(
        *,
        mode: PermissionMode = "default",
        prompt_timeout: float | None = None,
        refuse: ResolverRefusedError | None = None,
        fenced: bool = False,
    ) -> _Driver:
        workspace = tmp_path / "work"
        workspace.mkdir(exist_ok=True)
        answers: dict[str, asyncio.Future[PermissionOptionId]] = {}

        async def _resolver(request: PermissionRequest) -> PermissionOptionId:
            if refuse is not None:
                raise refuse
            fut: asyncio.Future[PermissionOptionId] = asyncio.get_running_loop().create_future()
            answers[request.request_id] = fut
            return await fut

        factory = FakeAdapterFactory(FakeAdapter, available=True)
        runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
        runtimes.append(runtime)
        session = await runtime.open_chat(
            create=True,
            harness_type="agent",
            permission_broker=PermissionBroker(_resolver, default_timeout_seconds=prompt_timeout),
            path_fence=(
                PathFence(
                    escape=lambda request: fence.ask_escape(request, root=workspace),
                    reason=OUTSIDE,
                )
                if fenced
                else None
            ),
        )
        session.set_permission_mode(mode)
        driver = _Driver(session=session, factory=factory, answers=answers)
        sub = session.subscribe()

        async def _pump() -> None:
            async for event in sub:
                driver.events.append(event)

        pumps.append(asyncio.get_running_loop().create_task(_pump()))
        await asyncio.sleep(0.02)
        return driver

    try:
        yield _open
    finally:
        for pump in pumps:
            pump.cancel()
        for runtime in runtimes:
            for session_id in list(runtime.open_session_ids):
                await runtime.close_chat(session_id)


# --------------------------------------------------------------------------- #
# default mode: the prompt is pending until a person answers, and then it is theirs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["notes.md", None], ids=["engine", "coarse"])
@pytest.mark.parametrize("option", ["allow_once", "reject_once"])
async def test_a_default_mode_prompt_waits_for_the_person_and_is_then_theirs(
    open_session: Callable[..., Awaitable[_Driver]], raw: str | None, option: PermissionOptionId
) -> None:
    driver = await open_session(mode="default")
    ask = _ask(raw=raw)
    await driver.adapter.feed(ask)
    pending = await driver.parked(ask.request_id)

    # Let every callback the ask scheduled run before reading the adapter: with
    # nothing left to run and still no reply, the one that arrives below is the
    # person's answer and not the loop catching up.
    for _ in range(10):
        await asyncio.sleep(0)
    assert driver.reply_for(ask.request_id) is None, "nobody answered, so nothing may be replied"
    assert not pending.done(), "the ask is parked on the person"

    pending.set_result(option)
    assert await driver.reply(ask.request_id) == (option, None)
    event = await driver.resolved(ask.request_id, option)
    assert (event.option_id, event.decided_by) == (option, "user")
    assert [r.decided_by for r in driver.session.decision_sink.read()] == ["human"]


# --------------------------------------------------------------------------- #
# the session's own decisions are policy, never the person's
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["notes.md", None], ids=["engine", "coarse"])
async def test_a_mode_refusal_is_policy_not_the_person(
    open_session: Callable[..., Awaitable[_Driver]], raw: str | None
) -> None:
    driver = await open_session(mode="read_only")
    option, reason = await driver.ask(_ask(raw=raw))
    assert option == "reject_once"
    assert reason and "read-only" in reason.lower(), "the model is told why"
    assert driver.answers == {}, "read_only never asks anyone"
    event = await driver.resolved("req-1", "reject_once")
    assert event.decided_by == "policy"
    assert driver.adapter.cancel_count == 0


async def test_a_fenced_location_is_policy_not_the_person(
    open_session: Callable[..., Awaitable[_Driver]],
) -> None:
    driver = await open_session(mode="default", fenced=True)
    assert await driver.ask(_ask(raw="/etc/motd")) == ("reject_once", OUTSIDE)
    assert driver.answers == {}
    event = await driver.resolved("req-1", "reject_once")
    assert event.decided_by == "policy"


@pytest.mark.parametrize("raw", ["notes.md", None], ids=["engine", "coarse"])
async def test_a_resolver_that_refuses_on_its_own_grounds_is_policy_and_keeps_the_turn(
    open_session: Callable[..., Awaitable[_Driver]], raw: str | None
) -> None:
    """The cloud mirror's write fence: it refuses before any reader is asked, so
    the record must not say a person chose, and the model must be told the
    reason (a reason-bearing reject is recoverable; a bare one ends the turn)."""
    driver = await open_session(
        mode="default", refuse=ResolverRefusedError(reason=FOLDER_ONLY, decided_by="fence")
    )
    assert await driver.ask(_ask(raw=raw)) == ("reject_once", FOLDER_ONLY)
    assert driver.adapter.cancel_count == 0, "a fence refusal must not end the turn"
    event = await driver.resolved("req-1", "reject_once")
    assert event.decided_by == "policy"
    assert [r.decided_by for r in driver.session.decision_sink.read()] == ["fence"]


@pytest.mark.parametrize("given", ["", "   "], ids=["empty", "blank"])
async def test_a_policy_refusal_always_carries_a_reason(
    open_session: Callable[..., Awaitable[_Driver]], given: str
) -> None:
    """The walkthrough's `ls ..`: a policy refusal reached the model as the
    harness's bare sentence, which reads as a person's decision with no reason."""
    driver = await open_session(
        mode="default", refuse=ResolverRefusedError(reason=given, decided_by="fence")
    )
    option, sent = await driver.ask(_ask())
    assert option == "reject_once"
    assert sent and sent.strip(), "a policy refusal is never reason-less"
    assert sent != "Permission was not granted for this call."


async def test_a_fence_refusal_is_the_policy_s_reason_alone(
    open_session: Callable[..., Awaitable[_Driver]],
) -> None:
    reason = (
        'The workspace policy refused this read: "/etc/shadow" is outside this '
        "chat's workspace; read inside /work instead."
    )
    driver = await open_session(
        mode="default", refuse=ResolverRefusedError(reason=reason, decided_by="fence")
    )
    option, sent = await driver.ask(_ask())
    assert (option, sent) == ("reject_once", reason)


@pytest.mark.parametrize(
    ("decided_by", "reason", "expected"),
    [
        pytest.param("human", None, None, id="person-no-feedback-ends-the-turn"),
        pytest.param("human", "  ", None, id="person-blank-feedback"),
        pytest.param(
            "human",
            "use the staging table",
            "Permission was not granted for this call. use the staging table",
            id="person-with-feedback",
        ),
        pytest.param("timeout", None, None, id="timeout-stays-reasonless"),
        pytest.param("broker", "no reader", "no reader", id="broker-keeps-its-words"),
        pytest.param("fence", "Outside.", "Outside.", id="fence-reason-alone"),
        pytest.param(
            "mode", None, "The workspace policy refused this call.", id="policy-no-reason"
        ),
        pytest.param("judge", "Refused.", "Refused.", id="judge-reason-alone"),
    ],
)
def test_refusal_feedback_speaks_for_whoever_decided(
    decided_by: str, reason: str | None, expected: str | None
) -> None:
    from alkera_cli.harness.permission_mode import refusal_feedback

    assert refusal_feedback(decided_by, reason) == expected
    if expected is not None:
        assert "user rejected" not in expected.lower()


@pytest.mark.parametrize("raw", ["notes.md", None], ids=["engine", "coarse"])
async def test_a_prompt_that_runs_out_is_a_timeout_not_the_person(
    open_session: Callable[..., Awaitable[_Driver]], raw: str | None
) -> None:
    driver = await open_session(mode="default", prompt_timeout=0.05)
    assert await driver.ask(_ask(raw=raw)) == ("reject_once", None)
    event = await driver.resolved("req-1", "reject_once")
    assert event.decided_by == "timeout"
    assert [r.decided_by for r in driver.session.decision_sink.read()] == ["timeout"]


async def test_a_backend_echo_for_an_ask_this_session_never_decided_is_left_alone(
    open_session: Callable[..., Awaitable[_Driver]],
) -> None:
    """A resolution the session has no decision for — a resumed harness replaying
    an old reply — is not re-attributed; the schema's default stands."""
    driver = await open_session(mode="default")
    event = await driver.resolved("req-unknown", "allow_once")
    assert event.decided_by == "user"
