"""Two members of one workspace on a ``none``-mode box.

With no container around either member, the uid a process runs as is the only
thing between it and another member's files on the host. So on a ``none`` box
each member runs as its own chat's uid: its agent's state, its environment and
every process it starts are that uid's alone, and the workspace's shared tree
stays the workspace's, reached through the workspace's group (setgid, so what
one member makes stays the group's, under a umask that keeps it the group's
to read and write). Under gVisor the per-container binds keep members apart
and every member runs as the workspace's uid, as before.

Driven through the real adapter launch and the real command sandbox with the
host capability and the uid lookup pinned.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters import opencode_http, opencode_sandbox
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.harness.sandbox_env import session_envs_dir
from alkera_cli.harness.sandbox_layout import workspace_dir_name
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.harness.sandbox_scope import SCOPE_KEY, TOPOLOGY_ENV, forget_scope
from alkera_cli.harness.session_launches import reset_launches_for_tests
from alkera_cli.plugins.plugin_base import bash_exec

WS_KEY = "ws:8a7c1f2e-0000-4000-8000-000000000001"
CHAT_A = "chat_aaaaaaaaaaaaaaaa"
CHAT_B = "chat_bbbbbbbbbbbbbbbb"
AGENT_ARGV = ["/opt/alkera/agent/opencode", "serve", "--hostname", "127.0.0.1", "--port", "0"]
ENV = {"ALKERA_GATEWAY_URL": "https://gw.example", "PATH": "/usr/bin"}
UIDS = {WS_KEY: 20100, CHAT_A: 20101, CHAT_B: 20102, "solo": 20103}
WS = UIDS[WS_KEY]

NONE_READY = SandboxCapability(
    platform="linux",
    root=True,
    setpriv="/usr/bin/setpriv",
    setfacl="/usr/bin/setfacl",
    runsc=None,
    cgroup="systemd",
    reason="injected: none mode",
    unshare="/usr/bin/unshare",
    mount_ns=False,
    uv="/usr/local/bin/uv",
    python_home="/opt/alkera/python/current",
)
GVISOR_READY = replace(
    NONE_READY,
    runsc="/usr/bin/runsc",
    rootfs="/opt/alkera/rootfs/current",
    ip="/usr/sbin/ip",
    nft="/usr/sbin/nft",
    resolvers=("172.31.0.2",),
)


@pytest.fixture(autouse=True)
def _scopes() -> Iterator[None]:
    yield
    for session in (CHAT_A, CHAT_B, "solo"):
        forget_scope(session)


@pytest.fixture
def box(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    def set_mode(mode: str) -> None:
        for name in (sb.ENV_MODE, sb.ENV_DEDICATED, sb.ENV_ROOTFS, sb.ENV_NET, sb.ENV_PYTHON):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(sb.ENV_MODE, mode)
        monkeypatch.setenv(TOPOLOGY_ENV, "per_chat")
        cap = NONE_READY if mode == "none" else GVISOR_READY
        monkeypatch.setattr(opencode_http, "current_capability", lambda: cap)
        monkeypatch.setattr(bash_exec, "current_capability", lambda: cap)
        monkeypatch.setattr(opencode_sandbox, "ensure_chat_uid", lambda n, **_: UIDS[n])
        monkeypatch.setattr(bash_exec, "ensure_chat_uid", lambda n, **_: UIDS[n])
        reset_launches_for_tests()

    return set_mode


def _shared(tmp_path: Path) -> Path:
    return tmp_path / ".alkera" / "workspaces" / "ws" / "files"


def _adapter(tmp_path: Path, session: str, *, member: bool = True) -> OpencodeHttpAdapter:
    chat_dir = tmp_path / ".alkera" / "chats" / session
    config = SessionConfig(
        session_id=session,
        project_dir=tmp_path,
        chat_dir=chat_dir,
        fenced=True,
        working_dir=_shared(tmp_path) if member else chat_dir / "scratch",
        harness_native={SCOPE_KEY: WS_KEY} if member else {},
    )
    binary = ResolvedOpencodeBinary(
        path=Path("/opt/alkera/agent/opencode"),
        prefix_args=(),
        source="bundled",
        ripgrep_path=Path("/opt/alkera/rg/rg"),
    )
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


def _owners(launch: sb.SandboxLaunch) -> dict[str, str]:
    """Each tree a launch hands over, by the owner it is handed to: the
    launch's own trees on every launch, the workspace's shared trees whenever
    their root is not right."""
    return {
        sb.host_path(step.tree): f"{step.owner.uid}:{step.owner.gid}"
        for step in launch.before
        if isinstance(step, sb.RepairStep) and step.owner is not None
    }


def _drop(argv: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """The uid drop's own flags in a wrapped argv."""
    start = next(i for i, a in enumerate(argv) if a.endswith("setpriv"))
    return tuple(argv[start : argv.index("--", start)])


