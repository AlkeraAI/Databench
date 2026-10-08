"""A read-only cloud chat may read its own home — the sandbox's alias of the
working directory — whichever way the harness spells it.

Inside the sandbox the agent's ``$HOME`` is where the chat's working directory
is mounted, so ``ls /home/alkera`` is a read of the chat's own folder. opencode
raises that ask with the real path in the subject and the same path relative to
its worktree in the patterns — and in the sandbox the worktree is ``/``, so the
pattern reads ``home/alkera``. A fence that mapped only the rooted spelling
judged the pattern as a foreign directory, refused the chat's own folder, and
quoted it with its slash gone.

Driven through the real runtime over a ``FakeAdapter`` with the fence a cloud
chat installs (``SessionFence`` with the alias, the mirror's reason), in
``read_only``, so each case is the decision the session actually makes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import fence
from alkera_cli.cloud.refusal import fence_reason
from alkera_cli.harness import ChatSession, HarnessRuntime, PathFence, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.host import paths
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionOption, PermissionOptionId, PermissionRequest

_T = datetime(2026, 9, 28, tzinfo=UTC)


def _read_ask(real: str, pattern: str, *, request_id: str) -> PermissionRequest:
    """opencode's read ask: the real path in the subject (``metadata.filepath``),
    the worktree-relative spelling in the patterns."""
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_T,
        session_id="home-chat",
        request_id=request_id,
        tool_call_id=f"call-{request_id}",
        permission_kind="read",
        canonical_kind="other",
        patterns=[pattern],
        subject={
            "capability": "fs",
            "effect": "read",
            "operation": "read",
            "raw": real,
            "targets": [{"kind": "file", "name": real}],
            "classifier": "opencode-tool",
        },
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="reject_once", name="Reject"),
        ],
    )


class _Box:
    """A cloud box: the workspace, the chat's working directory inside its
    ``.alkera``, the operator's home beside it, and the sandbox's alias of the
    working directory — a directory that exists on the host too, as the mount
    point does on a real box."""

    def __init__(self, tmp: Path, alias: str | None) -> None:
        self.root = tmp / "work"
        self.working = self.root / ".alkera" / "chats" / "chat-a" / "work"
        (self.working / "scratch").mkdir(parents=True)
        (self.working / "scratch" / "x").write_text("x\n")
        self.home = tmp / "alkera-home"
        self.home.mkdir()
        if alias is None:
            mount = tmp / "sandbox-home"
            (mount / "scratch").mkdir(parents=True)
            (mount / "scratch" / "x").write_text("x\n")
            alias = str(mount)
        self.alias = alias
        self.fence = fence.SessionFence(
            root=self.root,
            folder=self.working,
            working_dir=self.working,
            home=self.home,
            aliases=((alias, self.working),),
        )

    def path_fence(self) -> PathFence:
        def _reason(request: PermissionRequest, escape: str) -> str:
            return fence_reason(writing=False, target=escape, boundary=self.root)

        def _escape(request: PermissionRequest) -> str | None:
            verdict = self.fence.judge_ask(request, writing=False)
            return verdict.target if verdict.escaped else None

        return PathFence(
            escape=_escape,
            reason=_reason,
            session=self.fence,
            canonical=self.fence.canonical,
        )


async def _never(request: PermissionRequest) -> PermissionOptionId:
    raise AssertionError("a read the fence decides must never reach a person")


@pytest.fixture
async def open_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Callable[[str | None], Awaitable[tuple[_Box, FakeAdapter]]]]:
    runtimes: list[HarnessRuntime] = []

    async def _open(alias: str | None) -> tuple[_Box, FakeAdapter]:
        box = _Box(tmp_path, alias)
        monkeypatch.setattr(paths, "ALKERA_HOME", box.home)
        factory = FakeAdapterFactory(FakeAdapter, available=True)
        runtime = HarnessRuntime(ProjectDirectory(box.root / ".alkera"), adapter_factory=factory)
        runtimes.append(runtime)
        session: ChatSession = await runtime.open_chat(
            create=True,
            harness_type="agent",
            permission_broker=PermissionBroker(_never, default_timeout_seconds=None),
            path_fence=box.path_fence(),
        )
        session.set_permission_mode("read_only")
        await asyncio.sleep(0.02)
        return box, factory.adapters[0]

    try:
        yield _open
    finally:
        for runtime in runtimes:
            for session_id in list(runtime.open_session_ids):
                await runtime.close_chat(session_id)


async def _answer(adapter: FakeAdapter, request: PermissionRequest) -> tuple[str, str | None]:
    await adapter.feed(request)
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        for (replied, option), (_, reason) in zip(
            adapter.permission_replies, adapter.permission_reply_reasons, strict=True
        ):
            if replied == request.request_id:
                return str(option), reason
        await asyncio.sleep(0.01)
    raise AssertionError(f"no answer for {request.request_id}")


#: ``None`` stands for an alias that is a real directory on the host, outside
#: the workspace — the shape that made the relative pattern read as foreign.
ALIASES = [
    pytest.param("/home/alkera", id="home-alkera"),
    pytest.param(None, id="alias-that-exists-on-the-host"),
]


@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("tail", ["", "/scratch", "/scratch/x"], ids=["home", "dir", "file"])
async def test_read_only_reads_the_chats_own_home(
    open_chat: Callable[[str | None], Awaitable[tuple[_Box, FakeAdapter]]],
    alias: str | None,
    tail: str,
) -> None:
    box, adapter = await open_chat(alias)
    real = box.alias + tail
    ask = _read_ask(real, real.lstrip("/"), request_id="req-home")
    assert await _answer(adapter, ask) == ("allow_once", None)


@pytest.mark.parametrize("alias", ALIASES)
async def test_read_only_reads_the_working_tree_by_its_host_name(
    open_chat: Callable[[str | None], Awaitable[tuple[_Box, FakeAdapter]]],
    alias: str | None,
) -> None:
    box, adapter = await open_chat(alias)
    real = str(box.working / "scratch" / "x")
    ask = _read_ask(real, real.lstrip("/"), request_id="req-host")
    assert await _answer(adapter, ask) == ("allow_once", None)


@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize(
    "real",
    [
        pytest.param("/etc/passwd", id="system-file"),
        # A climb out of the alias leaves the working directory, alias or not.
        pytest.param("{alias}/../../etc/passwd", id="climb-out-of-the-alias"),
    ],
)
async def test_read_only_refuses_a_foreign_read_and_quotes_the_path_as_given(
    open_chat: Callable[[str | None], Awaitable[tuple[_Box, FakeAdapter]]],
    alias: str | None,
    real: str,
) -> None:
    box, adapter = await open_chat(alias)
    spelled = real.format(alias=box.alias)
    ask = _read_ask(spelled, spelled.lstrip("/"), request_id="req-out")
    option, reason = await _answer(adapter, ask)
    assert option == "reject_once"
    assert reason is not None
    assert f'"{spelled}" is outside' in reason


@pytest.mark.parametrize("alias", ALIASES)
async def test_a_relative_pattern_that_climbs_out_of_the_alias_is_not_mapped(
    tmp_path: Path, alias: str | None
) -> None:
    """The restored-root reading maps only what stays under the alias: a
    pattern whose ``..`` leaves it is judged as spelled."""
    box = _Box(tmp_path, alias)
    climbing = (box.alias + "/.." * 20 + "/etc/passwd").lstrip("/")
    request = _read_ask(str(box.working / "scratch" / "x"), climbing, request_id="req-climb")
    assert box.fence.judge_ask(request, writing=False).escaped
