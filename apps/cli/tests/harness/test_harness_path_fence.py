"""The workspace fence, where a session actually decides: the runtime's own loop.

A session on a box the operator owns may read the customer's project and nothing
else — not the box's ``ALKERA_HOME`` (the operator's gateway token lives there),
not ``/etc``, not what a ``..`` or a symlink reaches. That bound cannot be an
answer to a permission prompt, because for a READ there is no prompt: the policy's
read fast path allows a read in EVERY mode, ``read_only`` included, so by the time
a broker could be asked the file would already be open. So
``ChatSession._decide_permission`` consults the session's ``PathFence`` ahead of
every allow it can reach.

The refusal is deliberately reason-BEARING: opencode turns a reject that carries a
reason into a recoverable tool error (a ``CorrectedError``) and keeps the turn,
where a reason-less one ends it (a ``RejectedError``). A file the model may not
read should cost it a sentence, not the conversation.

A session with NO fence — every local CLI and editor session — must be decided
exactly as it is today, so every case here runs both ways.
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
from alkera_cli.cloud.refusal import REFUSAL_COPY
from alkera_cli.harness import ChatSession, HarnessRuntime, PathFence, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapter import PromptInput
from alkera_cli.host import paths
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    Event,
    PartCreated,
    PermissionOption,
    PermissionOptionId,
    PermissionRequest,
    SessionStatusChanged,
    TextPart,
)

_T = datetime(2026, 9, 6, tzinfo=UTC)
SECRET = "eyJ-operator-jwt-DO-NOT-LEAK-7f3a9c"
OUTSIDE = REFUSAL_COPY["outside_workspace"]

#: Locations that leave the workspace, by the way they leave it. Resolved against
#: the fixture's roots (some are absolute, some relative to the project).
ESCAPES: dict[str, Callable[[dict[str, Path]], str]] = {
    "the-operators-token": lambda r: str(r["home"] / "auth.yml"),
    "an-absolute-system-file": lambda _r: "/etc/passwd",
    "a-dotdot-climb": lambda _r: "../outside/leak.txt",
    "a-dotdot-climb-from-a-subdir": lambda _r: "src/../../outside/leak.txt",
    "a-symlinked-directory": lambda _r: "escape/leak.txt",
    "a-symlinked-file": lambda _r: "cross",
    "a-tilde-expansion": lambda _r: "~/.ssh/id_ed25519",
    # opencode spells a location relative to ITS worktree, and for a project that
    # is not a repository that worktree is "/" — so the token file arrives with
    # its leading slash gone. It is still the token file.
    "the-operators-token-as-a-harness-spells-it": lambda r: str(r["home"] / "auth.yml").lstrip("/"),
}


# --------------------------------------------------------------------------- #
# the box: a workspace, the operator's home beside it, and ways out of both
# --------------------------------------------------------------------------- #


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    workspace = tmp_path / "work"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.py").write_text("print('hi')\n")
    (workspace / "README.md").write_text("hello\n")
    home = tmp_path / "alkera-home"
    home.mkdir()
    (home / "auth.yml").write_text(f"token: {SECRET}\n")
    user_home = tmp_path / "user-home"
    (user_home / ".ssh").mkdir(parents=True)
    (user_home / ".ssh" / "id_ed25519").write_text("private\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("x\n")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    (workspace / "cross").symlink_to(outside / "leak.txt")
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    return {"workspace": workspace, "home": home, "user_home": user_home, "outside": outside}


def _read_ask(target: str, *, request_id: str = "req-1") -> PermissionRequest:
    """The ask a harness raises for a file READ — the shape whose policy answer is
    the read fast path (allow, in every mode)."""
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_T,
        session_id="fence-chat",
        request_id=request_id,
        tool_call_id=f"call-{request_id}",
        permission_kind="read",
        canonical_kind="other",
        patterns=[],
        subject={
            "capability": "fs",
            "effect": "read",
            "operation": "read",
            "raw": target,
            "targets": [{"kind": "file", "name": target}],
            "classifier": "opencode-tool",
        },
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="reject_once", name="Reject"),
        ],
    )


def _workspace_fence(root: Path) -> PathFence:
    """The fence a cloud box installs: the project root, the box's ALKERA_HOME
    denied inside it, refused in the reader-facing words."""
    return PathFence(
        escape=lambda request: fence.ask_escape(request, root=root),
        reason=OUTSIDE,
    )


# --------------------------------------------------------------------------- #
# a real runtime + FakeAdapter, opened with or without a fence
# --------------------------------------------------------------------------- #


@dataclass
class _Driver:
    """One live read-only session and the harness it answers."""

    session: ChatSession
    factory: FakeAdapterFactory
    prompted: list[str]
    events: list[Event] = field(default_factory=list)

    @property
    def adapter(self) -> FakeAdapter:
        """The root session's harness (a spawned child's is the next one built)."""
        return self.factory.adapters[0]

    async def ask(self, request: PermissionRequest) -> tuple[str, str | None]:
        """Feed one permission ask; return the ``(option, reason)`` the harness was
        answered with — the reason being what the model gets to read."""
        await self.adapter.feed(request)
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            for (replied, option), (_, reason) in zip(
                self.adapter.permission_replies,
                self.adapter.permission_reply_reasons,
                strict=True,
            ):
                if replied == request.request_id:
                    return str(option), reason
            await asyncio.sleep(0.01)
        raise AssertionError(f"no answer for {request.request_id}")

    async def feed(self, event: Event) -> None:
        await self.adapter.feed(event)
        await asyncio.sleep(0.05)

    @property
    def errored(self) -> bool:
        return any(
            isinstance(event, SessionStatusChanged) and event.status == "error"
            for event in self.events
        )


async def _never_prompt(request: PermissionRequest) -> PermissionOptionId:
    raise AssertionError("a bounded read must never be parked in front of a human")


@pytest.fixture
async def open_session(
    roots: dict[str, Path],
) -> AsyncIterator[Callable[..., Awaitable[_Driver]]]:
    """Opens a read-only session over a ``FakeAdapter`` — fenced to the workspace
    or not — with a broker that fails the test if anything reaches it."""
    runtimes: list[HarnessRuntime] = []
    pumps: list[asyncio.Task[None]] = []
    prompted: list[str] = []

    async def _resolver(request: PermissionRequest) -> PermissionOptionId:
        prompted.append(request.request_id)
        return await _never_prompt(request)

    async def _open(*, fenced: bool, make: Callable[[], FakeAdapter] = FakeAdapter) -> _Driver:
        workspace = roots["workspace"]
        factory = FakeAdapterFactory(make, available=True)
        runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
        runtimes.append(runtime)
        driver = _Driver(
            session=await runtime.open_chat(
                create=True,
                harness_type="agent",
                permission_broker=PermissionBroker(_resolver, default_timeout_seconds=None),
                path_fence=_workspace_fence(workspace) if fenced else None,
            ),
            factory=factory,
            prompted=prompted,
        )
        driver.session.set_permission_mode("read_only")
        sub = driver.session.subscribe()

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
# (a) + (c) a read inside the workspace is decided exactly as it always was
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fenced", [True, False], ids=["fenced", "unfenced"])
@pytest.mark.parametrize(
    "target",
    [
        pytest.param("src/app.py", id="relative"),
        pytest.param("README.md", id="at-the-root"),
        pytest.param("src/../README.md", id="dotdot-that-stays-inside"),
        pytest.param("does/not/exist.txt", id="missing-but-inside"),
    ],
)
async def test_a_read_inside_the_workspace_is_auto_allowed(
    open_session: Callable[..., Awaitable[_Driver]], target: str, fenced: bool
) -> None:
    """The read fast path, untouched: allowed at once, with no reason and no human
    — whether or not the session is fenced."""
    driver = await open_session(fenced=fenced)
    assert await driver.ask(_read_ask(target)) == ("allow_once", None)
    assert driver.prompted == []
    assert not driver.errored


# --------------------------------------------------------------------------- #
# (b) a read that leaves the workspace is refused, WITH the reason
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", sorted(ESCAPES))
async def test_a_read_outside_the_workspace_is_refused_with_the_reason(
    open_session: Callable[..., Awaitable[_Driver]], roots: dict[str, Path], case: str
) -> None:
    driver = await open_session(fenced=True)
    target = ESCAPES[case](roots)
    assert await driver.ask(_read_ask(target, request_id="req-out")) == ("reject_once", OUTSIDE)
    assert driver.prompted == []


@pytest.mark.parametrize("case", sorted(ESCAPES))
async def test_a_read_outside_the_workspace_is_allowed_when_nothing_fences_it(
    open_session: Callable[..., Awaitable[_Driver]], roots: dict[str, Path], case: str
) -> None:
    """The control, and the reason the fence has to be injected: WITHOUT one the
    very same ask takes the read fast path and is allowed — which is correct for a
    developer's own machine and is what a local session must keep doing."""
    driver = await open_session(fenced=False)
    target = ESCAPES[case](roots)
    assert await driver.ask(_read_ask(target, request_id="req-out")) == ("allow_once", None)


