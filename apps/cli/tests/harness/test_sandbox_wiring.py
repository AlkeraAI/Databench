"""The opencode adapter launches a cloud chat's agent server in the box's
sandbox mode: gVisor (runsc) on a pool box — rooted at the staged rootfs, with a
network of its own that the daemon's tool server and the agent's listen address
are composed for — the per-chat uid + cgroup with no boundary on a
single-tenant box, or — on a bare container with no systemd and no cgroup
delegation — nothing at all, so the agent just starts. A local session is
untouched; a box set to gVisor that cannot run runsc refuses the chat rather
than running it unsandboxed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness.adapter import HarnessStartRefusedError, SessionConfig
from alkera_cli.harness.adapters import opencode_http, opencode_sandbox
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_core import process as core_process
from alkera_core.sandbox_tiers import POOL_FLOOR, TIER_LIMITS

CHAT = "chat_00112233aabbccdd"
AGENT_ARGV = ["/opt/alkera/agent/opencode", "serve", "--hostname", "127.0.0.1", "--port", "0"]
ENV = {"ALKERA_GATEWAY_URL": "https://gw.example", "PATH": "/usr/bin"}
MCP = {
    "alkera": {"type": "remote", "url": "http://127.0.0.1:41234/mcp", "headers": {"A": "b"}},
    "web": {"type": "remote", "url": "http://127.0.0.1:41234/mcp-web", "headers": {"A": "b"}},
}

#: A pool box that can run gVisor: Linux, root, setpriv, runsc, a cgroup, the
#: staged rootfs, the network tools, a resolver, and the interpreter + uv.
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
#: A single-tenant box with the controls but no runsc (mode none, capable).
NONE_CAPABLE = SandboxCapability(
    platform="linux",
    root=True,
    setpriv="/usr/bin/setpriv",
    setfacl="/usr/bin/setfacl",
    runsc=None,
    cgroup="systemd",
    reason="injected: uid + cgroup, no runsc",
    unshare="/usr/bin/unshare",
    mount_ns=True,
    uv="/usr/local/bin/uv",
    python_home="/opt/alkera/python/current",
)
#: A bare RunPod-style container: not root, no cgroup delegation, no systemd.
BARE_CONTAINER = SandboxCapability(
    platform="linux",
    root=False,
    setpriv=None,
    runsc=None,
    cgroup="none",
    reason="injected: unprivileged container, no systemd, no cgroup",
)
#: A RunPod CPU pod as the daemon meets it: root in a container with setpriv and
#: setfacl, no systemd, a read-only cgroup v2 and ``unshare --mount`` refused.
ROOT_BARE = SandboxCapability(
    platform="linux",
    root=True,
    setpriv="/usr/bin/setpriv",
    setfacl="/usr/bin/setfacl",
    runsc=None,
    cgroup="none",
    reason="no writable cgroup v2 and no systemd: chat uid only, no gVisor",
    unshare="/usr/bin/unshare",
    mount_ns=False,
)
#: The same container with more capabilities: a writable cgroup v2 and a
#: private mount namespace, still no systemd.
ROOT_PRIVILEGED = SandboxCapability(
    platform="linux",
    root=True,
    setpriv="/usr/bin/setpriv",
    setfacl="/usr/bin/setfacl",
    runsc=None,
    cgroup="cgroupfs",
    reason="no runsc: uid and cgroupfs cgroup only, mode none",
    unshare="/usr/bin/unshare",
    mount_ns=True,
)


def make_adapter(
    tmp_path: Path,
    *,
    fenced: bool = True,
    working_dir: bool = True,
    harness_native: dict[str, object] | None = None,
    parent_session_id: str | None = None,
    folder: Path | None = None,
) -> OpencodeHttpAdapter:
    chat_dir = tmp_path / ".alkera" / "chats" / CHAT
    config = SessionConfig(
        session_id=CHAT,
        project_dir=tmp_path,
        chat_dir=chat_dir,
        fenced=fenced,
        working_dir=(folder or chat_dir / "sandbox") if working_dir else None,
        harness_native=dict(harness_native or {}),
        parent_session_id=parent_session_id,
    )
    binary = ResolvedOpencodeBinary(
        path=Path("/opt/alkera/agent/opencode"),
        prefix_args=(),
        source="bundled",
        ripgrep_path=Path("/opt/alkera/rg/rg"),
    )
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


@pytest.fixture
def box(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[str]]:
    """Pin the host capability, the box mode and the chat uid; return the log of
    uid lookups so a test can assert none happened."""
    for name in (sb.ENV_MODE, sb.ENV_DEDICATED, sb.ENV_ROOTFS, sb.ENV_NET, sb.ENV_PYTHON):
        monkeypatch.delenv(name, raising=False)
    looked_up: list[str] = []

    def pin(cap: SandboxCapability, *, mode: str) -> list[str]:
        monkeypatch.setattr(opencode_http, "current_capability", lambda: cap)
        monkeypatch.setenv(sb.ENV_MODE, mode)

        def fake_uid(chat_id: str, **_: object) -> int:
            looked_up.append(chat_id)
            return 20031

        monkeypatch.setattr(opencode_sandbox, "ensure_chat_uid", fake_uid)
        return looked_up

    return pin


@pytest.mark.parametrize("mode", ["gvisor", "none"])
def test_a_subagent_childs_plan_runs_as_its_parents_uid_in_its_own_container(
    tmp_path: Path, box: Callable[..., list[str]], mode: str
) -> None:
    """A child shares its parent's working tree, so it runs as the parent's
    identity: one uid for the tree, resolved from the owner session and from
    nowhere else. Its container, slice and runtime trees stay its own, named
    from its own session id."""
    looked_up = box(GVISOR_READY, mode=mode)
    child = make_adapter(tmp_path, parent_session_id="chat_parent0000000000")
    plan = child._sandbox_plan()
    assert plan is not None
    # The tree's uid is the owner session's; the network slot is the child's
    # own, so a child and its parent running at once never ask for one veth.
    assert looked_up == ["chat_parent0000000000", CHAT]
    assert plan.spec.uid == 20031
    assert plan.spec.net_uid == 20031
    assert plan.spec.chat_id == CHAT
    launch = child._sandbox_launch(AGENT_ARGV, ENV)
    if mode == "gvisor":
        assert sb.chat_container(CHAT) in launch.wrap(AGENT_ARGV)
        assert sb.chat_container("chat_parent0000000000") not in launch.wrap(AGENT_ARGV)
    else:
        assert f"--slice={sb.chat_slice(CHAT)}" in launch.prefix
    looked_up.clear()
    own = make_adapter(tmp_path)._sandbox_plan()
    assert own is not None and looked_up == [CHAT], "a session with no parent is its own owner"


def test_a_local_session_is_never_sandboxed_and_never_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode() -> SandboxCapability:
        raise AssertionError("the capability must not be probed for a local session")

    monkeypatch.setattr(opencode_http, "current_capability", explode)
    monkeypatch.setenv(sb.ENV_MODE, "gvisor")
    assert make_adapter(tmp_path, fenced=False)._sandbox_launch(AGENT_ARGV, ENV) is sb.NO_SANDBOX
    unrooted = make_adapter(tmp_path, fenced=False, working_dir=False)
    assert unrooted._sandbox_launch(AGENT_ARGV, ENV) is sb.NO_SANDBOX


@pytest.mark.parametrize("mode", ["gvisor", "none"])
def test_a_bounded_session_without_a_working_directory_is_refused_not_run_unsandboxed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A fenced session takes its sandbox from its binding, whose root is the
    working directory. A fenced session that names none (a child opened
    without its parent's root, a caller that forgot) used to be read as "not
    a cloud chat" and spawned as the daemon, in the daemon's directory, with
    no sandbox at all. It is refused instead, before the host is even
    probed."""

    def explode() -> SandboxCapability:
        raise AssertionError("the capability must not be probed before the refusal")

    monkeypatch.setattr(opencode_http, "current_capability", explode)
    monkeypatch.setenv(sb.ENV_MODE, mode)
    adapter = make_adapter(tmp_path, fenced=True, working_dir=False)
    with pytest.raises(HarnessStartRefusedError, match="no working directory to sandbox"):
        adapter._sandbox_launch(AGENT_ARGV, ENV)


