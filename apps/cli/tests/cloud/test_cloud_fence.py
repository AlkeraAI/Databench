"""The cloud mirror's workspace fence.

A cloud session is read-only, and what it may read is the workspace and
nothing else: never the box's own ``ALKERA_HOME`` (the operator's gateway
token lives there), never a path that climbs out of the project through
``..``, a symlink, ``~`` or an absolute location. The fence is a pure decision
over a tool call's path-bearing arguments; the mirror consults it on every
permission ask it is handed before parking the ask for a reader, and answers
an escaping ask with the plain-language refusal instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket, DocHandle, fence
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.cloud.refusal import REFUSAL_COPY, fence_reason, quote_statement
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.harness import HarnessRuntime, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.host import paths
from alkera_cli.plugins.plugin_base.permissions.config import (
    PermissionRule,
    PermissionsConfig,
    save_permissions,
)
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    Event,
    PermissionOption,
    PermissionRequest,
    ToolCall,
)

_T = datetime(2026, 9, 6, tzinfo=UTC)
CHAT_ID = "chat-fence"
OWNER = "00000000-0000-4000-8000-000000000001"
SECRET = "eyJ-operator-jwt-DO-NOT-LEAK-7f3a9c"

OUTSIDE = REFUSAL_COPY["outside_workspace"]


# --------------------------------------------------------------------------- #
# the roots every case is judged against
# --------------------------------------------------------------------------- #


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A workspace, an ``ALKERA_HOME`` beside it, a user home ``~`` resolves
    to, and a symlink inside the workspace that points out of it."""
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


def _first(
    roots: Mapping[str, Path], tool: str, args: Mapping[str, Any], **kwargs: Any
) -> str | None:
    return fence.first_escape(tool, args, root=roots["workspace"], **kwargs)


def _spilled_path(output_path: str | None) -> str:
    """The path a shortened tool result named, proven to be a real file before the
    fence is asked about it. SYNC, so the blocking stat stays out of the async test
    body (ruff ASYNC240)."""
    assert output_path and Path(output_path).is_file(), (
        f"the tool reported no spill file: {output_path!r}"
    )
    return output_path


# --------------------------------------------------------------------------- #
# the path matrix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        pytest.param("read", {"file_path": "src/app.py"}, id="relative-inside"),
        pytest.param("read", {"filePath": "README.md"}, id="camel-case-key"),
        pytest.param("read", {"path": "src/../README.md"}, id="dotdot-that-stays-inside"),
        pytest.param("read", {"file_path": "./src/./app.py"}, id="dot-segments"),
        pytest.param("read", {"file_path": "src"}, id="a-directory-inside"),
        pytest.param("read", {"file_path": "does/not/exist.txt"}, id="missing-but-inside"),
        pytest.param("list", {"path": "."}, id="the-root-itself"),
        pytest.param("glob", {"pattern": "**/*.py"}, id="glob-under-root"),
        pytest.param("glob", {"pattern": "src/**/*.py", "path": "."}, id="glob-with-dir"),
        pytest.param(
            "grep", {"pattern": "/api/v1/chats", "path": "src"}, id="grep-regex-is-not-a-path"
        ),
        pytest.param("grep", {"pattern": "a..b", "include": "*.py"}, id="grep-include-glob"),
        pytest.param(
            "bash", {"cwd": "src", "command": "cat /etc/passwd"}, id="shell-command-is-not-a-path"
        ),
        pytest.param("read", {"file_path": "src/app.py", "cwd": "src"}, id="cwd-inside"),
        pytest.param("read", {"file_path": 3, "path": None}, id="non-string-values"),
        pytest.param("read", {}, id="no-arguments"),
        pytest.param("read", {"file_path": ""}, id="empty-path"),
    ],
)
def test_a_path_inside_the_workspace_passes(
    roots: dict[str, Path], tool: str, args: dict[str, Any]
) -> None:
    assert _first(roots, tool, args) is None


