"""The agent's whole world is one root folder, and every way out of it is
refused by the one judge every decision path asks.

The fence under test is the one the cloud mirror builds: a real
:class:`ChatMirror` over a real project, on a host whose probe says gVisor,
so the aliases are the ones ``ChatMirror._mount_aliases`` composes (the
working directory at the sandbox's home, every internal mount at its fixed
path under ``/opt/alkera``, from the same bind table the launch mounts from),
the Python environments its own tree, the system roots its own list. Each
case is an attempt from the audit of a real box: a path the
container does hold but the agent must not reach (its own session database,
its instructions, the agent binary), a path it does not hold but used to
(the chat folder by its host path), a climb out of the root, a link planted
inside it pointing out, a process-table read, a glob that walks up, a
destination outside the folder, and the same attempts spelled through the
container's environment or wrapped in an interpreter the shell model cannot
read. The allowed cases pin that the root and the environment still work;
without the aliases the environment would be refused and the root would be a
foreign path, so they are what the refused cases are measured against.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud import mirror as mirror_module
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.agent_root import agent_config_root
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.host import paths
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionOption, PermissionRequest

#: Every case here is a gVisor box's container path, planted links included;
#: a Windows host runs no such box, and the fence's own dialect rules are
#: proven in ``test_cloud_shell_fence.py``.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a gVisor box's paths are POSIX")

HOME = "/home/alkera"
ENV = f"{sb.ENVS_MOUNT}/alkera"
DATA = f"{sb.HARNESS_DATA_MOUNT}/agent"
_T = datetime(2026, 10, 4, tzinfo=UTC)


CHAT_ID = "chat-a"
OWNER = "00000000-0000-4000-8000-000000000001"

#: What the probe finds on a gVisor box: root, the controls, runsc, the staged
#: rootfs, the interpreter and uv. The mirror's aliases follow from it.
GVISOR_READY = SandboxCapability(
    platform="linux",
    root=True,
    setpriv="/usr/bin/setpriv",
    setfacl="/usr/bin/setfacl",
    runsc="/usr/bin/runsc",
    cgroup="systemd",
    reason="injected: gvisor ready",
    rootfs="/opt/alkera/rootfs/current",
    ip="/usr/sbin/ip",
    nft="/usr/sbin/nft",
    unshare="/usr/bin/unshare",
    mount_ns=True,
    uv="/usr/local/bin/uv",
    python_home="/opt/alkera/python/current",
    resolvers=("172.31.0.2",),
)
#: A host with nothing to apply: not root, no runsc. Nothing is mounted
#: anywhere, so the mirror aliases nothing.
NOTHING_TO_APPLY = SandboxCapability(
    platform="linux",
    root=False,
    setpriv=None,
    runsc=None,
    cgroup="none",
    reason="injected: nothing to apply",
)


class _Box:
    """A box's disk as the mirror sees it, and the fence the mirror builds for
    its chat: a real :class:`ChatMirror` over a real project, on a host whose
    probe is pinned, so the aliases under test are the ones
    ``ChatMirror._mount_aliases`` composes and nothing hand-made. The disk
    holds the work root, the daemon's home, two chats, this chat's runtime
    state and agent root, and the links an agent could plant in its root."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        cap: SandboxCapability = GVISOR_READY,
        mode: str = "gvisor",
    ) -> None:
        monkeypatch.setenv(sb.ENV_MODE, mode)
        monkeypatch.setattr(mirror_module, "current_capability", lambda: cap)
        self.root = tmp_path / "work"
        runtime = HarnessRuntime(
            ProjectDirectory(self.root / ".alkera"),
            adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
        )
        chats = runtime.project.chats()
        chats.create(session_id=CHAT_ID, title="a", harness_type="agent").close()
        chats.create(session_id="chat-b", title="b", harness_type="agent").close()
        self.mirror = ChatMirror(
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
        self.chat_dir = self.mirror.chat_folder
        self.working = self.mirror.working_dir
        (self.working / "sub").mkdir(parents=True)
        (self.working / "notes.md").write_text("hello\n")
        self.home = paths.ALKERA_HOME
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "auth.yml").write_text("token: secret\n")
        self.runtime = self.chat_dir / sb.RUNTIME_STATE_SUBDIR
        (self.runtime / "agent").mkdir(parents=True)
        (self.runtime / "agent" / "agent.db").write_bytes(b"SQLite format 3\0")
        self.envs = self.runtime / sb.ENVS_SUBDIR
        (self.envs / "alkera" / "bin").mkdir(parents=True)
        (self.envs / "alkera" / "pyvenv.cfg").write_text("home = /opt/alkera/python\n")
        (self.envs / "alkera" / "bin" / "python").write_text("")
        self.agent_root = agent_config_root(CHAT_ID)
        (self.agent_root / "config").mkdir(parents=True)
        (self.agent_root / "config" / "global-instructions.md").write_text("## Your root\n")
        (self.agent_root / "state").mkdir()
        self.other = mirror_module.chat_sandbox_path(chats.path / "chat-b")
        self.other.mkdir(parents=True, exist_ok=True)
        (self.other / "secret.csv").write_text("a,b\n")
        # Links an agent could plant in its root: out to the daemon's token
        # file, to its own database by host path and by container path, and
        # into its own environment (the one that leads somewhere it may go).
        (self.working / "leak").symlink_to(self.home / "auth.yml")
        (self.working / "to-db").symlink_to(self.runtime / "agent" / "agent.db")
        (self.working / "to-internal").symlink_to(f"{DATA}/agent.db")
        (self.working / "to-env").symlink_to(self.envs / "alkera" / "pyvenv.cfg")
        self.fence = self.mirror.session_fence
        self.env = {
            "HOME": HOME,
            "PATH": f"{ENV}/bin:{sb.AGENT_MOUNT}:/usr/bin:/bin",
            "VIRTUAL_ENV": ENV,
            "XDG_DATA_HOME": sb.HARNESS_DATA_MOUNT,
            "XDG_CONFIG_HOME": sb.HARNESS_CONFIG_MOUNT,
        }


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Box:
    return _Box(tmp_path, monkeypatch)


