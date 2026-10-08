"""A stance says when the reader will be asked — this holds it to that.

A chat in Default was asked to create a file; the file was written and no
permission card appeared, while the picker's own line read "asks before every
edit or command". Both halves were defensible on their own: the write landed in
the chat's own working folder, which every mode admits by design (it is where
plan.md is drafted and where a cloud agent's whole working tree lives), and the
sentence was written before that carve-out existed. Together they cost the
reader the one thing a stance is for.

So the sentence and the outcomes live in one row (``MODE_RULES``), and every
case here drives ``ChatSession._decide_permission`` — the real chokepoint, on a
real session, through the harness's own permission loop — rather than replaying
the order it runs its layers in. That order is part of what is promised: the
session's path fence, then the chat-folder carve-out, then the mode and effect
policy, then the auto-mode judge. A test that re-derived it would stay green
through a reorder that broke the promise.

What a stance decides is read off the adapter's reply and whether a person was
reached: allowed without a prompt is ``allow``, allowed or refused after the
broker handed it to someone is ``prompt``, refused without one is ``reject``.
The browser picker's copy is pinned to the same rows, because a promise nobody
reads in the code is one the reader reads in the UI.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud.mirror import NO_WRITE_MODES
from alkera_cli.contracts.tool_types import ActionDescriptor, Effect, ResourceRef
from alkera_cli.harness import ChatSession, HarnessRuntime, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.permission_mode import (
    ALL_MODES,
    MODE_RULES,
    MODES_THAT_DISCARD_AN_APPROVAL,
    MODES_THAT_HONOUR_AN_APPROVAL,
    PermissionMode,
    approval_reaches_the_agent,
)
from alkera_cli.harness.safety_judge import JudgeVerdict
from alkera_core.permission_presentation import (
    APPROVAL_REFUSALS,
    PERMISSION_MODES,
    approval_refusal,
)
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionOption, PermissionRequest

_T = datetime(2026, 9, 20, tzinfo=UTC)

#: How long a reply may take before the case is a failure rather than a wait.
#: The work is in-process; the budget is for a loaded machine, not for the path.
_REPLY_DEADLINE_SECONDS = 5.0


class _AllowingJudge:
    """The auto-mode grounded judge, which is a metered gateway call and the one
    thing here worth standing in for. It allows, because what auto PROMISES is
    that the recoverable write middle runs — a judge that blocked would pin the
    judge's opinion instead of the stance's contract. That it is consulted at
    all is asserted separately, as is the fail-closed path when none is wired."""

    def __init__(self) -> None:
        self.calls: list[ActionDescriptor] = []

    async def judge(
        self, descriptor: ActionDescriptor, task_goal: str, *, workspace_root: str | None = None
    ) -> JudgeVerdict:
        self.calls.append(descriptor)
        return JudgeVerdict("allow", reason="within the task")


@dataclass
class _Stance:
    """One opened chat, and what its chokepoint did with the last ask."""

    session: ChatSession
    factory: FakeAdapterFactory
    judge: _AllowingJudge
    prompted: set[str] = field(default_factory=set)

    @property
    def chat_folder(self) -> Path:
        """The chat's own working folder — the carve-out's subject, taken from
        the binding the session actually made rather than rebuilt from a guess."""
        return Path(self.session._tool_binding.sandbox_dir)

    @property
    def workspace(self) -> Path:
        return self.session._runtime.project.path.parent

    async def decide(self, descriptor: ActionDescriptor, canonical_kind: str) -> str:
        """Feed one ask and read back what the chokepoint decided."""
        adapter = self.factory.adapters[0]
        request_id = f"req-{len(adapter.permission_replies)}-{canonical_kind}"
        await adapter.feed(_ask(request_id, descriptor, canonical_kind))
        deadline = asyncio.get_running_loop().time() + _REPLY_DEADLINE_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            for replied, option in adapter.permission_replies:
                if replied != request_id:
                    continue
                if request_id in self.prompted:
                    return "prompt"
                return "allow" if str(option).startswith("allow") else "reject"
            await asyncio.sleep(0.01)
        raise AssertionError(f"{request_id} was never answered")


def _ask(request_id: str, descriptor: ActionDescriptor, canonical_kind: str) -> PermissionRequest:
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_T,
        session_id="stance-chat",
        request_id=request_id,
        permission_kind=canonical_kind,
        canonical_kind=canonical_kind,  # type: ignore[arg-type]
        patterns=[descriptor.raw or ""],
        subject=descriptor.model_dump(mode="json"),
        options=[
            PermissionOption(option_id="allow_once", name="Allow once"),
            PermissionOption(option_id="reject_once", name="Reject once"),
        ],
    )


@pytest.fixture
async def open_stance(tmp_path: Path) -> AsyncIterator[Callable[..., object]]:
    runtimes: list[HarnessRuntime] = []

    async def _open(
        mode: PermissionMode, *, judge: _AllowingJudge | None = None, shared: bool = False
    ) -> _Stance:
        workspace = tmp_path / f"{mode}-shared" if shared else tmp_path / mode
        workspace.mkdir(parents=True, exist_ok=True)
        # A workspace member runs in the workspace's shared folder, outside its
        # own chat folder; the box hands that folder in as the sandbox.
        shared_tree = workspace / "workspace" / "files" if shared else None
        if shared_tree is not None:
            shared_tree.mkdir(parents=True)
        judge = _AllowingJudge() if judge is None else judge
        prompted: set[str] = set()

        async def _resolver(request: PermissionRequest) -> str:
            # The person allows. A stance that reaches them at all is a prompt
            # whatever they answer, so the answer only has to be unambiguous.
            prompted.add(request.request_id)
            return "allow_once"

        factory = FakeAdapterFactory(FakeAdapter, available=True)
        runtime = HarnessRuntime(
            ProjectDirectory(workspace / ".alkera"),
            adapter_factory=factory,
            safety_judge=judge,  # type: ignore[arg-type]
        )
        runtimes.append(runtime)
        session = await runtime.open_chat(
            create=True,
            harness_type="agent",
            permission_broker=PermissionBroker(_resolver, default_timeout_seconds=None),
            working_dir=shared_tree,
            sandbox_dir=shared_tree,
        )
        session.set_permission_mode(mode)
        session._last_user_text = "do the task"
        return _Stance(session=session, factory=factory, judge=judge, prompted=prompted)

    try:
        yield _open
    finally:
        for runtime in runtimes:
            for session_id in list(runtime.open_session_ids):
                await runtime.close_chat(session_id)


def _fs(path: Path, effect: Effect = Effect.WRITE) -> ActionDescriptor:
    return ActionDescriptor(
        capability="fs",
        effect=effect,
        operation="edit",
        targets=[ResourceRef(kind="file", name=str(path))],
        raw=str(path),
        classifier="test",
        confidence="exact",
    )


def _shell(command: str, effect: Effect) -> ActionDescriptor:
    return ActionDescriptor(
        capability="shell",
        effect=effect,
        operation="run",
        targets=[],
        raw=command,
        classifier="test",
        confidence="exact",
    )


#: Each shape is named by the ``ModeRule`` field it is supposed to land on, so a
#: row that drifts names the promise it broke. Built per session: the chat
#: folder is the one the session bound, not a path this file invented.
_SHAPES: dict[str, Callable[[_Stance], tuple[ActionDescriptor, str]]] = {
    "read/a-file-read": lambda s: (_fs(s.workspace / "src/app.py", Effect.READ), "edit"),
    "read/a-listing": lambda s: (_shell("ls -la src", Effect.READ), "shell"),
    "chat_folder_change/a-write-into-it": lambda s: (_fs(s.chat_folder / "plan.md"), "edit"),
    "chat_folder_change/a-redirect-into-it": lambda s: (
        _shell(f"echo plan >> {s.chat_folder}/plan.md", Effect.WRITE),
        "shell",
    ),
    "workspace_change/an-edit-to-a-project-file": lambda s: (
        _fs(s.workspace / "src/app.py"),
        "edit",
    ),
    "workspace_change/a-command-that-writes": lambda s: (
        _shell(f"echo hi > {s.workspace}/out.txt", Effect.WRITE),
        "shell",
    ),
    "destroy/removing-a-project-tree": lambda s: (
        _shell(f"rm -rf {s.workspace}/build", Effect.DESTROY),
        "shell",
    ),
}


@pytest.mark.parametrize("mode", ALL_MODES)
@pytest.mark.parametrize("shape", list(_SHAPES), ids=lambda name: name.split("/", 1)[1])
async def test_the_chokepoint_decides_what_the_stance_table_promises(
    open_stance: Callable[..., object], mode: PermissionMode, shape: str
) -> None:
    stance: _Stance = await open_stance(mode)  # type: ignore[misc]
    descriptor, canonical_kind = _SHAPES[shape](stance)
    field_name = shape.split("/", 1)[0]
    expected = getattr(MODE_RULES[mode], field_name)
    assert await stance.decide(descriptor, canonical_kind) == expected, (
        f"{mode} decides {shape.split('/', 1)[1]} differently from its own row's {field_name}"
    )


#: The same writes, made by a workspace member into the shared folder its
#: sandbox is. Read-only once wrote a sibling's ``notes.md`` this way, though its
#: sentence says it writes only in this chat.
_SHARED_SHAPES: dict[str, Callable[[_Stance], tuple[ActionDescriptor, str]]] = {
    "shared_folder_change/a-write-into-it": lambda s: (_fs(s.chat_folder / "notes.md"), "edit"),
    "shared_folder_change/a-redirect-into-it": lambda s: (
        _shell(f"echo line >> {s.chat_folder}/notes.md", Effect.WRITE),
        "shell",
    ),
}


@pytest.mark.parametrize("mode", ALL_MODES)
@pytest.mark.parametrize("shape", list(_SHARED_SHAPES), ids=lambda name: name.split("/", 1)[1])
async def test_a_member_writes_the_shared_folder_as_its_row_promises(
    open_stance: Callable[..., object], mode: PermissionMode, shape: str
) -> None:
    stance: _Stance = await open_stance(mode, shared=True)  # type: ignore[misc]
    descriptor, canonical_kind = _SHARED_SHAPES[shape](stance)
    expected = MODE_RULES[mode].shared_folder_change
    assert await stance.decide(descriptor, canonical_kind) == expected, (
        f"{mode} decides a member's {shape.split('/', 1)[1]} differently from its row"
    )


async def test_read_only_refuses_the_shared_folder_but_not_a_chat_on_its_own(
    open_stance: Callable[..., object],
) -> None:
    """The asymmetry, as one case: the same write into the session's sandbox is
    allowed when the sandbox is the chat's own folder and refused when it is a
    workspace's shared folder. A fix that refused every sandbox write in
    read-only, or none, fails one half."""
    alone: _Stance = await open_stance("read_only")  # type: ignore[misc]
    member: _Stance = await open_stance("read_only", shared=True)  # type: ignore[misc]
    assert await alone.decide(_fs(alone.chat_folder / "notes.md"), "edit") == "allow"
    assert await member.decide(_fs(member.chat_folder / "notes.md"), "edit") == "reject"


def test_read_only_tells_the_agent_it_does_not_write_a_shared_folder() -> None:
    """The agent is told what the gate does, so it does not try a write it will
    be refused in a workspace."""
    from alkera_cli.harness.permission_mode import mode_system_prompt

    steering = mode_system_prompt("read_only") or ""
    assert "shared folder" in steering and "is not this chat's own" in steering


async def test_the_chat_folder_carve_out_is_taken_before_the_mode_refuses(
    open_stance: Callable[..., object],
) -> None:
    """The ORDER, not just the outcome: read-only refuses every mutation, and the
    only reason a plan file can be written in it is that the carve-out is
    consulted first. A reorder that let the mode answer first would leave the
    outcome table above green for four stances and break plan mode."""
    stance: _Stance = await open_stance("read_only")  # type: ignore[misc]
    assert await stance.decide(_fs(stance.chat_folder / "plan.md"), "edit") == "allow"
    assert await stance.decide(_fs(stance.workspace / "src/app.py"), "edit") == "reject"


async def test_auto_runs_the_write_middle_only_because_the_judge_cleared_it(
    open_stance: Callable[..., object],
) -> None:
    """Auto's row says a project write runs. It runs because the grounded judge
    said so — the deterministic policy hands it over rather than allowing it — so
    the row is a promise about the judge being there, not about the write being
    safe on its face."""
    stance: _Stance = await open_stance("auto")  # type: ignore[misc]
    assert await stance.decide(_fs(stance.workspace / "src/app.py"), "edit") == "allow"
    assert [d.raw for d in stance.judge.calls] == [str(stance.workspace / "src/app.py")]


async def test_auto_with_no_judge_asks_rather_than_running_the_write_middle(
    open_stance: Callable[..., object],
) -> None:
    """And where there is no judge, auto does not fall back to its row: an
    ungrounded write middle prompts like Default. Pinned here so the row above is
    read as "with the judge wired", which is every real session."""
    stance: _Stance = await open_stance("auto", judge=None)  # type: ignore[misc]
    stance.session._runtime._safety_judge = None
    assert await stance.decide(_fs(stance.workspace / "src/app.py"), "edit") == "prompt"


def test_every_mode_has_a_row() -> None:
    """A mode with no row is a stance with no stated contract — and the copy
    pin below would silently skip it."""
    assert set(MODE_RULES) == set(ALL_MODES)


def test_default_admits_a_chat_folder_write_and_says_so() -> None:
    """The finding, as one case: Default writes inside the chat's own folder
    without asking, and its sentence carries the exception that makes that
    true. A sentence claiming every edit is asked fails here."""
    rule = MODE_RULES["default"]
    assert rule.chat_folder_change == "allow"
    assert rule.workspace_change == "prompt"
    assert "outside the chat's files" in rule.description


@pytest.mark.parametrize(
    ("mode", "writes_shared"),
    [
        pytest.param("default", True, id="default"),
        pytest.param("plan", True, id="plan"),
        pytest.param("read_only", False, id="read_only"),
    ],
)
def test_a_line_names_the_chats_files_exactly_where_a_shared_folder_is_written(
    mode: PermissionMode, writes_shared: bool
) -> None:
    """In a workspace the chat's files are the workspace's shared folder. A mode
    that writes it unasked says "the chat's files", which is true of a plain
    chat's folder too; one that refuses it must not, since "this chat" is then
    the only true claim."""
    rule = MODE_RULES[mode]
    assert (rule.shared_folder_change == "allow") is writes_shared
    assert ("the chat's files" in rule.description) is writes_shared


async def test_read_only_says_it_writes_in_this_chat_and_runs_no_shell() -> None:
    """Read-only admits a write in the chat's own folder and refuses every shell
    command, read-classified ones included. A sentence that promised only "no
    change outside this chat" read as leaving the shell open, and one that
    promised no writes at all turned the agent away from its own scratch."""
    from alkera_cli.plugins.plugin_base.permissions.gate import (
        READ_ONLY_SHELL_REASON,
        GateBinding,
        gate_shell_action,
    )

    rule = MODE_RULES["read_only"]
    assert rule.chat_folder_change == "allow"
    assert rule.workspace_change == "reject"
    assert "writes only in this chat" in rule.description
    assert "runs no shell" in rule.description
    refused = await gate_shell_action(
        "ls -la", mode="read_only", binding=GateBinding(decision_sink=None)
    )
    assert refused.allowed is False
    assert refused.reason == READ_ONLY_SHELL_REASON


@pytest.mark.parametrize("mode", ALL_MODES)
async def test_a_stance_that_honours_an_approval_is_one_that_does_not_refuse_the_write(
    open_stance: Callable[..., object], mode: PermissionMode
) -> None:
    """``approval_reaches_the_agent`` against the real chokepoint, not the table
    it is derived from.

    What a surface needs to know is whether the Allow it offers would be acted
    on. A stance that REFUSES a project write refuses it whatever the reader
    answers, so an approval offered there is discarded; a stance that hands the
    write to a person — or runs it, having raised the ask for a reason of its
    own — acts on the answer. Driven here through ``_decide_permission`` so the
    derivation is pinned to behaviour: a policy change that started rejecting in
    a stance the browser still offers an Allow in fails here.
    """
    stance: _Stance = await open_stance(mode)  # type: ignore[misc]
    decided = await stance.decide(_fs(stance.workspace / "src/app.py"), "edit")
    assert (decided != "reject") is approval_reaches_the_agent(mode)
    assert (mode in MODES_THAT_HONOUR_AN_APPROVAL) is approval_reaches_the_agent(mode)
    assert (mode in MODES_THAT_DISCARD_AN_APPROVAL) is not approval_reaches_the_agent(mode)


async def test_auto_honours_an_approval_because_it_pauses_for_the_risky_step(
    open_stance: Callable[..., object],
) -> None:
    """The finding, as one case. Auto was read as a stance that approves nothing
    because it was missing from a hand-kept list of "the modes that may write" —
    written when the cloud had four stances, never revisited when auto became
    the fifth. Its row prompts on the destroy floor, and the box acts on the
    answer, so the browser must offer it."""
    assert approval_reaches_the_agent("auto")
    assert MODE_RULES["auto"].destroy == "prompt"
    stance: _Stance = await open_stance("auto")  # type: ignore[misc]
    assert (
        await stance.decide(_shell(f"rm -rf {stance.workspace}/build", Effect.DESTROY), "shell")
        == "prompt"
    )


def test_the_mirror_drops_a_relayed_allow_in_exactly_the_discarding_stances() -> None:
    """The box's own gate on a browser's answer reads the same derivation. Two
    lists here would be the same drift one step further in: a stance the card
    offers an Allow in, while the mirror drops it, is a button that does
    nothing."""
    assert NO_WRITE_MODES == MODES_THAT_DISCARD_AN_APPROVAL
    assert NO_WRITE_MODES == {"read_only", "plan"}


def test_the_server_withholds_an_approval_in_exactly_the_stances_that_discard_one() -> None:
    """Every surface renders the server's verdict (``approval_refusal`` on the
    chat read), so the server's table is the one held to the harness here.

    This is the guard the bug got past: a card kept its own list of the stances
    an approval may be offered in, `auto` joined the vocabulary without joining
    that list, and every Auto chat showed reject-only cards claiming the
    workspace was read-only. Both directions, so a stance dropped from either
    side fails rather than silently changing what a reader is offered.
    """
    assert set(APPROVAL_REFUSALS) == set(MODES_THAT_DISCARD_AN_APPROVAL)
    for mode in MODE_RULES:
        assert (approval_refusal(mode) is None) is approval_reaches_the_agent(mode)


def test_a_word_that_is_no_stance_is_answered_with_the_floor() -> None:
    assert approval_refusal("supervised") == APPROVAL_REFUSALS["read_only"]
    assert approval_refusal(None) == APPROVAL_REFUSALS["read_only"]
    assert approval_refusal("") == APPROVAL_REFUSALS["read_only"]


def _picker_entries() -> dict[str, tuple[str, str]]:
    """The mode picker's `{value, label, description}` rows. Every surface that
    offers a stance -- the portal, the editor webview, the Slack card's menu and
    `/alkera mode` -- renders this one registry (the web through its generated
    copy, which the drift gate holds to it)."""
    return {mode.value: (mode.label, mode.description) for mode in PERMISSION_MODES}


def test_the_picker_offers_exactly_the_modes_that_have_rows() -> None:
    assert set(_picker_entries()) == set(MODE_RULES)


@pytest.mark.parametrize("mode", ALL_MODES)
def test_the_picker_says_what_the_stance_table_says(mode: PermissionMode) -> None:
    """One wording, not two. The picker is the only place a reader learns what a
    stance does, so it quotes the row the machine acts on."""
    label, description = _picker_entries()[mode]
    assert description == MODE_RULES[mode].description
    assert label == MODE_RULES[mode].label