def test_on_a_none_box_each_member_runs_as_its_own_uid_and_shares_only_the_tree(
    tmp_path: Path, box: Callable[[str], None]
) -> None:
    box("none")
    a = _adapter(tmp_path, CHAT_A)._sandbox_launch(AGENT_ARGV, ENV)
    b = _adapter(tmp_path, CHAT_B)._sandbox_launch(AGENT_ARGV, ENV)
    shared = _shared(tmp_path).as_posix()
    for launch, session, other in ((a, CHAT_A, CHAT_B), (b, CHAT_B, CHAT_A)):
        mine = UIDS[session]
        drop = _drop(launch.wrap(AGENT_ARGV))
        assert f"--reuid={mine}" in drop and f"--regid={mine}" in drop
        # The workspace's group and no other: the shared tree, nothing more.
        assert f"--groups={WS}" in drop and "--clear-groups" not in drop
        owners = _owners(launch)
        assert owners.pop(shared) == f"{WS}:{WS}"
        assert owners, "the member's own trees are handed over too"
        assert set(owners.values()) == {f"{mine}:{mine}"}
        # The member's own trees are its chat's, never the other member's.
        assert all(f"/{session}" in tree for tree in owners)
        assert not any(f"/{other}" in tree for tree in owners)
        # What one member makes in the tree stays the group's to share.
        assert launch.umask == 0o007
        setgid = [
            s
            for s in launch.before
            if isinstance(s, sb.RepairStep)
            and s.owner is None
            and not s.files
            and sb.host_path(s.tree) == shared
        ]
        assert setgid, "the shared tree's directories are setgid"
        # The environment steps keep no group: the environment is the
        # member's, never the workspace's (only the folder is shared here).
        env_drops = [
            s.argv
            for s in launch.before
            if isinstance(s, sb.ShellStep) and s.argv[0].endswith("setpriv")
        ]
        assert env_drops
        for argv in env_drops:
            assert f"--reuid={mine}" in argv and "--clear-groups" in argv
            assert tuple(argv[len(_drop(argv)) + 1 :][:4]) == sb.umask_prefix(
                umask=sb.SHARED_TREE_UMASK
            )
        envs = session_envs_dir(session, tmp_path / ".alkera" / "chats" / session)
        assert workspace_dir_name(WS_KEY) not in envs.parts


def test_on_a_none_box_a_members_commands_run_as_the_member(
    tmp_path: Path, box: Callable[[str], None]
) -> None:
    box("none")
    _adapter(tmp_path, CHAT_A)._sandbox_launch(AGENT_ARGV, ENV)
    launch = bash_exec.session_sandbox(CHAT_A, folder=_shared(tmp_path), fenced=True)
    drop = _drop(launch.wrap(["/bin/sh", "-c", "true"]))
    assert f"--reuid={UIDS[CHAT_A]}" in drop and f"--groups={WS}" in drop
    # Every command hands the shared tree back to the workspace, not the member.
    assert _owners(launch) == {_shared(tmp_path).as_posix(): f"{WS}:{WS}"}
    assert launch.umask == 0o007


def test_under_gvisor_every_member_still_runs_as_the_workspace(
    tmp_path: Path, box: Callable[[str], None]
) -> None:
    box("gvisor")
    launch = _adapter(tmp_path, CHAT_A)._sandbox_launch(AGENT_ARGV, ENV)
    assert set(_owners(launch).values()) == {f"{WS}:{WS}"}
    # Here the members share the workspace's environments (every member's
    # container and the kernel sandbox bind the one directory), repaired as
    # the workspace's like the folder.
    envs = session_envs_dir(CHAT_A, tmp_path / ".alkera" / "chats" / CHAT_A)
    assert envs.name == workspace_dir_name(WS_KEY)
    assert _owners(launch)[sb.host_path(envs)] == f"{WS}:{WS}"
    assert launch.umask == sb.RUNSC_UMASK


def test_a_chat_on_its_own_on_a_none_box_is_launched_as_before(
    tmp_path: Path, box: Callable[[str], None]
) -> None:
    box("none")
    launch = _adapter(tmp_path, "solo", member=False)._sandbox_launch(AGENT_ARGV, ENV)
    drop = _drop(launch.wrap(AGENT_ARGV))
    assert f"--reuid={UIDS['solo']}" in drop and "--clear-groups" in drop
    assert not any(a.startswith("--groups") for a in drop)
    assert set(_owners(launch).values()) == {f"{UIDS['solo']}:{UIDS['solo']}"}
    assert launch.umask is None