def _ask(kind: str, raw: str, *, patterns: list[str] | None = None) -> PermissionRequest:
    writing = kind in ("edit", "write")
    return PermissionRequest(
        event_id="ev-1",
        time=_T,
        session_id="chat-a",
        request_id="req-1",
        tool_call_id="call-1",
        permission_kind=kind,
        canonical_kind="other",
        patterns=patterns or [],
        subject={
            "capability": "fs",
            "effect": "write" if writing else "read",
            "operation": kind,
            "raw": raw,
            "targets": [{"kind": "file", "name": raw}],
            "classifier": "opencode-tool",
        },
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="reject_once", name="Reject"),
        ],
    )


# ---------------------------------------------------------------------------
# The shell: every attempt from the audit
# ---------------------------------------------------------------------------

#: ``(command, expected outcome, the location a refusal quotes)``. A ``{box}``
#: field is spelled with the box's host paths, which the agent may still type.
ESCAPES: list[Any] = [
    pytest.param("cat /proc/1/environ", "/proc/1/environ", id="the-agent-servers-environ"),
    pytest.param("ps eww -p 1", "/proc/all/environ", id="ps-with-environments"),
    pytest.param(f"ls {DATA}", DATA, id="the-session-database-dir"),
    pytest.param(f"cat {DATA}/agent.db", f"{DATA}/agent.db", id="the-session-database"),
    pytest.param(
        f"cat {sb.HARNESS_CONFIG_MOUNT}/global-instructions.md",
        f"{sb.HARNESS_CONFIG_MOUNT}/global-instructions.md",
        id="the-instructions-file",
    ),
    pytest.param(f"ls {sb.HARNESS_STATE_MOUNT}", sb.HARNESS_STATE_MOUNT, id="the-agent-state"),
    pytest.param(f"ls {sb.HARNESS_MOUNT}", sb.HARNESS_MOUNT, id="the-harness-root"),
    pytest.param(f"ls {sb.AGENT_MOUNT}", sb.AGENT_MOUNT, id="the-agent-binary-dir"),
    pytest.param("ls ..", "..", id="dot-dot"),
    pytest.param("ls ../..", "../..", id="dot-dot-twice"),
    pytest.param("ls /home", "/home", id="the-parent-of-the-root"),
    pytest.param("ls /", "/", id="the-container-root"),
    pytest.param("ls /opt", "/opt", id="the-internal-prefixes-parent"),
    pytest.param(f"ls {sb.INTERNAL_PREFIX}", sb.INTERNAL_PREFIX, id="the-internal-prefix"),
    pytest.param("cat /home/alkera/../../etc/passwd", "/home/alkera/../../etc/passwd", id="climb"),
    pytest.param("cat ../manifest.json", "../manifest.json", id="a-record-by-dot-dot"),
    pytest.param("cat /home/alkera/../manifest.json", "/home/alkera/../manifest.json", id="rec"),
    # Spelled through the agent's own environment: the shell model expands the
    # variable and the tilde from the command's environment, and the refusal
    # quotes the location it resolved.
    pytest.param("ls ~/..", f"{HOME}/..", id="tilde-up"),
    pytest.param("cat $HOME/../../etc/passwd", f"{HOME}/../../etc/passwd", id="home-var-up"),
    pytest.param("cat $XDG_DATA_HOME/agent/agent.db", f"{DATA}/agent.db", id="data-var"),
    pytest.param(
        "cat $XDG_CONFIG_HOME/global-instructions.md",
        f"{sb.HARNESS_CONFIG_MOUNT}/global-instructions.md",
        id="config-var",
    ),
    pytest.param("ls {chat_dir}/.runtime/agent", "{chat_dir}/.runtime/agent", id="host-runtime"),
    pytest.param("cat {chat_dir}/manifest.json", "{chat_dir}/manifest.json", id="host-record"),
    pytest.param("cat {home}/auth.yml", "{home}/auth.yml", id="host-token"),
    pytest.param("ls {other}", "{other}", id="the-other-chat"),
    pytest.param("cat {other}/secret.csv", "{other}/secret.csv", id="the-other-chats-file"),
    pytest.param("cd / && ls", "/", id="cd-out-then-list"),
    pytest.param("cd .. && cat manifest.json", "..", id="cd-up-then-read"),
    pytest.param("ls /home/alkera/../../**", "/home/alkera/../../**", id="glob-up"),
    pytest.param("ls ../**/*.json", "../**/*.json", id="relative-glob-up"),
    pytest.param("cat leak", "leak", id="a-planted-link-out"),
    pytest.param("cat /home/alkera/leak", "/home/alkera/leak", id="a-planted-link-absolute"),
    pytest.param("cat sub/../leak", "sub/../leak", id="a-planted-link-via-dot-dot"),
    pytest.param("cat to-db", "to-db", id="a-planted-link-to-the-database-by-host-path"),
    pytest.param("cat to-internal", "to-internal", id="a-planted-link-to-the-database-by-mount"),
    pytest.param("cat $HOME/to-db", f"{HOME}/to-db", id="a-planted-link-by-variable"),
    pytest.param(
        f"python -c \"print(open('{DATA}/agent.db','rb').read(16))\"",
        f"{DATA}/agent.db",
        id="interpreter-naming-the-database",
    ),
    pytest.param(
        f"python3 - <<EOF\nopen('{sb.HARNESS_CONFIG_MOUNT}/global-instructions.md')\nEOF",
        f"{sb.HARNESS_CONFIG_MOUNT}/global-instructions.md",
        id="heredoc-naming-the-instructions",
    ),
    pytest.param("bash -c 'cat /proc/1/environ'", "/proc/1/environ", id="wrapped-proc"),
]


