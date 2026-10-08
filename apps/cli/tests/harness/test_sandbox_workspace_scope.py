"""Two chats of one workspace, sandboxed on one box.

Each member of a workspace runs with the workspace's shared tree as its root,
and every process that touches that tree (either member's agent server,
either member's commands, the daemon's own writes into it) acts as ONE tree
identity: the workspace's uid. In the ``per_chat`` topology each member still
has a container of its own, so its network slot comes from its own user and
two members never ask for one veth. A chat on its own is launched exactly as
it always was.

Driven through the real adapter launch with the host capability and the uid
lookup pinned, so the OCI config, the steps and the commands asserted on are
the ones a box would run.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alkera_cli.cloud import chat_fs
from alkera_cli.files.chat_fs import TreeIdentity
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness.adapter import HarnessStartRefusedError, SessionConfig
from alkera_cli.harness.adapters import opencode_http, opencode_sandbox
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.harness.sandbox_layout import chat_container
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.harness.sandbox_scope import (
    SCOPE_KEY,
    TOPOLOGY_ENV,
    forget_scope,
    register_scope,
    scope_for,
    scope_of,
    topology_from_env,
)
from alkera_cli.harness.session_launches import reset_launches_for_tests
from alkera_cli.plugins.plugin_base import bash_exec

WS_KEY = "ws:8a7c1f2e-0000-4000-8000-000000000001"
CHAT_A = "chat_aaaaaaaaaaaaaaaa"
CHAT_B = "chat_bbbbbbbbbbbbbbbb"
AGENT_ARGV = ["/opt/alkera/agent/opencode", "serve", "--hostname", "0.0.0.0", "--port", "0"]
ENV = {"ALKERA_GATEWAY_URL": "https://gw.example", "PATH": "/usr/bin"}
#: The uid each user the box makes is given, by the name it is made for.
UIDS = {WS_KEY: 20100, CHAT_A: 20101, CHAT_B: 20102, "solo": 20103}

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


@pytest.fixture(autouse=True)
def _scopes() -> Iterator[None]:
    yield
    for session in (CHAT_A, CHAT_B, "solo"):
        forget_scope(session)


@pytest.fixture
def box(monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[str]]:
    for name in (sb.ENV_MODE, sb.ENV_DEDICATED, sb.ENV_ROOTFS, sb.ENV_NET, sb.ENV_PYTHON):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(sb.ENV_MODE, "gvisor")
    monkeypatch.setenv(TOPOLOGY_ENV, "per_chat")
    monkeypatch.setattr(opencode_http, "current_capability", lambda: GVISOR_READY)
    monkeypatch.setattr(bash_exec, "current_capability", lambda: GVISOR_READY)
    made: list[str] = []

    def fake_uid(name: str, **_: object) -> int:
        made.append(name)
        return UIDS[name]

    monkeypatch.setattr(opencode_sandbox, "ensure_chat_uid", fake_uid)
    monkeypatch.setattr(bash_exec, "ensure_chat_uid", fake_uid)
    reset_launches_for_tests()
    return lambda: made


def _member(tmp_path: Path, session: str, *, workspace: str | None = WS_KEY) -> OpencodeHttpAdapter:
    chat_dir = tmp_path / ".alkera" / "chats" / session
    shared = tmp_path / ".alkera" / "workspaces" / "ws" / "files"
    config = SessionConfig(
        session_id=session,
        project_dir=tmp_path,
        chat_dir=chat_dir,
        fenced=True,
        working_dir=shared if workspace else chat_dir / "scratch",
        harness_native={SCOPE_KEY: workspace} if workspace else {},
    )
    binary = ResolvedOpencodeBinary(
        path=Path("/opt/alkera/agent/opencode"),
        prefix_args=(),
        source="bundled",
        ripgrep_path=Path("/opt/alkera/rg/rg"),
    )
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


def _oci(launch: sb.SandboxLaunch) -> dict[str, object]:
    config = next(
        s for s in launch.before if isinstance(s, sb.WriteStep) and s.path.name == "config.json"
    )
    loaded: dict[str, object] = json.loads(config.content)
    return loaded


def test_two_members_run_as_the_workspaces_uid_in_containers_of_their_own(
    tmp_path: Path, box: Callable[[], list[str]]
) -> None:
    box()
    a = _member(tmp_path, CHAT_A)._sandbox_launch(AGENT_ARGV, ENV)
    b = _member(tmp_path, CHAT_B)._sandbox_launch(AGENT_ARGV, ENV)
    shared = (tmp_path / ".alkera" / "workspaces" / "ws" / "files").as_posix()

    for launch, session in ((a, CHAT_A), (b, CHAT_B)):
        oci = _oci(launch)
        process = oci["process"]
        assert isinstance(process, dict)
        assert process["user"] == {"uid": UIDS[WS_KEY], "gid": UIDS[WS_KEY]}
        mounts = oci["mounts"]
        assert isinstance(mounts, list)
        home = next(m for m in mounts if m["destination"] == "/home/alkera")
        assert home["source"] == shared
        assert chat_container(session) in launch.wrap(AGENT_ARGV)
        # The shared tree is put right for the workspace's uid on every spawn
        # (walked only when its root is not already the uid's, setgid).
        owners = {
            (s.owner.uid, s.owner.gid)
            for s in launch.before
            if isinstance(s, sb.RepairStep)
            and s.when_wrong
            and s.owner is not None
            and sb.host_path(s.tree) == shared
        }
        assert owners == {(UIDS[WS_KEY], UIDS[WS_KEY])}

    # Two containers, two network slots: each from its own chat's user.
    nets = [
        s.argv
        for launch in (a, b)
        for s in launch.before
        if isinstance(s, sb.ShellStep) and s.argv[:3] == ("/usr/sbin/ip", "link", "add")
    ]
    assert {argv[3] for argv in nets} == {f"vc{UIDS[CHAT_A]}", f"vc{UIDS[CHAT_B]}"}


def test_a_members_commands_run_as_the_workspace_in_the_members_container(
    tmp_path: Path, box: Callable[[], list[str]]
) -> None:
    box()
    _member(tmp_path, CHAT_A)._sandbox_launch(AGENT_ARGV, ENV)
    launch = bash_exec.session_sandbox(
        CHAT_A, folder=tmp_path / ".alkera" / "workspaces" / "ws" / "files", fenced=True
    )
    argv = launch.wrap(["/bin/sh", "-c", "true"])
    assert f"--user={UIDS[WS_KEY]}:{UIDS[WS_KEY]}" in argv
    assert chat_container(CHAT_A) in argv


def test_a_chat_on_its_own_is_launched_exactly_as_before(
    tmp_path: Path, box: Callable[[], list[str]]
) -> None:
    made = box()
    launch = _member(tmp_path, "solo", workspace=None)._sandbox_launch(AGENT_ARGV, ENV)
    process = _oci(launch)["process"]
    assert isinstance(process, dict)
    assert process["user"] == {"uid": UIDS["solo"], "gid": UIDS["solo"]}
    assert made == ["solo"]
    assert scope_of("solo").member is False


def test_the_shared_topology_is_refused_until_this_build_can_run_it(
    tmp_path: Path, box: Callable[[], list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    box()
    monkeypatch.setenv(TOPOLOGY_ENV, "shared")
    with pytest.raises(HarnessStartRefusedError, match=TOPOLOGY_ENV):
        _member(tmp_path, CHAT_A)._sandbox_launch(AGENT_ARGV, ENV)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("", "per_chat", id="unset"),
        pytest.param("SHARED", "shared", id="shared-any-case"),
        pytest.param("per_chat", "per_chat", id="per-chat"),
        pytest.param("one-big-box", "per_chat", id="an-unknown-word-is-the-default"),
    ],
)
def test_the_topology_setting(raw: str, expected: str) -> None:
    assert topology_from_env({TOPOLOGY_ENV: raw}) == expected


def test_a_bag_naming_no_workspace_is_the_sessions_own_scope() -> None:
    assert scope_for("s", {}).member is False
    assert scope_for("s", {SCOPE_KEY: ""}).member is False
    assert scope_for("s", {SCOPE_KEY: "s"}).member is False
    shared = scope_for("s", {SCOPE_KEY: WS_KEY}, topology="shared")
    assert (shared.tree, shared.container, shared.shared) == (WS_KEY, WS_KEY, True)
    own = scope_for("s", {SCOPE_KEY: WS_KEY}, topology="per_chat")
    assert (own.tree, own.container, own.shared) == (WS_KEY, "s", False)


def test_the_daemons_writes_into_a_members_tree_are_the_workspaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat_fs owns what the daemon writes as the tree's identity: for a
    member, the workspace's user, never the chat's own."""
    from alkera_cli.harness import sandbox_probe, sandbox_uid

    users = {WS_KEY: 20100, CHAT_A: 20101}
    monkeypatch.setattr(sandbox_probe, "current_capability", lambda: GVISOR_READY)
    monkeypatch.setattr(sandbox_uid, "ensure_chat_uid", lambda name, **_: users[name])
    assert chat_fs.sandbox_identity(CHAT_A) == TreeIdentity(uid=20101, gid=20101)
    register_scope(scope_for(CHAT_A, {SCOPE_KEY: WS_KEY}, topology="per_chat"))
    assert chat_fs.sandbox_identity(CHAT_A) == TreeIdentity(uid=20100, gid=20100)
    assert chat_fs.sandbox_identity(WS_KEY) == TreeIdentity(uid=20100, gid=20100)
