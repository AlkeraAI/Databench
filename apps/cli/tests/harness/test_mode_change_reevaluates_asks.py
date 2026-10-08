"""A mode change decides again every ask a person is still being shown.

An ask is decided under the stance in force when it arrives, and a person is
asked only when that stance says to. Changing the stance while the prompt is up
used to leave it pending under the old stance's question: a reader who switched
to bypass still had to press Allow, and one who switched to read-only could
still allow a write the stance they had just chosen refuses. The session now
runs the pending ask through the same ladder again, as if it had arrived under
the new stance: an answer the stance gives on its own settles it (as policy,
on the record with the mode change named), and an ask it still puts to a person
keeps the very prompt already up.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.harness import ChatSession, HarnessRuntime, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.permission_mode import ALL_MODES, PermissionMode
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    Event,
    PermissionOption,
    PermissionOptionId,
    PermissionRequest,
    PermissionResolved,
)

_T = datetime(2026, 9, 29, tzinfo=UTC)

#: How long a wait on the session's own signal may take before it is a failure
#: rather than a slow machine. Only a bound: every wait below returns the moment
#: its event lands. The first write ask in a fresh test worker goes through the
#: file-edit evidence lookup, whose deferred imports and manifest read run on a
#: worker thread; on a loaded Windows gate runner that alone has taken over five
#: seconds. Kept well inside the suite's per-test timeout so a lost ask still
#: fails here, by name, instead of as a timeout.
SETTLE_BOUND = 30.0


def _write_ask(request_id: str = "req-1", *, raw: str | None = "notes.md") -> PermissionRequest:
    """A file write. ``default`` asks a person about it; with ``raw`` it is
    decided by the engine, without one by the coarse kind-only fallback."""
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
        session_id="mode-chat",
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


@dataclass
class _Driver:
    session: ChatSession
    factory: FakeAdapterFactory
    prompts: dict[str, asyncio.Future[PermissionOptionId]]
    prompted: list[str]
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

    async def parked(self, request_id: str) -> asyncio.Future[PermissionOptionId]:
        """The prompt a person is shown for ``request_id``, once it is up."""
        deadline = asyncio.get_running_loop().time() + SETTLE_BOUND
        while asyncio.get_running_loop().time() < deadline:
            pending = self.prompts.get(request_id)
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

    async def settle_loop(self) -> None:
        """Run every callback already scheduled, so a reply that is still absent
        afterwards is absent because nothing decided the ask."""
        for _ in range(20):
            await asyncio.sleep(0)

    async def resolved(self, request_id: str, option_id: str) -> PermissionResolved:
        """Let the harness echo its reply, and return the event every subscriber
        (the transcript, a cloud mirror) actually received."""
        await self.adapter.feed(
            PermissionResolved(
                event_id=f"done-{request_id}",
                time=_T,
                session_id="mode-chat",
                request_id=request_id,
                option_id=option_id,  # type: ignore[arg-type]
            )
        )
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

    async def _open(*, mode: PermissionMode = "default", name: str = "work") -> _Driver:
        workspace = tmp_path / name
        workspace.mkdir(exist_ok=True)
        prompts: dict[str, asyncio.Future[PermissionOptionId]] = {}
        prompted: list[str] = []

        async def _resolver(request: PermissionRequest) -> PermissionOptionId:
            prompted.append(request.request_id)
            fut: asyncio.Future[PermissionOptionId] = asyncio.get_running_loop().create_future()
            prompts[request.request_id] = fut
            return await fut

        factory = FakeAdapterFactory(FakeAdapter, available=True)
        runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
        runtimes.append(runtime)
        session = await runtime.open_chat(
            create=True,
            harness_type="agent",
            permission_broker=PermissionBroker(_resolver, default_timeout_seconds=None),
        )
        session.set_permission_mode(mode)
        driver = _Driver(session=session, factory=factory, prompts=prompts, prompted=prompted)
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


async def _pending_under_default(
    open_session: Callable[..., Awaitable[_Driver]], ask: PermissionRequest
) -> tuple[_Driver, asyncio.Future[PermissionOptionId]]:
    driver = await open_session(mode="default")
    await driver.adapter.feed(ask)
    prompt = await driver.parked(ask.request_id)
    await driver.settle_loop()
    assert driver.reply_for(ask.request_id) is None, "default mode puts a write to a person"
    return driver, prompt


# --------------------------------------------------------------------------- #
# the new stance answers it on its own
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["notes.md", None], ids=["engine", "coarse"])
async def test_switching_to_a_mode_that_allows_it_resolves_the_ask_as_allowed(
    open_session: Callable[..., Awaitable[_Driver]], raw: str | None
) -> None:
    driver, prompt = await _pending_under_default(open_session, _write_ask(raw=raw))

    driver.session.set_permission_mode("bypass")

    assert await driver.reply("req-1") == ("allow_once", None)
    assert prompt.cancelled(), "the prompt nobody needs to answer any more is taken down"
    event = await driver.resolved("req-1", "allow_once")
    assert event.decided_by == "policy", "no person chose this; the stance did"
    [record] = driver.session.decision_sink.read()
    assert (record.decision, record.mode) == ("allow", "bypass")
    assert record.decided_by != "human"
    assert any("default" in r and "bypass" in r for r in record.reasons), record.reasons


@pytest.mark.parametrize("raw", ["notes.md", None], ids=["engine", "coarse"])
@pytest.mark.parametrize("stricter", ["read_only", "plan"])
async def test_switching_to_a_mode_that_refuses_it_resolves_the_ask_as_rejected(
    open_session: Callable[..., Awaitable[_Driver]], raw: str | None, stricter: PermissionMode
) -> None:
    driver, prompt = await _pending_under_default(open_session, _write_ask(raw=raw))

    driver.session.set_permission_mode(stricter)

    option, reason = await driver.reply("req-1")
    assert option == "reject_once"
    assert reason, "a stance's refusal tells the model why, so the turn goes on"
    assert prompt.cancelled()
    assert driver.adapter.cancel_count == 0, "a policy refusal does not end the turn"
    event = await driver.resolved("req-1", "reject_once")
    assert event.decided_by == "policy"
    [record] = driver.session.decision_sink.read()
    assert (record.decision, record.mode) == ("reject", stricter)
    assert any("default" in r for r in record.reasons), record.reasons


# --------------------------------------------------------------------------- #
# the new stance still asks: the same prompt stays up
# --------------------------------------------------------------------------- #


async def test_switching_to_a_mode_that_still_asks_leaves_the_same_prompt_pending(
    open_session: Callable[..., Awaitable[_Driver]],
) -> None:
    # Auto mode with no safety judge wired puts the write middle to a person,
    # exactly like default.
    driver, prompt = await _pending_under_default(open_session, _write_ask())

    driver.session.set_permission_mode("auto")
    await driver.settle_loop()

    assert driver.reply_for("req-1") is None, "nothing answered it"
    assert not prompt.done(), "the prompt a person is looking at stays up"
    assert driver.prompted == ["req-1"], "and no second prompt is raised for it"
    assert driver.session.decision_sink.read() == []

    prompt.set_result("allow_once")
    assert await driver.reply("req-1") == ("allow_once", None)
    event = await driver.resolved("req-1", "allow_once")
    assert event.decided_by == "user", "the person who answered it is who decided"
    [record] = driver.session.decision_sink.read()
    assert (record.decided_by, record.mode) == ("human", "auto")


async def test_setting_the_mode_already_in_force_changes_nothing(
    open_session: Callable[..., Awaitable[_Driver]],
) -> None:
    driver, prompt = await _pending_under_default(open_session, _write_ask())
    driver.session.set_permission_mode("default")
    await driver.settle_loop()
    assert not prompt.done()
    assert driver.reply_for("req-1") is None
    prompt.set_result("reject_once")
    assert (await driver.reply("req-1"))[0] == "reject_once"
    [record] = driver.session.decision_sink.read()
    assert not any("mode changed" in r for r in record.reasons)


# --------------------------------------------------------------------------- #
# the same ladder as an ask that arrives under the new stance
# --------------------------------------------------------------------------- #


async def _fresh_outcome(
    open_session: Callable[..., Awaitable[_Driver]], mode: PermissionMode, name: str
) -> str:
    """What the same ask gets when it ARRIVES under ``mode``: the reply's option,
    or ``pending`` when a person is asked."""
    driver = await open_session(mode=mode, name=name)
    await driver.adapter.feed(_write_ask())
    deadline = asyncio.get_running_loop().time() + SETTLE_BOUND
    while asyncio.get_running_loop().time() < deadline:
        if "req-1" in driver.prompts:
            return "pending"
        found = driver.reply_for("req-1")
        if found is not None:
            return found[0]
        await asyncio.sleep(0.01)
    raise AssertionError("the ask was neither answered nor put to a person")


@pytest.mark.parametrize("target", ALL_MODES)
async def test_a_reconsidered_ask_gets_what_it_would_get_arriving_under_the_new_mode(
    open_session: Callable[..., Awaitable[_Driver]], target: PermissionMode
) -> None:
    expected = await _fresh_outcome(open_session, target, name=f"fresh-{target}")

    driver, prompt = await _pending_under_default(open_session, _write_ask())
    driver.session.set_permission_mode(target)
    # The switch returns at once; the ask is decided again on the session's own
    # permission loop, so what it decided is read off the reply it sends.
    await driver.settle_loop()

    if expected == "pending":
        # Still the person's: the same prompt is up, and the answer they give
        # is taken by a decision made under the NEW mode.
        assert driver.reply_for("req-1") is None
        assert not prompt.done()
        prompt.set_result("reject_once")
        assert (await driver.reply("req-1"))[0] == "reject_once"
        [record] = driver.session.decision_sink.read()
        assert (record.decided_by, record.mode) == ("human", target)
        return

    got, _ = await driver.reply("req-1")
    assert got == expected
    assert prompt.cancelled()
    if target != "bypass" and target != "auto":
        # Never widened: a stance no laxer than the one it was asked under
        # never turns the ask into an allow.
        assert not got.startswith("allow")


# --------------------------------------------------------------------------- #
# several asks: each gets its own outcome
# --------------------------------------------------------------------------- #


async def test_each_ask_gets_its_own_outcome_and_only_the_reconsidered_one_names_the_change(
    open_session: Callable[..., Awaitable[_Driver]],
) -> None:
    driver = await open_session(mode="default")
    await driver.adapter.feed(_write_ask("req-1"))
    first = await driver.parked("req-1")
    # Raised while the first is being asked; it waits its turn.
    await driver.adapter.feed(_write_ask("req-2", raw="draft.md"))
    await driver.settle_loop()
    assert "req-2" not in driver.prompts

    driver.session.set_permission_mode("read_only")

    assert (await driver.reply("req-1"))[0] == "reject_once"
    assert (await driver.reply("req-2"))[0] == "reject_once"
    assert first.cancelled()
    assert "req-2" not in driver.prompts, "the second ask arrived under read-only: nobody is asked"
    records = {r.targets[0]: r for r in driver.session.decision_sink.read()}
    assert records["notes.md"].mode == records["draft.md"].mode == "read_only"
    assert any("mode changed" in r for r in records["notes.md"].reasons)
    assert not any("mode changed" in r for r in records["draft.md"].reasons)