def _spell(text: str, box: _Box) -> str:
    return text.format(chat_dir=box.chat_dir, home=box.home, other=box.other)


@pytest.mark.parametrize(("command", "quoted"), ESCAPES)
def test_a_read_out_of_the_root_is_refused(box: _Box, command: str, quoted: str) -> None:
    verdict = box.fence.judge_shell(_spell(command, box), cwd=Path(HOME), env=box.env)
    assert verdict.outcome == "escape", (command, verdict)
    assert verdict.target == _spell(quoted, box)
    # The way out names the root as the agent spells it, never the host path
    # (the location it quotes is the one the model typed, host path or not).
    explained = box.fence.explain(verdict)
    assert explained.endswith(f"; read inside {HOME} instead.")
    assert str(box.working) not in explained.rsplit("inside ", 1)[1]


@pytest.mark.parametrize(
    ("command", "quoted"),
    [
        pytest.param(f"echo hi > {DATA}/planted", f"{DATA}/planted", id="into-the-session-data"),
        pytest.param(
            f"echo hi > {sb.HARNESS_STATE_MOUNT}/x", f"{sb.HARNESS_STATE_MOUNT}/x", id="agent-state"
        ),
        pytest.param("echo hi > ../manifest.json", "../manifest.json", id="a-record-by-dot-dot"),
        pytest.param("echo hi > /home/x", "/home/x", id="beside-the-root"),
        pytest.param("echo hi > /tmp/x", "/tmp/x", id="the-containers-tmp"),
        pytest.param("echo hi > {other}/x", "{other}/x", id="the-other-chat"),
        pytest.param(
            f"echo hi > {sb.AGENT_MOUNT}/opencode", f"{sb.AGENT_MOUNT}/opencode", id="bin"
        ),
        pytest.param("cp notes.md {chat_dir}/", "{chat_dir}/", id="host-chat-folder"),
    ],
)
def test_a_write_out_of_the_folder_is_refused(box: _Box, command: str, quoted: str) -> None:
    verdict = box.fence.judge_shell(_spell(command, box), cwd=Path(HOME), env=box.env, writing=True)
    assert verdict.outcome == "escape", (command, verdict)
    assert verdict.target == _spell(quoted, box)
    assert verdict.writing is True