def test_a_bare_container_none_node_just_starts_the_agent_with_no_scaffolding(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    """The production blocker: a RunPod-style container has no systemd, no dbus
    and no cgroup delegation, so a launch that reaches for any of them makes the
    node provision to ready and then refuse every chat. A ``none`` node on such
    a box must apply NOTHING — the agent just starts."""
    looked_up = box(BARE_CONTAINER, mode="none")
    launch = make_adapter(tmp_path)._sandbox_launch(AGENT_ARGV, ENV)
    assert launch is sb.NO_SANDBOX
    assert launch.before == () and launch.after_exit == () and launch.command is None
    assert launch.wrap(AGENT_ARGV) == AGENT_ARGV  # the command, untouched
    # And no scaffolding token appears anywhere in what would be run.
    text = " ".join(launch.wrap(AGENT_ARGV))
    for banned in ("systemd", "systemctl", "bwrap", "runsc", "nft", "setpriv", "unshare"):
        assert banned not in text, banned
    assert looked_up == []  # no uid is created either


def _run_text(launch: sb.SandboxLaunch) -> str:
    """Every executable the launch would run: its before and after steps and
    the command itself."""
    steps = [*launch.before, *launch.after_exit]
    text = " ".join(" ".join(s.argv) for s in steps if isinstance(s, sb.ShellStep))
    return text + " " + " ".join(launch.wrap(AGENT_ARGV))


def test_a_root_container_with_no_cgroup_and_no_mount_namespace_drops_to_the_chat_uid_only(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    """A RunPod CPU pod: the daemon is root with setpriv and setfacl, but the
    box has no systemd, a read-only cgroup and no ``unshare --mount``. The one
    control the host has — the chat uid — is applied; the cgroup and the alias
    are not; nothing the host lacks is named anywhere in what would run."""
    looked_up = box(ROOT_BARE, mode="none")
    launch = make_adapter(tmp_path)._sandbox_launch(AGENT_ARGV, ENV)
    assert launch is not sb.NO_SANDBOX and launch.mode == "none"
    assert looked_up == [CHAT]
    command = launch.wrap(AGENT_ARGV)
    assert "--reuid=20031" in command and "--no-new-privs" in command
    assert command[-len(AGENT_ARGV) :] == AGENT_ARGV
    text = _run_text(launch)
    for absent in ("systemd", "systemctl", "unshare", "runsc", "nft", "cgroup"):
        assert absent not in text, absent
    assert launch.after_exit == () and launch.cgroup_procs is None
    # No alias: HOME is the folder's own host path and the fence gets no alias.
    folder = (tmp_path / ".alkera" / "chats" / CHAT / "sandbox").as_posix()
    assert launch.env["HOME"] == folder and launch.agent_home is None
    # The chat's uid is lent the agent's own trees, which it now opens itself.
    lent = [
        s.argv
        for s in launch.before
        if isinstance(s, sb.ShellStep) and s.argv[:2] == ("setfacl", "-R")
    ]
    assert lent == [("setfacl", "-R", "-m", "u:20031:rX", "/opt/alkera/agent", "/opt/alkera/rg")]
    assert {
        (s.owner.uid, s.owner.gid)
        for s in launch.before
        if isinstance(s, sb.RepairStep) and s.owner is not None
    } == {(20031, 20031)}


def test_a_privileged_container_applies_cgroupfs_and_the_alias_without_systemd(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    """The same container with a writable cgroup v2 and a working ``unshare``:
    the cgroup is made through cgroupfs, the folder is bound at the short path,
    the uid is dropped — and systemd is still never named."""
    box(ROOT_PRIVILEGED, mode="none")
    launch = make_adapter(tmp_path)._sandbox_launch(AGENT_ARGV, ENV)
    command = launch.wrap(AGENT_ARGV)
    text = _run_text(launch)
    assert "systemd" not in text and "systemctl" not in text and "runsc" not in text
    assert "/usr/bin/unshare" in command and "--reuid=20031" in command
    assert launch.cgroup_procs == sb.chat_cgroup_path(CHAT) / "cgroup.procs"
    assert launch.env["HOME"] == "/home/alkera" and launch.agent_home == "/home/alkera"
    assert any(isinstance(s, sb.WriteStep) and s.path.name == "memory.max" for s in launch.before)
    assert ("rmdir", sb.host_path(sb.chat_cgroup_path(CHAT))) in [
        s.argv for s in launch.after_exit if isinstance(s, sb.ShellStep)
    ]


@pytest.mark.parametrize(
    "cap",
    [
        pytest.param(BARE_CONTAINER, id="unprivileged"),
        pytest.param(ROOT_BARE, id="uid-only"),
        pytest.param(ROOT_PRIVILEGED, id="cgroupfs-and-alias"),
        pytest.param(NONE_CAPABLE, id="systemd-and-alias"),
        pytest.param(
            SandboxCapability(
                platform="linux",
                root=True,
                setpriv="/usr/bin/setpriv",
                setfacl="/usr/bin/setfacl",
                runsc=None,
                cgroup="none",
                reason="injected: no cgroup, unshare works",
                unshare="/usr/bin/unshare",
                mount_ns=True,
            ),
            id="alias-without-a-cgroup",
        ),
    ],
)
def test_the_fence_and_the_launch_agree_on_the_agents_home_alias(
    tmp_path: Path, box: Callable[..., list[str]], cap: SandboxCapability
) -> None:
    """The mirror tells the fence the alias from the capability alone
    (``cap.controls and cap.mount_ns``); the launch binds it from the spec. A
    box that aliases for one and not the other would refuse every write to
    ``/home/alkera`` or judge a foreign path as the folder."""
    box(cap, mode="none")
    launch = make_adapter(tmp_path)._sandbox_launch(AGENT_ARGV, ENV)
    fence_alias = sb.agent_home(
        sb.SandboxSettings.from_env(), gvisor=cap.gvisor, mount_alias=cap.controls and cap.mount_ns
    )
    assert launch.agent_home == fence_alias


def test_a_none_node_with_only_cgroupfs_never_touches_systemd(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    """A rootful container with a writable cgroupfs but no systemd applies the
    uid + cgroup through cgroupfs, never systemd-run / systemctl (which would
    fail with 'System has not been booted with systemd')."""
    cap = SandboxCapability(
        platform="linux",
        root=True,
        setpriv="/usr/bin/setpriv",
        setfacl="/usr/bin/setfacl",
        runsc=None,
        cgroup="cgroupfs",
        reason="injected: cgroupfs, no systemd",
    )
    box(cap, mode="none")
    launch = make_adapter(tmp_path)._sandbox_launch(AGENT_ARGV, ENV)
    text = " ".join(s.argv[0] for s in launch.before if isinstance(s, sb.ShellStep))
    text += " ".join(launch.wrap(AGENT_ARGV))
    assert "systemd" not in text and "systemctl" not in text
    assert "runsc" not in text and "bwrap" not in text
    # It still drops to the chat uid — the boundary is gone, the controls stay.
    assert "--reuid=20031" in launch.wrap(AGENT_ARGV)
    # With no mount namespace and no interpreter, HOME is the folder and no
    # environment is named.
    assert launch.env["HOME"] == (tmp_path / ".alkera" / "chats" / CHAT / "sandbox").as_posix()
    assert "VIRTUAL_ENV" not in launch.env and "unshare" not in text


def test_gvisor_launch_runs_the_agent_under_runsc_and_names_the_chats_trees(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    looked_up = box(GVISOR_READY, mode="gvisor")
    adapter = make_adapter(
        tmp_path,
        harness_native={"sandbox_vcpu": 2, "sandbox_memory_mb": 4096, "alkera_mcp": MCP},
    )
    launch = adapter._sandbox_launch(AGENT_ARGV, ENV)
    assert launch.mode == "gvisor"
    assert looked_up == [CHAT]
    command = launch.wrap(AGENT_ARGV)
    assert command[0].endswith("runsc") and "run" in command
    config = next(
        s for s in launch.before if isinstance(s, sb.WriteStep) and s.path.name == "config.json"
    )
    oci = json.loads(config.content)
    chat_dir = tmp_path / ".alkera" / "chats" / CHAT
    folder = (chat_dir / "sandbox").as_posix()
    # Rooted at the staged rootfs the probe found.
    assert oci["root"] == {"path": "/opt/alkera/rootfs/current", "readonly": True}
    rw = {
        m["destination"]: m["source"]
        for m in oci["mounts"]
        if m["type"] == "bind" and "ro" not in m["options"]
    }
    ro = {
        m["destination"]: m["source"]
        for m in oci["mounts"]
        if m["type"] == "bind" and "ro" in m["options"]
    }
    runtime = chat_dir / ".runtime"
    assert rw == {
        "/home/alkera": folder,
        f"{sb.HARNESS_DATA_MOUNT}/agent": (runtime / "agent").as_posix(),
        sb.ENVS_MOUNT: (runtime / "envs").as_posix(),
        sb.HARNESS_STATE_MOUNT: (adapter._agent_config_root / "state").as_posix(),
    }
    config_dir = (adapter._agent_config_root / "config").as_posix()
    assert ro[sb.AGENT_MOUNT] == "/opt/alkera/agent"
    assert ro[sb.RIPGREP_MOUNT] == "/opt/alkera/rg"
    assert ro[sb.HARNESS_CONFIG_MOUNT] == config_dir
    # The chat's records beside the folder are never mounted, nor the overlay,
    # nor the runtime directory whole, nor anything at a host path.
    sources = set(rw.values()) | set(ro.values())
    assert chat_dir.as_posix() not in sources
    assert runtime.as_posix() not in sources
    assert (chat_dir / ".overlay").as_posix() not in sources
    assert tmp_path.as_posix() not in sources
    assert not any(d.startswith(tmp_path.as_posix()) for d in (*rw, *ro))
    assert f"--overlay2=root:dir={(chat_dir / '.overlay').as_posix()}" in command
    # The chat's own limits reach the cgroup steps.
    first = launch.before[0]
    assert isinstance(first, sb.ShellStep)
    assert "MemoryMax=4096M" in first.argv and "CPUQuota=200%" in first.argv
    # The agent's environment is baked into the OCI config, with the default
    # environment and rg on the PATH where the container sees them, and the
    # agent binary itself spelled where the container sees it.
    env = dict(item.split("=", 1) for item in oci["process"]["env"])
    assert env["ALKERA_GATEWAY_URL"] == "https://gw.example"
    default_env = f"{sb.ENVS_MOUNT}/alkera"
    assert env["VIRTUAL_ENV"] == default_env
    assert env["PATH"].startswith(f"{default_env}/bin:{sb.RIPGREP_MOUNT}:")
    umask = list(sb.umask_prefix())
    assert oci["process"]["args"][: len(umask)] == umask
    assert oci["process"]["args"][len(umask)] == f"{sb.AGENT_MOUNT}/opencode"
    # The daemon's tool-server port is granted to this chat's host end.
    grants = [
        s.argv
        for s in launch.before
        if isinstance(s, sb.ShellStep) and s.argv[0] == "/usr/sbin/nft"
    ]
    assert len(grants) == 1 and grants[0][-1].endswith(". 41234 }")
    # The container's resolv.conf names the host's real upstream.
    resolv = next(
        s for s in launch.before if isinstance(s, sb.WriteStep) and s.path.name == "resolv.conf"
    )
    assert resolv.content == "nameserver 172.31.0.2\n"


@pytest.mark.parametrize(
    ("tier", "vcpu", "memory_mb"),
    [pytest.param(name, *TIER_LIMITS[name], id=name) for name in ("free", "plus", "pro")],
)
def test_the_rows_tier_figures_bound_the_gvisor_launch(
    tmp_path: Path, box: Callable[..., list[str]], tier: str, vcpu: int, memory_mb: int
) -> None:
    """Each plan tier's figures, as the chat row carries them into the harness
    bag, are what the launch puts on the chat's slice and in the OCI limits."""
    box(GVISOR_READY, mode="gvisor")
    adapter = make_adapter(
        tmp_path,
        harness_native={"sandbox_vcpu": vcpu, "sandbox_memory_mb": memory_mb, "alkera_mcp": MCP},
    )
    launch = adapter._sandbox_launch(AGENT_ARGV, ENV)
    first = launch.before[0]
    assert isinstance(first, sb.ShellStep)
    assert f"MemoryMax={memory_mb}M" in first.argv
    assert "MemorySwapMax=0" in first.argv
    assert f"CPUQuota={vcpu * 100}%" in first.argv
    config = next(
        s for s in launch.before if isinstance(s, sb.WriteStep) and s.path.name == "config.json"
    )
    oci = json.loads(config.content)
    assert oci["linux"]["resources"]["memory"]["limit"] == memory_mb * 1024 * 1024
    assert oci["linux"]["resources"]["cpu"]["quota"] == vcpu * 100_000
    assert launch.memory_mb == memory_mb
    assert launch.memory_events is not None and launch.memory_events.name == "memory.events"


@pytest.mark.parametrize(
    "harness_native",
    [
        pytest.param({}, id="no-figures"),
        pytest.param({"sandbox_vcpu": None, "sandbox_memory_mb": None}, id="nulls"),
        pytest.param({"sandbox_vcpu": 0, "sandbox_memory_mb": -5}, id="nonsense"),
        pytest.param({"sandbox_vcpu": "4", "sandbox_memory_mb": "8192"}, id="mistyped"),
    ],
)
def test_a_row_with_no_figures_is_bounded_at_the_pool_floor_never_unlimited(
    tmp_path: Path, box: Callable[..., list[str]], harness_native: dict[str, object]
) -> None:
    """A pool chat whose row names nothing — the defect: every pool chat read
    ``null`` — still gets a hard bound, the smallest tier's, so one tenant can
    never take the shared box."""
    box(GVISOR_READY, mode="gvisor")
    adapter = make_adapter(tmp_path, harness_native={**harness_native, "alkera_mcp": MCP})
    launch = adapter._sandbox_launch(AGENT_ARGV, ENV)
    floor_vcpu, floor_memory = POOL_FLOOR
    first = launch.before[0]
    assert isinstance(first, sb.ShellStep)
    assert f"MemoryMax={floor_memory}M" in first.argv
    assert f"CPUQuota={floor_vcpu * 100}%" in first.argv
    config = next(
        s for s in launch.before if isinstance(s, sb.WriteStep) and s.path.name == "config.json"
    )
    oci = json.loads(config.content)
    assert oci["linux"]["resources"]["memory"]["limit"] == floor_memory * 1024 * 1024
    assert launch.memory_mb == floor_memory


@pytest.mark.parametrize(
    ("capability", "mode"),
    [
        pytest.param(GVISOR_READY, "gvisor", id="gvisor"),
        pytest.param(NONE_CAPABLE, "none", id="none"),
    ],
)
def test_the_chats_agent_root_is_closed_to_other_chats_like_its_records(
    tmp_path: Path,
    box: Callable[..., list[str]],
    capability: SandboxCapability,
    mode: str,
) -> None:
    """Two directories hold what no other chat may reach: the chat's records
    beside its folder, and its agent root under ``ALKERA_HOME/harness`` beside
    every other chat's — the config tree in it is readable by whoever can
    reach it, so the root must be closed by the launch's own steps. Left to
    the daemon's umask, a box whose daemon started at 022 let one chat read
    another chat's agent definitions and instructions (the root probe found
    exactly that)."""
    box(capability, mode=mode)
    adapter = make_adapter(tmp_path, harness_native={"alkera_mcp": MCP})
    plan = adapter._sandbox_plan()
    assert plan is not None
    chat_dir = tmp_path / ".alkera" / "chats" / CHAT
    assert plan.spec.private_dirs == (chat_dir, adapter._agent_config_root)
    assert adapter._agent_config_root != chat_dir
    assert (adapter._agent_config_root / "config",) == plan.spec.config_trees
    # ...and the container sees it at the fixed internal path, read-only.
    config_bind = next(b for b in plan.spec.binds if b.kind == "config")
    assert (config_bind.destination, config_bind.readonly) == (sb.HARNESS_CONFIG_MOUNT, True)
    launch = adapter._sandbox_launch(AGENT_ARGV, ENV)
    closed = next(
        s.argv
        for s in launch.before
        if isinstance(s, sb.ShellStep) and s.argv[:2] == ("chmod", "go-rwx")
    )
    assert set(closed[2:]) == {chat_dir.as_posix(), adapter._agent_config_root.as_posix()}


def test_under_gvisor_the_tool_server_is_named_at_the_host_end_and_the_agent_binds_all(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    """The container's loopback is not the daemon's: the agent's config points
    the tool server at the host end of the veth pair, the agent server binds
    every address it has (so the daemon reaches it over the pair), and the
    address the daemon dials is the container's end, whatever the agent
    reported binding."""
    box(GVISOR_READY, mode="gvisor")
    adapter = make_adapter(tmp_path, harness_native={"alkera_mcp": MCP})
    adapter._state.sandbox_plan = adapter._sandbox_plan()
    adapter._state.sandbox_planned = True
    plan = adapter._state.sandbox_plan
    assert plan is not None and plan.network is not None
    net = plan.network
    mcp = adapter._opencode_config()["mcp"]
    assert mcp["alkera"]["url"] == f"http://{net.host_ip}:41234/mcp"
    assert mcp["web"]["url"] == f"http://{net.host_ip}:41234/mcp-web"
    assert mcp["alkera"]["headers"] == {"A": "b"}  # nothing else about the entry changes
    content = adapter._build_env("pw").secrets["ALKERA_CONFIG_CONTENT"]
    assert f"http://{net.host_ip}:41234/mcp" in content
    assert "127.0.0.1:41234" not in content
    assert adapter._agent_address("http://127.0.0.1:38111") == f"http://{net.container_ip}:38111"
    assert adapter._agent_address("http://0.0.0.0:38111") == f"http://{net.container_ip}:38111"


def test_under_gvisor_the_agents_own_paths_are_spelled_where_the_container_sees_them(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    """Everything the daemon tells the agent server about its own files names
    the container's path, never the host's: its data, config, state and cache
    roots, the file it announces its port in, and the instructions file it is
    handed. The host path of each is not in the environment at all, so an
    agent that prints its environment learns nothing of the box's layout."""
    box(GVISOR_READY, mode="gvisor")
    adapter = make_adapter(
        tmp_path, harness_native={"alkera_mcp": MCP, "global_instructions": "# be brief"}
    )
    adapter._state.sandbox_plan = adapter._sandbox_plan()
    adapter._state.sandbox_planned = True
    launch = adapter._build_env("pw")
    env = launch.env
    assert env["XDG_DATA_HOME"] == sb.HARNESS_DATA_MOUNT
    assert env["XDG_CONFIG_HOME"] == env["ALKERA_TEST_HOME"] == sb.HARNESS_CONFIG_MOUNT
    assert env["XDG_STATE_HOME"] == env["XDG_CACHE_HOME"] == sb.HARNESS_STATE_MOUNT
    assert env["ALKERA_LISTEN_FILE"] == f"{sb.HARNESS_STATE_MOUNT}/listen-url"
    assert env["ALKERA_SECRETS_FILE"] == f"{sb.HARNESS_STATE_MOUNT}/launch.json"
    config = json.loads(launch.secrets["ALKERA_CONFIG_CONTENT"])
    assert config["instructions"] == [f"{sb.HARNESS_CONFIG_MOUNT}/global-instructions.md"]
    # The file itself is written where the host keeps the config tree, which
    # the container sees at that path.
    assert (adapter._agent_config_root / "config" / "global-instructions.md").read_text() == (
        "# be brief"
    )
    assert adapter._listen_file == adapter._agent_config_root / "state" / "listen-url"
    host_roots = (tmp_path.as_posix(), adapter._agent_config_root.as_posix())
    for name, value in env.items():
        if name == "ALKERA_SHELL_ENV_RESTORE":
            continue  # the daemon's reverse diff; the sandbox drops it from the container
        assert not any(root in value for root in host_roots), (name, value)


def test_on_a_none_node_the_agents_own_paths_are_the_host_paths(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    """With no container there is nothing to respell: the agent opens its
    data, config and state where the host keeps them, spelled for the Linux
    host a plan is always composed for."""
    box(NONE_CAPABLE, mode="none")
    adapter = make_adapter(tmp_path, harness_native={"alkera_mcp": MCP})
    adapter._state.sandbox_plan = adapter._sandbox_plan()
    adapter._state.sandbox_planned = True
    env = adapter._build_env("pw").env
    assert env["XDG_DATA_HOME"] == sb.host_path(adapter._harness_dir)
    assert env["XDG_CONFIG_HOME"] == sb.host_path(adapter._agent_config_root / "config")
    assert env["ALKERA_LISTEN_FILE"] == sb.host_path(
        adapter._agent_config_root / "state" / "listen-url"
    )


@pytest.mark.parametrize(
    "working_dir",
    [
        pytest.param(Path(".alkera") / "chats" / CHAT / "sandbox", id="a-chat-on-its-own"),
        pytest.param(
            Path(".alkera") / "workspaces" / "8870a53d-efa2-424f-8cc3-86003628be76" / "files",
            id="a-workspace-member",
        ),
    ],
)
def test_under_gvisor_opencode_runs_in_the_sandboxs_home_never_the_host_path(
    tmp_path: Path, box: Callable[..., list[str]], working_dir: Path
) -> None:
    """The container mounts the chat's folder at ``/home/alkera`` and nowhere
    else, so the directory opencode is told it runs in (what a relative path
    in a write resolves against, and what its prompt names as the working
    directory) is that home. The host path names nothing in the container: a
    write resolved against it fails to make its directory."""
    box(GVISOR_READY, mode="gvisor")
    adapter = make_adapter(tmp_path, folder=tmp_path / working_dir)
    adapter._state.sandbox_plan = adapter._sandbox_plan()
    adapter._state.sandbox_planned = True
    assert (
        opencode_sandbox.agent_directory(adapter._state.sandbox_plan, adapter._config)
        == "/home/alkera"
    )


def test_on_a_none_node_with_a_mount_namespace_opencode_runs_in_the_aliased_home(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    """A ``none`` box that can alias the folder at the sandbox's home names
    that home, as the root-folder brief does, so the model reads one path."""
    box(NONE_CAPABLE, mode="none")
    adapter = make_adapter(tmp_path)
    adapter._state.sandbox_plan = adapter._sandbox_plan()
    adapter._state.sandbox_planned = True
    assert (
        opencode_sandbox.agent_directory(adapter._state.sandbox_plan, adapter._config)
        == "/home/alkera"
    )


def test_on_a_none_node_without_an_alias_opencode_runs_in_the_folders_host_path(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    """With no container and no mount namespace the folder exists only where
    the host keeps it, spelled for the Linux host."""
    box(ROOT_BARE, mode="none")
    adapter = make_adapter(tmp_path)
    adapter._state.sandbox_plan = adapter._sandbox_plan()
    adapter._state.sandbox_planned = True
    assert adapter._state.sandbox_plan is not None
    folder = tmp_path / ".alkera" / "chats" / CHAT / "sandbox"
    assert opencode_sandbox.agent_directory(
        adapter._state.sandbox_plan, adapter._config
    ) == sb.host_path(folder)


def test_an_unsandboxed_session_runs_opencode_in_its_working_directory(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    box(BARE_CONTAINER, mode="none")
    adapter = make_adapter(tmp_path)
    adapter._state.sandbox_plan = adapter._sandbox_plan()
    adapter._state.sandbox_planned = True
    assert adapter._state.sandbox_plan is None
    assert opencode_sandbox.agent_directory(adapter._state.sandbox_plan, adapter._config) == str(
        tmp_path / ".alkera" / "chats" / CHAT / "sandbox"
    )


def test_on_a_none_node_the_tool_server_and_the_agent_stay_on_the_daemons_loopback(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    box(NONE_CAPABLE, mode="none")
    adapter = make_adapter(tmp_path, harness_native={"alkera_mcp": MCP})
    adapter._state.sandbox_plan = adapter._sandbox_plan()
    adapter._state.sandbox_planned = True
    assert adapter._state.sandbox_plan is not None
    assert adapter._state.sandbox_plan.network is None
    assert adapter._opencode_config()["mcp"]["alkera"]["url"] == "http://127.0.0.1:41234/mcp"
    assert adapter._agent_address("http://127.0.0.1:38111") == "http://127.0.0.1:38111"


def test_a_none_node_with_the_controls_applies_uid_cgroup_the_alias_and_the_env_but_no_runsc(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    box(NONE_CAPABLE, mode="none")
    launch = make_adapter(tmp_path)._sandbox_launch(AGENT_ARGV, ENV)
    assert launch.mode == "none"
    command = launch.wrap(AGENT_ARGV)
    assert "--reuid=20031" in command and command[0] == "systemd-run"
    assert "runsc" not in " ".join(command)
    # The box can make a private mount namespace, so the folder is bound at
    # the short path and HOME is that path.
    assert "/usr/bin/unshare" in command and launch.env["HOME"] == "/home/alkera"
    assert launch.agent_home == "/home/alkera"
    # The default environment is made and put first on the PATH.
    default_env = (
        tmp_path / ".alkera" / "chats" / CHAT / ".runtime" / "envs" / "alkera"
    ).as_posix()
    assert launch.path_prefix == (f"{default_env}/bin",)
    # Made by the chat's uid, never by the daemon as itself.
    venv = next(s for s in launch.before if isinstance(s, sb.ShellStep) and "venv" in s.argv)
    assert "/usr/local/bin/uv" in venv.argv
    assert venv.argv[0] == "/usr/bin/setpriv" and "--reuid=20031" in venv.argv
    first = launch.before[0]
    assert isinstance(first, sb.ShellStep)
    assert f"MemoryMax={sb.DEFAULT_POOL_MEMORY_MB}M" in first.argv


def test_a_gvisor_box_that_cannot_run_runsc_refuses_the_chat_fail_closed(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    """Fail closed: a box configured for gVisor whose host has no working runsc
    must NEVER silently run the agent unsandboxed."""
    looked_up = box(NONE_CAPABLE, mode="gvisor")  # gvisor asked for, runsc absent
    with pytest.raises(HarnessStartRefusedError, match="gVisor") as refused:
        make_adapter(tmp_path)._sandbox_launch(AGENT_ARGV, ENV)
    assert looked_up == []  # refused before a uid is ever created
    # The refusal carries the box's account: its mode and what the probe found.
    assert "sandbox mode=gvisor" in str(refused.value)
    assert "host: injected: uid + cgroup, no runsc" in str(refused.value)


def test_a_gvisor_box_without_a_staged_rootfs_refuses_the_chat_fail_closed(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    from dataclasses import replace

    looked_up = box(replace(GVISOR_READY, rootfs=None), mode="gvisor")
    with pytest.raises(HarnessStartRefusedError, match="gVisor"):
        make_adapter(tmp_path)._sandbox_launch(AGENT_ARGV, ENV)
    assert looked_up == []


def test_a_gvisor_box_whose_host_names_no_reachable_resolver_refuses_the_chat(
    tmp_path: Path, box: Callable[..., list[str]]
) -> None:
    from dataclasses import replace

    box(replace(GVISOR_READY, resolvers=()), mode="gvisor")
    with pytest.raises(HarnessStartRefusedError, match="resolver"):
        make_adapter(tmp_path)._sandbox_launch(AGENT_ARGV, ENV)


def test_a_uid_the_box_cannot_make_refuses_the_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, box: Callable[..., list[str]]
) -> None:
    box(GVISOR_READY, mode="gvisor")

    def no_user(chat_id: str, **_: object) -> int:
        raise sb.SandboxRefusedError("could not create the chat's user")

    monkeypatch.setattr(opencode_sandbox, "ensure_chat_uid", no_user)
    with pytest.raises(HarnessStartRefusedError, match="could not create the chat's user"):
        make_adapter(tmp_path)._sandbox_launch(AGENT_ARGV, ENV)


class _ExitedProc:
    """A child that died before it reported a listen URL."""

    pid = 4242
    returncode = 255
    stdout = None
    stderr = None

    def terminate(self) -> None:
        return None


@pytest.fixture
def linux_spawn(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Spawn as the box does — the Linux death-signal launcher in front — but
    hand back a child that exited at once; return the commands spawned."""
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(core_process, "pdeathsig_launcher", lambda: "/usr/bin/setpriv")
    spawned: list[list[str]] = []

    async def fake_exec(*command: str, **_: object) -> _ExitedProc:
        spawned.append(list(command))
        return _ExitedProc()

    monkeypatch.setattr(opencode_http.asyncio, "create_subprocess_exec", fake_exec)
    # The fake start stands in for the OS, which is what finds the program:
    # the agent binary these cases name is a box path, not a file here.
    monkeypatch.setattr(core_process, "require_program", lambda spec, command: None)
    monkeypatch.setattr(opencode_http, "run_steps", lambda steps, **_: None)
    return spawned


SECRET_ENV = {**ENV, "OPENCODE_SERVER_PASSWORD": "s3cret-pw-value"}


@pytest.mark.asyncio
async def test_a_start_failure_names_the_probe_verdict_and_the_launch_shape(
    tmp_path: Path, box: Callable[..., list[str]], linux_spawn: list[list[str]]
) -> None:
    """What the operator reads on the console when the agent dies before it
    reports its listen URL: the mode, the cgroup driver, the uid, the alias,
    the probe's own line about the host, and the wrapper argv up to the agent
    binary — never the environment, never the agent's own argv."""
    box(ROOT_PRIVILEGED, mode="none")
    adapter = make_adapter(tmp_path)
    proc = await adapter._spawn_opencode(dict(SECRET_ENV))
    with pytest.raises(opencode_http.HarnessStartError) as failed:
        await adapter._await_listen_url(proc)  # type: ignore[arg-type]
    message = str(failed.value)
    assert "exit code 255" in message
    assert "sandbox mode=none, cgroup=cgroupfs, uid=20031, alias=/home/alkera" in message
    assert "host: no runsc: uid and cgroupfs cgroup only, mode none" in message
    folder = (tmp_path / ".alkera" / "chats" / CHAT / "sandbox").as_posix()
    wrapper = message.split("wrapper: ", 1)[1].split(").\n", 1)[0]
    assert wrapper.startswith("/usr/bin/setpriv --pdeathsig KILL -- /usr/bin/unshare --mount")
    assert f"alkera-home {folder} /home/alkera /usr/bin/setpriv --reuid=20031" in wrapper
    assert wrapper.endswith("--pdeathsig KILL -- " + " ".join(sb.umask_prefix()))
    # The spawned command is what the message describes, up to the agent binary.
    spawned = linux_spawn[0]
    assert spawned[-len(AGENT_ARGV) :] == AGENT_ARGV
    assert " ".join(spawned[: -len(AGENT_ARGV)]) == wrapper
    assert "s3cret-pw-value" not in message and "serve --hostname" not in message


def _windows_composed_adapter(tmp_path: Path, *, fenced: bool) -> OpencodeHttpAdapter:
    """An adapter whose binary path joins with backslashes, as a Windows
    composer's ``Path`` would spell the same Linux location."""
    from pathlib import PureWindowsPath

    chat_dir = tmp_path / ".alkera" / "chats" / CHAT
    config = SessionConfig(
        session_id=CHAT,
        project_dir=tmp_path,
        chat_dir=chat_dir,
        fenced=fenced,
        working_dir=chat_dir / "sandbox" if fenced else None,
        harness_native={},
    )
    binary = ResolvedOpencodeBinary(
        path=PureWindowsPath("/opt/alkera/agent/opencode"),  # type: ignore[arg-type]
        prefix_args=(),
        source="bundled",
        ripgrep_path=PureWindowsPath("/opt/alkera/rg/rg"),  # type: ignore[arg-type]
    )
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


@pytest.mark.asyncio
async def test_the_agent_command_under_a_sandbox_is_spelled_for_the_linux_box_whatever_composes_it(
    tmp_path: Path, box: Callable[..., list[str]], linux_spawn: list[list[str]]
) -> None:
    """The launch runs on a Linux box, so its command is ``/opt/...`` even when
    the composer's own ``Path`` would write ``\\opt\\...``; with no sandbox the
    binary is this host's own file and keeps the host's spelling."""
    box(ROOT_BARE, mode="none")
    sandboxed = _windows_composed_adapter(tmp_path, fenced=True)
    await sandboxed._spawn_opencode(dict(ENV))
    spawned = linux_spawn[-1]
    tail = spawned[len(spawned) - len(AGENT_ARGV) :]
    assert tail[0] == "/opt/alkera/agent/opencode" and tail[1] == "serve"
    assert not any("\\opt" in word for word in spawned)
    assert "--reuid=20031" in spawned  # the uid drop is in front of it
    local = _windows_composed_adapter(tmp_path, fenced=False)
    await local._spawn_opencode(dict(ENV))
    assert linux_spawn[-1][-len(AGENT_ARGV)] == str(local._binary.path)  # native, untouched


@pytest.mark.asyncio
async def test_an_unsandboxed_start_failure_still_says_what_the_box_found(
    tmp_path: Path, box: Callable[..., list[str]], linux_spawn: list[list[str]]
) -> None:
    box(BARE_CONTAINER, mode="none")
    adapter = make_adapter(tmp_path)
    proc = await adapter._spawn_opencode(dict(SECRET_ENV))
    with pytest.raises(opencode_http.HarnessStartError) as failed:
        await adapter._await_listen_url(proc)  # type: ignore[arg-type]
    message = str(failed.value)
    assert "sandbox mode=none; host: injected: unprivileged container" in message
    assert "wrapper: /usr/bin/setpriv --pdeathsig KILL --).\n" in message
    assert "cgroup=" not in message and "uid=" not in message  # no spec: none was made


@pytest.mark.asyncio
async def test_a_failing_before_step_is_refused_with_the_boxs_account(
    tmp_path: Path, box: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step the launch depends on (the chown, the ACL, the cgroup file) that
    fails on this box is a refusal the reader can act on — the step, the
    verdict and the launch — never a retried opaque start error, and the
    agent is never spawned on top of it."""
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(core_process, "pdeathsig_launcher", lambda: "/usr/bin/setpriv")
    box(ROOT_BARE, mode="none")

    def failing(steps: Iterable[sb.Step], **_: object) -> None:
        raise sb.SandboxRefusedError("sandbox step failed: setfacl -m u:20031:x /opt (exit 1)")

    async def never(*command: str, **_: object) -> _ExitedProc:
        raise AssertionError(f"the agent must not be spawned after a failed step: {command}")

    monkeypatch.setattr(opencode_http, "run_steps", failing)
    monkeypatch.setattr(opencode_http.asyncio, "create_subprocess_exec", never)
    adapter = make_adapter(tmp_path)
    with pytest.raises(HarnessStartRefusedError) as refused:
        await adapter._spawn_opencode(dict(SECRET_ENV))
    message = str(refused.value)
    assert message.startswith("sandbox step failed: setfacl -m u:20031:x /opt (exit 1) (")
    assert "sandbox mode=none, cgroup=none, uid=20031, alias=none" in message
    assert "host: no writable cgroup v2 and no systemd: chat uid only, no gVisor" in message
    assert "wrapper: /usr/bin/setpriv --pdeathsig KILL -- /usr/bin/setpriv --reuid=20031" in message
    assert "s3cret-pw-value" not in message


@pytest.mark.asyncio
async def test_after_exit_steps_run_once_and_the_launch_is_forgotten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[tuple[sb.Step, ...]] = []
    monkeypatch.setattr(opencode_http, "run_steps", lambda steps, **_: ran.append(tuple(steps)))
    adapter = make_adapter(tmp_path)
    stop = sb.ShellStep(("systemctl", "stop", "alkera-chat-x.slice"), check=False)
    adapter._state.sandbox = sb.SandboxLaunch(mode="gvisor", after_exit=(stop,))
    await adapter._sandbox_after_exit()
    await adapter._sandbox_after_exit()
    assert ran == [(stop,)]
    assert adapter._state.sandbox is None


@pytest.mark.asyncio
async def test_a_failing_cleanup_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(steps: Iterable[sb.Step], **_: object) -> None:
        raise OSError("cgroup busy")

    monkeypatch.setattr(opencode_http, "run_steps", boom)
    adapter = make_adapter(tmp_path)
    adapter._state.sandbox = sb.SandboxLaunch(
        mode="none", after_exit=(sb.ShellStep(("rmdir", "/x"), check=False),)
    )
    await adapter._sandbox_after_exit()
    assert adapter._state.sandbox is None