async def test_the_turn_continues_after_a_refused_read(
    open_session: Callable[..., Awaitable[_Driver]], roots: dict[str, Path]
) -> None:
    """The refusal answers ONE ask; it does not end the session. The reject carries
    a reason (opencode's recoverable ``CorrectedError``), the harness is never
    cancelled, the next read is decided on its own, and the harness's next
    assistant event still reaches the session's consumers."""
    driver = await open_session(fenced=True)
    token = str(roots["home"] / "auth.yml")
    assert await driver.ask(_read_ask(token, request_id="req-1")) == ("reject_once", OUTSIDE)
    assert driver.adapter.cancel_count == 0, "a fenced read must not stop the turn"
    assert await driver.ask(_read_ask("src/app.py", request_id="req-2")) == ("allow_once", None)
    await driver.feed(
        PartCreated(
            event_id="ev-after",
            time=_T,
            session_id=driver.session.session_id,
            part=TextPart(part_id="p1", message_id="m1", text="I'll stay in the workspace."),
        )
    )
    assert [
        event.part.text  # type: ignore[union-attr]
        for event in driver.events
        if isinstance(event, PartCreated)
    ] == ["I'll stay in the workspace."]
    assert not driver.errored
    assert driver.prompted == []


async def test_a_refused_read_is_recorded_as_a_decision(
    open_session: Callable[..., Awaitable[_Driver]], roots: dict[str, Path]
) -> None:
    """Every decision the session makes is on the record — a refusal the model was
    given a reason for is one, so an operator can see what the box would not read."""
    driver = await open_session(fenced=True)
    token = str(roots["home"] / "auth.yml")
    assert await driver.ask(_read_ask(token, request_id="req-1")) == ("reject_once", OUTSIDE)
    records = driver.session.decision_sink.read()
    assert [(r.decision, r.decided_by) for r in records] == [("reject", "fence")]