@pytest.mark.parametrize(
    ("tool", "args", "offending"),
    [
        pytest.param(
            "read", {"file_path": "../outside/leak.txt"}, "../outside/leak.txt", id="dotdot-out"
        ),
        pytest.param(
            "read",
            {"file_path": "src/../../outside/leak.txt"},
            "src/../../outside/leak.txt",
            id="dotdot-out-from-a-subdir",
        ),
        pytest.param("read", {"file_path": "/etc/passwd"}, "/etc/passwd", id="absolute-outside"),
        pytest.param(
            "read", {"path": "~/.ssh/id_ed25519"}, "~/.ssh/id_ed25519", id="tilde-expands-out"
        ),
        pytest.param(
            "read",
            {"file_path": "escape/leak.txt"},
            "escape/leak.txt",
            id="symlinked-directory-escapes",
        ),
        pytest.param("read", {"file_path": "cross"}, "cross", id="symlinked-file-escapes"),
        pytest.param("glob", {"pattern": "../**/*.yml"}, "../**/*.yml", id="glob-climbs-out"),
        pytest.param("glob", {"pattern": "/etc/*"}, "/etc/*", id="absolute-glob"),
        pytest.param(
            "glob",
            {"pattern": "*/../../outside/*"},
            "*/../../outside/*",
            id="glob-dotdot-after-a-wildcard",
        ),
        pytest.param("glob", {"pattern": "**/*.py", "path": "/"}, "/", id="glob-dir-outside"),
        pytest.param("grep", {"pattern": "token", "path": "/opt"}, "/opt", id="grep-dir-outside"),
        pytest.param(
            "grep",
            {"pattern": "token", "include": "../*.yml"},
            "../*.yml",
            id="grep-include-climbs-out",
        ),
        pytest.param("bash", {"cwd": "/tmp"}, "/tmp", id="cwd-outside"),
        pytest.param(
            "read", {"file_path": "app.py", "cwd": "/etc"}, "/etc", id="cwd-outside-before-the-file"
        ),
        pytest.param(
            "read",
            {"file_path": "../work/../outside/leak.txt"},
            "../work/../outside/leak.txt",
            id="round-trip-through-the-parent",
        ),
        pytest.param(
            "read",
            {"file_path": "src/app.py", "path": "/etc/hosts"},
            "/etc/hosts",
            id="one-escaping-key-among-many",
        ),
    ],
)
def test_a_path_outside_the_workspace_is_named(
    roots: dict[str, Path], tool: str, args: dict[str, Any], offending: str
) -> None:
    assert _first(roots, tool, args) == offending


def test_a_directory_that_merely_shares_the_roots_prefix_is_outside(
    roots: dict[str, Path], tmp_path: Path
) -> None:
    sibling = tmp_path / "work-2"
    sibling.mkdir()
    assert _first(roots, "read", {"file_path": str(sibling / "x")}) == str(sibling / "x")