@pytest.mark.parametrize(
    ("command", "quoted"),
    [
        pytest.param("echo hi > tool-output/x", "tool-output/x", id="through-a-linked-tool-output"),
        pytest.param(
            f"echo hi > {ENV}/lib/x.py", f"{ENV}/lib/x.py", id="through-a-linked-environment"
        ),
        pytest.param("cp notes.md tool-output/", "tool-output/", id="a-copy-into-the-link"),
    ],
)
def test_a_write_through_a_directory_the_agent_linked_out_is_refused(
    box: _Box, command: str, quoted: str
) -> None:
    """The names are inside the root (``tool-output``, where the daemon keeps
    long results) or the chat's own environment; the agent swapped each for
    a link to a host directory. The fence judges where the name leads."""
    elsewhere = box.root.parent / "etc"
    elsewhere.mkdir()
    (box.working / "tool-output").symlink_to(elsewhere)
    (box.envs / "alkera").rename(box.envs / "real")
    (box.envs / "alkera").symlink_to(elsewhere)
    verdict = box.fence.judge_shell(command, cwd=Path(HOME), env=box.env, writing=True)
    assert verdict.outcome == "escape", (command, verdict)
    assert verdict.target == quoted
    assert verdict.writing is True


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("ls", id="the-root"),
        pytest.param("ls /home/alkera", id="the-root-by-name"),
        pytest.param("cat notes.md", id="a-file-in-the-root"),
        pytest.param("cat /home/alkera/sub/../notes.md", id="dot-dot-that-stays-inside"),
        pytest.param("ls ~", id="tilde"),
        pytest.param("cat $HOME/notes.md", id="home-var"),
        pytest.param("ls /usr/bin", id="a-system-root"),
        pytest.param(f"cat {ENV}/pyvenv.cfg", id="the-environment"),
        pytest.param(f"ls {ENV}/bin", id="the-environments-bin"),
        pytest.param("cat $VIRTUAL_ENV/pyvenv.cfg", id="the-environment-by-variable"),
        pytest.param("echo hi > report.md", id="a-write-in-the-root"),
        pytest.param("echo hi > /home/alkera/sub/report.md", id="a-write-under-the-root"),
        pytest.param(f"echo hi > {ENV}/lib/x.py", id="a-write-into-the-environment"),
        pytest.param("ls sub && cat sub/../notes.md", id="a-directory-in-the-root"),
        pytest.param("cat to-env", id="a-planted-link-into-the-environment"),
        # The container's process table is the sandbox's own.
        pytest.param("sleep 300 & pkill sleep", id="stop-an-own-background-process"),
        pytest.param("ps -eo pid,ppid,comm", id="list-the-sandboxes-processes"),
        pytest.param("cat /proc/self/environ", id="the-commands-own-environment"),
    ],
)
def test_the_root_and_the_environment_stay_open(box: _Box, command: str) -> None:
    verdict = box.fence.judge_shell(command, cwd=Path(HOME), env=box.env)
    assert verdict.outcome == "inside", (command, verdict)