async def test_the_secret_is_never_in_what_the_harness_is_told(
    open_session: Callable[..., Awaitable[_Driver]], roots: dict[str, Path]
) -> None:
    """The refusal names no content: the file is never opened, so the token cannot
    ride back to the model in the reason."""
    driver = await open_session(fenced=True)
    token_file = roots["home"] / "auth.yml"
    assert await driver.ask(_read_ask(str(token_file), request_id="req-1")) == (
        "reject_once",
        OUTSIDE,
    )
    told = repr(driver.adapter.permission_reply_reasons) + repr(driver.events)
    assert SECRET not in told
    assert token_file.read_text(encoding="utf-8").count(SECRET) == 1, "the file itself is untouched"


# --------------------------------------------------------------------------- #
# a subagent of a bounded session is bounded the same way
# --------------------------------------------------------------------------- #


class _AskingAdapter(FakeAdapter):
    """A harness that asks to read the operator's token the moment it is prompted,
    and finishes its turn once the runtime has answered."""

    target: str = "/etc/passwd"

    async def send_prompt(self, prompt: PromptInput) -> None:
        self._permission_reply_event.clear()
        await self.feed(_read_ask(type(self).target, request_id="req-child"))
        await asyncio.wait_for(self._permission_reply_event.wait(), 5.0)
        await super().send_prompt(prompt)