def test_the_parent_of_the_workspace_is_outside(roots: dict[str, Path]) -> None:
    parent = str(roots["workspace"].parent)
    assert _first(roots, "list", {"path": parent}) == parent
    assert _first(roots, "list", {"path": ".."}) == ".."


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("auth.yml", id="the-token-file"),
        pytest.param(".", id="the-home-itself"),
        pytest.param("preferences.yml", id="any-file-in-it"),
    ],
)
def test_alkera_home_is_refused_even_when_it_sits_inside_the_workspace(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The box may be provisioned with ``ALKERA_HOME`` under the project; the
    operator's token is still not the workspace's to read."""
    nested = roots["workspace"] / ".alkera-home"
    nested.mkdir()
    (nested / "auth.yml").write_text(f"token: {SECRET}\n")
    monkeypatch.setattr(paths, "ALKERA_HOME", nested)
    relative = str(Path(".alkera-home") / path)
    assert _first(roots, "read", {"file_path": relative}) == relative
    absolute = str(nested / path)
    assert _first(roots, "read", {"file_path": absolute}) == absolute
    # An explicit home wins over the ambient one, so a caller can pin it.
    assert _first(roots, "read", {"file_path": relative}, home=roots["home"]) is None


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param(".alkera/team-connections.json", id="the-connections-manifest"),
        pytest.param(
            ".alkera/plugins/postgres/team-connections/pg/credential",
            id="a-file-custody-connector-credential",
        ),
        pytest.param(".alkera/chats/other-chat/events.jsonl", id="another-chats-transcript"),
        pytest.param(".alkera/decisions.jsonl", id="the-decisions-log"),
    ],
)
def test_the_daemon_state_dir_is_refused_even_though_it_is_under_the_root(
    roots: dict[str, Path], relative: str
) -> None:
    """``<root>/.alkera`` is the daemon's own state — other chats'
    transcripts, file-custody connector credentials, the connections manifest — not
    the customer's project. It sits inside the root, so only this explicit rule keeps
    a read tool out of it."""
    assert _first(roots, "read", {"file_path": relative}) == relative
    absolute = str(roots["workspace"] / relative)
    assert _first(roots, "read", {"file_path": absolute}) == absolute


def test_a_glob_into_the_daemon_state_dir_is_refused(roots: dict[str, Path]) -> None:
    assert _first(roots, "glob", {"pattern": ".alkera/**/*"}) == ".alkera/**/*"
    assert _first(roots, "glob", {"pattern": ".alkera/chats/**/events.jsonl"}) is not None


def test_the_sessions_own_sandbox_under_alkera_is_the_one_carve_out(roots: dict[str, Path]) -> None:
    """The session's own scratch dir (where plan/scratch files live) is the single
    location inside ``.alkera`` a session may still reach — passed in explicitly, so
    ANOTHER chat's sandbox, and the rest of ``.alkera``, stay refused."""
    sandbox = roots["workspace"] / ".alkera" / "chats" / "c1" / "sandbox"
    own = ".alkera/chats/c1/sandbox/plan.md"
    other = ".alkera/chats/c2/sandbox/plan.md"
    # with the carve-out, the session's own sandbox file passes
    assert _first(roots, "read", {"file_path": own}, sandbox=sandbox) is None
    # another chat's sandbox is still refused
    assert _first(roots, "read", {"file_path": other}, sandbox=sandbox) == other
    # and the connections manifest is still refused even with a sandbox carve-out
    assert (
        _first(roots, "read", {"file_path": ".alkera/team-connections.json"}, sandbox=sandbox)
        == ".alkera/team-connections.json"
    )
    # without the carve-out (the default), even the session's own sandbox is refused
    assert _first(roots, "read", {"file_path": own}) == own


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only bash tool")
async def test_a_cloud_session_can_read_the_file_its_own_output_spilled_to(
    roots: dict[str, Path],
) -> None:
    """A command whose output is shortened tells the model where the rest of it
    went, and the model is told to read that file rather than run the command
    again. The path has to be one the fence admits: written into the daemon's own
    state directory it is a path this session is refused, and the only thing the
    model can do with it is spend another turn re-running the command.

    Built from the real tool so the location under test is the one it reports.
    """
    from alkera_cli.plugins.plugin_base import ToolRegistry
    from alkera_cli.plugins.plugin_base.bash_tool import register_bash_tools
    from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink

    workspace = roots["workspace"]
    folder = workspace / ".alkera" / "chats" / CHAT_ID
    working_dir = folder / "work"
    working_dir.mkdir(parents=True)

    project = ProjectDirectory(workspace / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    register_bash_tools(registry)
    out = await registry.dispatch(
        "bash",
        {"command": "for i in $(seq 1 4000); do echo 'line-'$i; done", "description": "loud"},
        alkera_dir=str(project.path),
        sandbox_dir=str(working_dir),
    )
    spilled = _spilled_path(out["output_path"])

    # The fence a cloud chat runs under: the workspace, minus ALKERA_HOME, minus
    # `.alkera` — except this chat's own folder.
    assert _first(roots, "read", {"file_path": spilled}, sandbox=folder) is None
    assert _first(roots, "grep", {"pattern": "line-9", "path": spilled}, sandbox=folder) is None
    # ANOTHER chat's folder is still not a place this session may read from.
    other = workspace / ".alkera" / "chats" / "other"
    assert _first(roots, "read", {"file_path": spilled}, sandbox=other) == spilled


def test_the_default_home_is_read_at_call_time(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test override or a custom install moves ``ALKERA_HOME`` after import;
    the fence follows it rather than a value frozen at import time."""
    moved = roots["workspace"] / "moved-home"
    moved.mkdir()
    assert _first(roots, "read", {"file_path": "moved-home/auth.yml"}) is None
    monkeypatch.setattr(paths, "ALKERA_HOME", moved)
    assert _first(roots, "read", {"file_path": "moved-home/auth.yml"}) == "moved-home/auth.yml"


def test_the_fence_never_opens_the_file(roots: dict[str, Path]) -> None:
    """Deciding is a path computation: the token file is not read to decide
    that it may not be read."""
    target = roots["home"] / "auth.yml"
    before = target.stat().st_atime_ns
    target.chmod(0o000)
    try:
        assert _first(roots, "read", {"file_path": str(target)}) == str(target)
    finally:
        target.chmod(0o600)
    assert target.stat().st_atime_ns == before


# --------------------------------------------------------------------------- #
# a permission ask, as the mirror sees one
# --------------------------------------------------------------------------- #


def _ask(
    kind: str,
    *,
    raw: str | None = None,
    patterns: list[str] | None = None,
    subject: bool = True,
    request_id: str = "req-1",
) -> PermissionRequest:
    descriptor: dict[str, Any] | None = None
    if subject:
        descriptor = {
            "capability": "shell" if kind == "bash" else "fs",
            "effect": "write" if kind == "bash" else "read",
            "operation": kind,
            "raw": raw,
            "targets": [{"kind": "file", "name": raw}] if raw else [],
            "classifier": "opencode-tool",
        }
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_T,
        session_id=CHAT_ID,
        request_id=request_id,
        tool_call_id=f"call-{request_id}",
        permission_kind=kind,
        canonical_kind="shell" if kind == "bash" else "other",
        patterns=patterns or [],
        subject=descriptor,
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="reject_once", name="Reject"),
        ],
    )


@pytest.mark.parametrize(
    ("ask", "offending"),
    [
        pytest.param(_ask("read", raw="src/app.py"), None, id="read-inside"),
        pytest.param(_ask("read", raw="/etc/passwd"), "/etc/passwd", id="read-outside"),
        pytest.param(_ask("read", raw="~/.ssh/id_ed25519"), "~/.ssh/id_ed25519", id="read-tilde"),
        pytest.param(
            _ask("read", raw="src/app.py", patterns=["/etc/*"]), "/etc/*", id="read-pattern-outside"
        ),
        pytest.param(_ask("glob", patterns=["**/*.py"]), None, id="glob-inside"),
        pytest.param(_ask("glob", patterns=["../**"]), "../**", id="glob-climbs"),
        pytest.param(_ask("glob", raw="/opt", patterns=["*.yml"]), "/opt", id="glob-dir-outside"),
        pytest.param(_ask("grep", raw="src", patterns=["/api/v1"]), None, id="grep-regex-ignored"),
        pytest.param(_ask("grep", raw="/opt", patterns=["x"]), "/opt", id="grep-dir-outside"),
        pytest.param(_ask("list", raw=".."), "..", id="list-parent"),
        pytest.param(
            _ask("external_directory", raw="/opt/alkera"), "/opt/alkera", id="external-dir"
        ),
        pytest.param(_ask("bash", raw="cat /etc/passwd"), None, id="shell-is-not-fenced-by-path"),
        pytest.param(_ask("read", subject=False), None, id="no-subject-no-paths"),
        pytest.param(_ask("read", raw=None, patterns=["../*"]), "../*", id="patterns-only"),
    ],
)
def test_an_ask_is_judged_by_every_location_it_names(
    roots: dict[str, Path], ask: PermissionRequest, offending: str | None
) -> None:
    assert fence.ask_escape(ask, root=roots["workspace"]) == offending


# --------------------------------------------------------------------------- #
# a location the harness spelled against ITS OWN root
# --------------------------------------------------------------------------- #


def _a_system_file() -> str:
    """A file the operating system itself owns, well outside any workspace.

    It has to be a name that really exists: the fence re-reads a root-relative
    spelling as absolute only when that reading lands on a real location, so
    ``/etc/passwd`` — which no Windows box has — would make the case pass for
    the wrong reason there.
    """
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", "C:\\Windows"))
        return str(system_root / "System32" / "drivers" / "etc" / "hosts")
    return "/etc/passwd"


def _as_opencode_spells_it(target: str) -> str:
    """``target`` as opencode reports it when its worktree is the filesystem
    root: forward slashes, no leading slash, and on Windows no drive letter
    either — the harness speaks POSIX paths whatever the host runs."""
    return re.sub(r"^(?:[A-Za-z]:)?/*", "", Path(target).as_posix())


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(lambda r: str(r["home"] / "auth.yml"), id="the-operators-token"),
        pytest.param(lambda r: str(r["outside"] / "leak.txt"), id="a-file-beside-the-workspace"),
        pytest.param(lambda _r: _a_system_file(), id="a-system-file"),
    ],
)
def test_a_location_spelled_without_its_leading_slash_is_still_that_location(
    roots: dict[str, Path], target: Any
) -> None:
    """opencode reports a path relative to ITS worktree, and for a project that
    is not a repository that worktree is ``/`` — so ``/etc/passwd`` arrives as
    ``etc/passwd``. Read against the project root that is a file which is simply
    not there, and a fence a spelling can walk around is not a fence."""
    spelled = _as_opencode_spells_it(str(target(roots)))
    assert _first(roots, "read", {"file_path": spelled}) == spelled
    assert fence.ask_escape(_ask("read", raw=spelled), root=roots["workspace"]) == spelled


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        pytest.param("read", {"file_path": "does/not/exist.txt"}, id="a-name-that-exists-nowhere"),
        pytest.param("read", {"file_path": "src/app.py"}, id="a-real-file-in-the-workspace"),
        pytest.param("read", {"file_path": "README.md"}, id="a-real-file-at-the-root"),
        pytest.param("glob", {"pattern": "src/**/*.py"}, id="a-glob-under-the-root"),
    ],
)
def test_the_leading_slash_reading_never_refuses_an_ordinary_relative_path(
    roots: dict[str, Path], tool: str, args: dict[str, Any]
) -> None:
    """The other reading is only taken when the name is NOT where it says it is
    AND is a real location outside: a miss stays a miss (a read of a file that
    is not there discloses nothing), and a real in-root file is never re-read as
    ``/src/app.py``."""
    assert _first(roots, tool, args) is None


def test_an_ask_for_the_token_file_names_it(roots: dict[str, Path]) -> None:
    target = str(roots["home"] / "auth.yml")
    assert fence.ask_escape(_ask("read", raw=target), root=roots["workspace"]) == target


# --------------------------------------------------------------------------- #
# the mirror refuses an escaping ask and the session keeps going
# --------------------------------------------------------------------------- #


class _Mirror:
    def __init__(self, mirror: ChatMirror, adapter: FakeAdapter, roots: dict[str, Path]) -> None:
        self.mirror = mirror
        self.adapter = adapter
        self.roots = roots

    async def feed(self, *events: Event) -> None:
        for event in events:
            await self.adapter.feed(event)
        await asyncio.sleep(0.05)

    async def ask(self, request: PermissionRequest) -> str:
        """Feed a permission ask; return the option the harness was answered,
        or ``"parked"`` once the mirror has put the ask in front of a reader."""
        await self.adapter.feed(request)
        return await self._settled(request.request_id)

    async def answer(self, request_id: str, option: str) -> str:
        """A reader answers a parked ask; return what the harness was told."""
        self.mirror._answer_interrupt(request_id, {"option_id": option})
        return await self._settled(request_id, parked_is_final=False)

    async def _settled(self, request_id: str, *, parked_is_final: bool = True) -> str:
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            for replied, option in self.adapter.permission_replies:
                if replied == request_id:
                    await asyncio.sleep(0.05)
                    return str(option)
            if parked_is_final and request_id in self.mirror.pending_interrupts:
                return "parked"
            await asyncio.sleep(0.02)
        raise AssertionError(f"no answer for {request_id}")

    def entries(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not self.mirror._outbound.empty():
            entry = self.mirror._outbound.get_nowait()
            if "__meta__" not in entry:
                out.append(entry)
        return out

    @staticmethod
    def notes_of(entries: list[dict[str, Any]]) -> list[str]:
        return [
            str(entry["payload"]["part"]["text"])
            for entry in entries
            if entry["kind"] == "part.created"
            and entry["payload"].get("part", {}).get("type") == "text"
            and entry["role"] == "system"
        ]


@pytest.fixture
async def fenced(roots: dict[str, Path]) -> AsyncIterator[_Mirror]:
    """A mirror over a FakeAdapter whose reads reach the mirror's own
    permission resolver: the project asks on every filesystem read, which is
    what routes a read-only session's read to the broker the mirror installed."""
    workspace = roots["workspace"]
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
    save_permissions(
        workspace / ".alkera",
        PermissionsConfig(
            rules=[PermissionRule(capability="fs", effect=Effect.READ, decision="ask")]
        ),
    )
    runtime.project.chats().create(session_id=CHAT_ID, title="t", harness_type="agent").close()
    handle = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://objects.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={})),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
    )
    broker = PermissionBroker(handle._resolve_permission, default_timeout_seconds=None)
    handle._session = await runtime.open_chat(CHAT_ID, permission_broker=broker)
    handle._session.set_permission_mode("read_only")
    # The subscription is registered here, as the mirror's own start() does,
    # so nothing published before the task's first step is missed.
    pump = asyncio.get_running_loop().create_task(handle._pump(handle._session.subscribe()))
    handle._outbound = asyncio.Queue()
    try:
        yield _Mirror(handle, factory.adapters[0], roots)
    finally:
        pump.cancel()
        with __import__("contextlib").suppress(BaseException):
            await pump
        await runtime.close_chat(CHAT_ID)


async def test_a_read_of_the_token_file_is_refused_in_plain_language(fenced: _Mirror) -> None:
    target = str(fenced.roots["home"] / "auth.yml")
    assert await fenced.ask(_ask("read", raw=target, request_id="req-auth")) == "reject_once"
    entries = fenced.entries()
    assert fenced.notes_of(entries) == [f"{OUTSIDE}\n{quote_statement(target)}"]
    assert fenced.mirror.pending_interrupts == [], "an escaping ask is never parked on a reader"
    assert fenced.mirror.failure is None


async def test_a_read_inside_the_workspace_is_not_the_fences_business(fenced: _Mirror) -> None:
    """An ask the fence passes takes the path it always took: parked for a
    reader, without a word, and a reader's allow reaches the harness."""
    target = str(fenced.roots["workspace"] / "README.md")
    assert await fenced.ask(_ask("read", raw=target, request_id="req-readme")) == "parked"
    assert fenced.notes_of(fenced.entries()) == []
    assert await fenced.answer("req-readme", "allow_once") == "allow_once"
    assert fenced.notes_of(fenced.entries()) == []


async def test_the_turn_continues_after_a_refused_read(fenced: _Mirror) -> None:
    """The refusal is an answer to one ask, not the end of the session: the
    next ask is decided on its own, and the harness's events keep flowing."""
    outside = str(fenced.roots["outside"] / "leak.txt")
    inside = str(fenced.roots["workspace"] / "src" / "app.py")
    assert await fenced.ask(_ask("read", raw=outside, request_id="req-1")) == "reject_once"
    assert await fenced.ask(_ask("read", raw=inside, request_id="req-2")) == "parked"
    assert await fenced.answer("req-2", "allow_once") == "allow_once"
    assert fenced.notes_of(fenced.entries()) == [f"{OUTSIDE}\n{quote_statement(outside)}"]
    await fenced.feed(
        ToolCall(
            event_id="tc-after",
            time=_T,
            session_id=CHAT_ID,
            tool_call_id="call-after",
            message_id="m-after",
            tool_name="read",
            input={"filePath": inside},
            status="running",
        )
    )
    kinds = [entry["kind"] for entry in fenced.entries()]
    assert "tool.call" in kinds, kinds
    assert fenced.mirror.failure is None
    assert fenced.mirror.pending_interrupts == []


async def test_the_secret_never_appears_in_anything_the_mirror_emits(fenced: _Mirror) -> None:
    """Grep the whole output surface — every published entry, every note, every
    reply the harness was given — for the token: it must not be there, in any
    spelling, even though the file exists and the ask named it."""
    home = fenced.roots["home"]
    asks = [
        _ask("read", raw=str(home / "auth.yml"), request_id="req-a"),
        _ask("read", raw="~/.ssh/id_ed25519", request_id="req-b"),
        _ask("glob", raw=str(home), patterns=["*.yml"], request_id="req-c"),
        _ask("grep", raw=str(home), patterns=["token"], request_id="req-d"),
    ]
    for ask in asks:
        assert await fenced.ask(ask) == "reject_once", ask.request_id
    surface = json.dumps(fenced.entries(), default=str)
    surface += json.dumps(fenced.adapter.permission_reply_reasons, default=str)
    surface += json.dumps(fenced.adapter.permission_replies, default=str)
    assert SECRET not in surface
    assert "DO-NOT-LEAK" not in surface
    assert (home / "auth.yml").read_text(encoding="utf-8").count(SECRET) == 1, (
        "the file itself is untouched"
    )


# --------------------------------------------------------------------------- #
# the bound rides INTO the session the mirror starts
# --------------------------------------------------------------------------- #


class _LiveSocket(CloudSocket):
    """A socket whose document is live the moment it is opened: what is under
    test here is the session the mirror starts, not the transport."""

    def open_doc(self, doc_type: Any, doc_id: str, *, presence: bool = True) -> DocHandle:
        handle = super().open_doc(doc_type, doc_id, presence=presence)
        handle.can_write = True
        handle.live.set()
        return handle


async def test_the_mirror_starts_a_session_bound_to_the_workspace(
    roots: dict[str, Path],
) -> None:
    """The broker hook below is the SECOND line, and on its own it is not enough:
    a filesystem read never reaches a broker (the policy's read fast path answers
    it), so the workspace bound has to ride into the session the mirror starts.
    Drive the mirror's own start and pin that it does — a read of the operator's
    token is refused by the session, with the reader-facing sentence, and is never
    put in front of a reader."""
    workspace = roots["workspace"]
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
    rest = CloudRestClient(
        api_url="http://objects.test",
        token="device-jwt",
        agent_id=CHAT_ID,
        transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={})),
    )
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=_LiveSocket(rest),
        rest=rest,
        user_id=OWNER,
        owner_user_id=OWNER,
    )
    await mirror.start()
    try:
        adapter = factory.adapters[0]
        target = str(roots["home"] / "auth.yml")
        await adapter.feed(_ask("read", raw=target, request_id="req-mirror"))
        deadline = asyncio.get_running_loop().time() + 5.0
        while not adapter.permission_replies:
            assert asyncio.get_running_loop().time() < deadline, "no answer for req-mirror"
            await asyncio.sleep(0.01)
        assert adapter.permission_replies == [("req-mirror", "reject_once")]
        # The model is told what the fence did and where it may go instead --
        # the session's bound is not a person's rejection, and saying so would
        # have the model reason from a human who was never asked.
        # The way out it names is the chat's root, the one directory the model
        # can type, never the box's project around it.
        assert adapter.permission_reply_reasons == [
            ("req-mirror", fence_reason(writing=False, target=target, boundary=mirror.working_dir))
        ]
        assert mirror.pending_interrupts == []
        assert mirror.failure is None
    finally:
        await mirror.stop()