def test_the_process_table_is_the_chats_where_the_sandbox_has_its_own(box: _Box) -> None:
    """The mirror opens the process table to the shell on a gVisor host, where
    ``/proc`` is the container's. The file tools' asks never see it: for them
    it is outside the workspace as before."""
    assert box.fence.sandboxed is True
    assert box.fence.judge_ask(_ask("read", "/proc/self/status"), writing=False).escaped
    assert box.fence.judge_ask(_ask("read", "/proc/1/cmdline"), writing=False).escaped


def test_the_same_names_are_foreign_on_a_box_that_never_mounted_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The aliases are what let the root and the environment pass; the mirror
    on a box that mounts nothing (not root, no runsc) builds a fence with
    none, and judges the same spellings as the foreign paths they are there."""
    plain = _Box(tmp_path, monkeypatch, cap=NOTHING_TO_APPLY, mode="none")
    assert plain.mirror._mount_aliases() == ()
    # And its process table is the box's, so it stays the floor it was.
    assert plain.fence.sandboxed is False
    for command in (
        "ls /home/alkera",
        f"cat {ENV}/pyvenv.cfg",
        "ps -eo pid",
        "cat /proc/self/environ",
    ):
        assert plain.fence.judge_shell(command, cwd=plain.working, env={}).outcome == "escape", (
            command
        )


# ---------------------------------------------------------------------------
# The file tools' asks, through the same judge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "raw", "patterns"),
    [
        pytest.param("read", f"{DATA}/agent.db", None, id="read-the-database"),
        pytest.param(
            "read", f"{sb.HARNESS_CONFIG_MOUNT}/global-instructions.md", None, id="read-config"
        ),
        pytest.param("read", f"{sb.AGENT_MOUNT}/opencode", None, id="read-the-binary"),
        pytest.param("read", "/home/alkera/../manifest.json", None, id="read-a-record"),
        pytest.param("read", "/proc/1/environ", None, id="read-proc"),
        pytest.param("read", "{chat_dir}/.runtime/agent/agent.db", None, id="read-host-runtime"),
        pytest.param("read", "{other}/secret.csv", None, id="read-the-other-chat"),
        pytest.param("glob", sb.HARNESS_MOUNT, ["**"], id="glob-the-harness"),
        pytest.param("glob", "/home/alkera", ["../../**"], id="glob-up-from-the-root"),
        pytest.param("grep", sb.INTERNAL_PREFIX, None, id="grep-the-prefix"),
        pytest.param("edit", f"{DATA}/agent.db", None, id="edit-the-database"),
        pytest.param("write", f"{sb.HARNESS_STATE_MOUNT}/x", None, id="write-the-agent-state"),
        pytest.param("write", "/home/alkera/../manifest.json", None, id="write-a-record"),
        pytest.param("write", "/home/x", None, id="write-beside-the-root"),
    ],
)
def test_a_file_tools_ask_out_of_the_root_is_refused(
    box: _Box, kind: str, raw: str, patterns: list[str] | None
) -> None:
    writing = kind in ("edit", "write")
    verdict = box.fence.judge_ask(_ask(kind, _spell(raw, box), patterns=patterns), writing=writing)
    assert verdict.outcome == "escape", (kind, raw, verdict)
    assert verdict.writing is writing


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        pytest.param("read", "/home/alkera/notes.md", id="read-in-the-root"),
        pytest.param("read", "home/alkera/notes.md", id="read-spelled-from-the-worktree-root"),
        pytest.param("read", f"{ENV}/pyvenv.cfg", id="read-the-environment"),
        pytest.param("glob", "/home/alkera", id="glob-the-root"),
        pytest.param("edit", "/home/alkera/notes.md", id="edit-in-the-root"),
        pytest.param("write", "/home/alkera/sub/report.md", id="write-under-the-root"),
        pytest.param("write", f"{ENV}/lib/site.py", id="write-into-the-environment"),
    ],
)
def test_a_file_tools_ask_inside_the_root_passes(box: _Box, kind: str, raw: str) -> None:
    writing = kind in ("edit", "write")
    assert box.fence.judge_ask(_ask(kind, raw), writing=writing).outcome == "inside"


# ---------------------------------------------------------------------------
# What the fence says back is spelled for the agent
# ---------------------------------------------------------------------------


def test_the_fence_spells_a_host_path_the_way_the_agent_sees_it(box: _Box) -> None:
    assert box.fence.spell(box.working) == HOME
    assert box.fence.spell(box.working / "sub" / "a.csv") == f"{HOME}/sub/a.csv"
    assert box.fence.spell(box.envs / "alkera") == ENV
    assert box.fence.spell(box.runtime / "agent" / "agent.db") == f"{DATA}/agent.db"
    assert box.fence.spell(box.agent_root / "config" / "x") == f"{sb.HARNESS_CONFIG_MOUNT}/x"
    # A host path no mount covers is the host path: the agent cannot reach it
    # under any name, and nothing pretends otherwise.
    assert box.fence.spell(box.chat_dir / "manifest.json") == str(box.chat_dir / "manifest.json")
    assert box.fence.spell(box.home / "auth.yml") == str(box.home / "auth.yml")
    # The inverse of canonical, on both sides.
    assert box.fence.canonical(f"{DATA}/agent.db") == str(box.runtime / "agent" / "agent.db")
    assert box.fence.canonical(box.fence.spell(box.working / "x")) == str(box.working / "x")


# ---------------------------------------------------------------------------
# The fence the tools get is the one handed over at open
# ---------------------------------------------------------------------------


async def _allow(_request: Any) -> str:
    return "allow_once"


async def test_the_fence_handed_over_at_open_already_maps_the_agents_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded chat's first tool call after a wake runs against the fence
    the mirror handed the runtime when it opened the chat: built from the same
    host probe and the same bind table the launch mounts from, so the agent's
    ``/home/alkera`` maps to the working directory from the first call on,
    never only after something else ran."""
    from alkera_cli.harness import PermissionBroker

    box = _Box(tmp_path, monkeypatch)
    seen: dict[str, Any] = {}

    async def open_chat(chat_id: str, **kwargs: Any) -> Any:
        seen.update(kwargs, chat_id=chat_id)
        return object()

    monkeypatch.setattr(box.mirror._runtime, "open_chat", open_chat)
    await box.mirror._open_on(PermissionBroker(_allow, default_timeout_seconds=None), None, "tok")
    fence = seen["path_fence"]
    assert seen["chat_id"] == CHAT_ID
    assert seen["working_dir"] == box.working and seen["sandbox_dir"] == box.working
    assert fence.agent_home == HOME
    assert fence.session.aliases == box.mirror._mount_aliases()
    assert (HOME, box.working) in fence.session.aliases
    assert fence.canonical(f"{HOME}/sub") == str(box.working / "sub")
    assert fence.session.judge_shell("ls /home/alkera/sub", cwd=box.working, env={}).outcome != (
        "escape"
    )