@pytest.mark.parametrize(
    ("fenced", "expected"),
    [
        pytest.param(True, ("reject_once", OUTSIDE), id="fenced-parent"),
        pytest.param(False, ("allow_once", None), id="unfenced-parent"),
    ],
)
async def test_a_subagent_inherits_the_parents_fence(
    open_session: Callable[..., Awaitable[_Driver]],
    roots: dict[str, Path],
    fenced: bool,
    expected: tuple[str, str | None],
) -> None:
    """A fence a subagent can walk around is not a fence: the child session opened
    for a spawn carries the parent's bound, and refuses the same read."""
    _AskingAdapter.target = str(roots["home"] / "auth.yml")
    driver = await open_session(
        fenced=fenced, make=lambda: _AskingAdapter(reply_text="looked around")
    )
    result = await driver.session.spawn_subagent("look around", agent="explore")
    assert result.error is None, result.error
    child = driver.factory.adapters[1]
    assert child.permission_replies == [("req-child", expected[0])]
    assert child.permission_reply_reasons == [("req-child", expected[1])]


# --------------------------------------------------------------------------- #
# (d) the words are the product's words, spelled in one module
# --------------------------------------------------------------------------- #


def test_the_refusal_the_model_is_given_is_the_products_own_sentence() -> None:
    """The reason is data the session is handed, not a sentence the harness knows:
    nothing under ``alkera_cli/harness/`` may spell it, or the product would refuse
    the same thing in two different voices."""
    harness = Path(__file__).resolve().parents[3] / "apps/cli/alkera_cli/harness"
    hits = [
        str(path)
        for path in harness.rglob("*.py")
        if OUTSIDE in path.read_text("utf-8", errors="ignore")
    ]
    assert hits == [], hits
    assert OUTSIDE == REFUSAL_COPY["outside_workspace"]


# --------------------------------------------------------------------------- #
# a fenced session is a shared box: a member's recorded exec grant does not bind
# --------------------------------------------------------------------------- #


def _exec_ask(request_id: str) -> PermissionRequest:
    """An ask whose subject is a server-side program (the EXEC tier)."""
    return _read_ask("COPY t FROM PROGRAM 'id'", request_id=request_id).model_copy(
        update={
            "permission_kind": "alkera_sql",
            "subject": {
                "capability": "sql",
                "effect": "exec",
                "operation": "copy_program",
                "raw": "COPY t FROM PROGRAM 'id'",
                "targets": [],
                "classifier": "sqlglot",
            },
        }
    )


@pytest.mark.parametrize(
    ("fenced", "option"),
    [
        pytest.param(False, "allow_once", id="local-session-overlay-grants"),
        pytest.param(True, "reject_once", id="fenced-session-overlay-does-not"),
    ],
)
async def test_a_fenced_session_reads_an_overlay_exec_grant_as_one_members(
    open_session: Callable[..., Awaitable[_Driver]],
    roots: dict[str, Path],
    fenced: bool,
    option: str,
) -> None:
    """The runtime's own decision: in bypass, an exec rule in the gitignored overlay
    grants on a local machine and does not on a fenced (cloud, shared) session."""
    alkera = roots["workspace"] / ".alkera"
    alkera.mkdir(exist_ok=True)
    (alkera / "permissions.local.yml").write_text(
        "rules:\n  - {capability: sql, effect: exec, decision: allow}\n"
    )
    driver = await open_session(fenced=fenced)
    driver.session.set_permission_mode("bypass")
    answered, _reason = await driver.ask(_exec_ask(f"exec-{fenced}"))
    assert answered == option
    assert driver.prompted == []
