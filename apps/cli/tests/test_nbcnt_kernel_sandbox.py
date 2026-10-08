"""The per-workspace kernel sandbox: what it binds, how it is bounded, where a
notebook may run, and its lifecycle (started by the first kernel, stopped on
put-away and when idle, restarted after it died, its OOM count carried)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _nbcnt_fakes import Rig, make_rig
from alkera_cli.files.chat_fs import TreeIdentity
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness.sandbox_kinds import workspace_sandbox_kinds
from alkera_cli.harness.sandbox_layout import ENVS_MOUNT
from alkera_cli.notebooks import kernel_sandbox as ks


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return make_rig(tmp_path, monkeypatch)


KIND_CONTAINER = "ws:ws1:kernel"


def compose(rig: Rig) -> sb.SandboxLaunch:
    return rig.sandbox().plan_launch()


def oci(launch: sb.SandboxLaunch) -> dict[str, object]:
    for step in launch.before:
        if isinstance(step, sb.WriteStep) and step.path.name == "config.json":
            loaded: dict[str, object] = json.loads(step.content)
            return loaded
    raise AssertionError("no OCI config written")


def shell(steps: tuple[sb.Step, ...]) -> list[tuple[str, ...]]:
    return [s.argv for s in steps if isinstance(s, sb.ShellStep)]


# --- composition, through the workspace sandbox seam ------------------------------------------


def test_the_kernel_kind_is_registered_with_the_workspace_sandbox_seam(rig: Rig) -> None:
    rig.sandbox()
    assert ks.KERNEL_KIND in workspace_sandbox_kinds()
    ks.register_kernel_kind()  # idempotent
    assert workspace_sandbox_kinds().count(ks.KERNEL_KIND) == 1


def test_the_sandbox_binds_exactly_the_tree_the_environments_the_platform_and_its_runtime(
    rig: Rig,
) -> None:
    mounts = oci(compose(rig))["mounts"]
    assert isinstance(mounts, list)
    binds = {m["destination"]: m for m in mounts if m["type"] == "bind"}
    etc = {d for d in binds if d.startswith("/etc/")}
    assert etc == {"/etc/passwd", "/etc/group", "/etc/hosts", "/etc/hostname", "/etc/resolv.conf"}
    trees = {d: (m["source"], "ro" in m["options"]) for d, m in binds.items() if d not in etc}
    config = rig.config
    assert trees == {
        # The files tree where the agents see it, read-write.
        "/home/alkera": (sb.host_path(config.folder), False),
        "/opt/alkera/envs": (sb.host_path(config.envs_dir), False),
        # The platform mount, read-only: boot.py and the kernel runtime.
        ks.KERNEL_MOUNT: (sb.host_path(config.platform_mount), True),
        # The runtime directory, the kernels' sockets and data.
        ks.RUN_MOUNT: (sb.host_path(config.runtime_dir), False),
    }
    assert ENVS_MOUNT == "/opt/alkera/envs"


def test_pid_one_is_a_reaper_running_as_the_tree_with_no_capability(rig: Rig) -> None:
    process = oci(compose(rig))["process"]
    assert isinstance(process, dict)
    assert process["args"] == [*sb.umask_prefix(), *ks.reaper_argv(rig.cap.python_home or "")]
    assert process["user"] == {"uid": 20001, "gid": 20001}  # the workspace's tree identity
    assert all(not caps for caps in process["capabilities"].values())
    assert process["noNewPrivileges"] is True
    # No secret of any agent: the process environment is the sandbox's own.
    names = {item.split("=", 1)[0] for item in process["env"]}
    assert names == {"HOME", "PWD", "PATH", "LANG", "TMPDIR"}


def test_the_reaper_outlives_having_no_child_and_ends_on_term() -> None:
    import signal
    import subprocess
    import sys
    import time

    proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", ks.REAPER_SOURCE])
    try:
        time.sleep(0.3)
        assert proc.poll() is None  # PID 1 with nothing to reap waits; it never exits
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == -signal.SIGTERM


def test_the_sandbox_has_its_own_bounded_cgroup_and_never_memory_high(rig: Rig) -> None:
    launch = compose(rig)
    writes = {s.path.name: s.content for s in launch.before if isinstance(s, sb.WriteStep)}
    assert writes["memory.max"] == str(1536 * 1024 * 1024)
    assert writes["memory.swap.max"] == "0"
    assert writes["memory.oom.group"] == "1"
    assert writes["pids.max"] == str(sb.TASKS_MAX)
    assert "memory.high" not in writes
    cgroup = rig.config.trees.cgroup_root / "alkera.slice" / f"chat-{sb.chat_slug(KIND_CONTAINER)}"
    assert launch.memory_events == cgroup / "memory.events"
    linux = oci(launch)["linux"]
    assert isinstance(linux, dict)
    assert linux["cgroupsPath"] == f"/alkera.slice/chat-{sb.chat_slug(KIND_CONTAINER)}"


def test_runsc_run_opens_host_sockets_on_its_own_network_and_the_resolved_rootfs(rig: Rig) -> None:
    launch = compose(rig)
    command = list(launch.command or ())
    run = command.index("run")
    assert "--host-uds=open" in command[:run]
    assert "--network=sandbox" in command[:run]
    # A memory overlay: a writable root (below) over a read-only rootfs.
    assert "--overlay2=root:memory" in command[:run]
    assert not any(a.startswith("--overlay2=root:dir=") for a in command)
    assert command[-1] == rig.config.container == sb.chat_container(KIND_CONTAINER)
    root = oci(launch)["root"]
    # Its root is writable into its own overlay (system installs); the staged
    # rootfs under it is shared and never written.
    assert root == {"path": sb.host_path(rig.root / "rootfs" / "abc123"), "readonly": False}
    # Its own network namespace and veth (its own slot, not the tree's), and
    # no port granted on the host: the kernels speak a Unix socket.
    argvs = shell(launch.before)
    assert ("ip", "netns", "add", sb.chat_netns(KIND_CONTAINER)) in argvs
    assert launch.network is not None
    assert launch.network.host_if == f"vc{rig.uids[KIND_CONTAINER]}"
    assert not any(a[0] == "nft" for a in argvs)


def test_an_agents_container_never_opens_host_sockets() -> None:
    from alkera_cli.harness.sandbox_layout import chat_binds

    spec = sb.SandboxSpec(
        chat_id="c1",
        folder=Path("/w/f"),
        uid=20001,
        vcpu=1,
        memory_mb=512,
        home="/home/alkera",
        mode="gvisor",
        binds=chat_binds(runtime_dir=Path("/w/r"), agent_config_root=Path("/w/c")),
        bundle=Path("/w/b"),
    )
    assert "--host-uds=open" not in sb.runsc_run_argv(spec, Path("/w/b"))


def test_the_shared_trees_are_put_right_and_the_runtime_is_left_as_it_is(rig: Rig) -> None:
    before = compose(rig).before
    argvs = shell(before)
    repairs = [s for s in before if isinstance(s, sb.RepairStep)]
    kernel = TreeIdentity(20001, 20001)
    assert repairs == [
        sb.RepairStep(Path(sb.host_path(rig.config.folder)), kernel, when_wrong=True),
        sb.RepairStep(Path(sb.host_path(rig.config.envs_dir)), kernel, when_wrong=True),
    ]  # and no agent trees to walk
    run = sb.host_path(rig.config.runtime_dir)
    # Nothing the launch runs re-owns, re-modes or empties the runtime
    # directory: each kernel's directory there is its own.
    assert not any(run in a for a in argvs if a[0] in {"chown", "chmod", "find", "rm"})
    steps = shell(ks.runtime_steps(rig.config.runtime_dir, 20001))
    parts = (run, f"{run}/sock", f"{run}/kernels")
    assert steps == [
        ("mkdir", "-p", *parts),
        ("chown", "0:20001", *parts),
        ("chmod", "0750", *parts),
    ]


def test_teardown_removes_the_container_network_and_cgroup(rig: Rig) -> None:
    launch = compose(rig)
    argvs = shell(launch.after_exit)
    assert argvs[0][:2] == (str(rig.runsc), f"--root={sb.host_path(rig.config.trees.runsc_root)}")
    assert argvs[0][-1] == rig.config.container
    assert ("ip", "netns", "delete", sb.chat_netns(KIND_CONTAINER)) in argvs
    assert launch.memory_events is not None
    assert ("rmdir", sb.host_path(launch.memory_events.parent)) in argvs
    # The root overlay lives in memory; there is no overlay directory to remove.
    assert not any(a[:2] == ("rm", "-rf") and a[-1].endswith("/overlay") for a in argvs)


def test_a_box_that_is_not_gvisor_starts_no_kernel_sandbox(rig: Rig) -> None:
    from dataclasses import replace

    rig.settings = replace(rig.settings, mode="none")
    with pytest.raises(sb.SandboxRefusedError, match="only in a gVisor sandbox"):
        rig.sandbox().plan_launch()


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        pytest.param((), [], id="no-capability-unless-named"),
        pytest.param(("CAP_CHOWN",), ["--cap=CAP_CHOWN"], id="named-capabilities-only"),
    ],
)
def test_exec_starts_from_an_empty_environment_as_the_named_user(
    rig: Rig, caps: tuple[str, ...], expected: list[str]
) -> None:
    target = rig.sandbox().target
    argv = ks.exec_argv(target, (20100, 20001), ("id",), env={"A": "b"}, caps=caps, cwd="/x")
    root = f"--root={sb.host_path(rig.config.trees.runsc_root)}"
    assert argv[:4] == [str(rig.runsc), root, "exec", "--user=20100:20001"]
    assert [a for a in argv if a.startswith("--cap")] == expected
    at = argv.index(rig.config.container)
    assert argv[at + 1 :] == [sb.CONTAINER_ENV, "-i", "A=b", "id"]
    assert "--cwd=/x" in argv[:at]


# --- the rootfs and placement -------------------------------------------------------------


@pytest.fixture
def trees(tmp_path: Path) -> dict[str, Path]:
    ws = tmp_path / "ws1" / "files"
    chat = tmp_path / "chat1" / "sandbox"
    for d in (ws / "nb", chat, tmp_path / "elsewhere"):
        d.mkdir(parents=True)
    for f in (ws / "nb" / "a.alknb.py", chat / "b.alknb.py", tmp_path / "elsewhere" / "c.alknb.py"):
        f.write_text("")
    (ws / "nb" / "escape.alknb.py").symlink_to(tmp_path / "elsewhere" / "c.alknb.py")
    (ws / "nb" / "to-chat.alknb.py").symlink_to(chat / "b.alknb.py")
    return {"ws:ws1": ws, "chat1": chat}


@pytest.mark.parametrize(
    ("notebook", "expected"),
    [
        pytest.param("ws1/files/nb/a.alknb.py", "ws:ws1", id="in-a-workspace"),
        pytest.param("chat1/sandbox/b.alknb.py", "chat1", id="private-chat-folder"),
        pytest.param("ws1/files/nb/to-chat.alknb.py", "chat1", id="link-placed-where-it-is"),
    ],
)
def test_a_notebook_runs_only_in_the_sandbox_of_the_tree_that_holds_it(
    tmp_path: Path, trees: dict[str, Path], notebook: str, expected: str
) -> None:
    assert ks.place_notebook(tmp_path / notebook, trees) == expected


@pytest.mark.parametrize(
    "notebook",
    [
        pytest.param("elsewhere/c.alknb.py", id="outside-every-tree"),
        pytest.param("ws1/files/nb/escape.alknb.py", id="link-out-of-the-tree"),
        pytest.param("ws1/files/nb/missing.alknb.py", id="not-there"),
        pytest.param("ws1/files", id="the-tree-itself"),
    ],
)
def test_anywhere_else_the_run_is_refused(
    tmp_path: Path, trees: dict[str, Path], notebook: str
) -> None:
    with pytest.raises(ks.PlacementRefusedError, match="Open in a workspace to run"):
        ks.place_notebook(tmp_path / notebook, trees)


@pytest.mark.parametrize(
    "kernel_id",
    [
        pytest.param("../x", id="parent"),
        pytest.param("a/b", id="slash"),
        pytest.param("", id="empty"),
        pytest.param("k" * 65, id="too-long"),
    ],
)
def test_a_kernel_id_that_cannot_name_a_file_is_refused(kernel_id: str) -> None:
    with pytest.raises(ValueError, match="refusing kernel id"):
        ks.checked_kernel_id(kernel_id)


# --- lifecycle ------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_first_claim_starts_the_sandbox_and_each_kernel_gets_its_own_uid(rig: Rig) -> None:
    sandbox = rig.sandbox(stop_grace=0.2)
    try:
        assert not sandbox.running()
        a = sandbox.claim("krn_a")
        b = sandbox.claim("krn_b")
        assert sandbox.running()
        assert a.uid != b.uid and a.uid != 20001 and b.uid != 20001
        assert a.gid == b.gid == 20001  # the workspace's files group
        assert sandbox.claim("krn_a") == a  # one kernel, one uid
        # Ready was asked of runsc exec true, and every uid's leftovers were
        # killed before the uid was handed out.
        calls = rig.runsc_calls()
        assert any(c[-1] == "true" for c in calls)
        kills = [c for c in calls if c[-4:] == ["kill", "-KILL", "--", "-1"]]
        assert {c[2] for c in kills} == {f"--user={a.uid}:20001", f"--user={b.uid}:20001"}
    finally:
        sandbox.stop()


@pytest.mark.parametrize("mode", ["gvisor", "none"])
def test_kernels_agents_and_members_share_the_one_workspace_group(rig: Rig, mode: str) -> None:
    """A kernel's and the build uid's primary group is the group the
    workspace's agents and members write its tree through: the tree identity
    the agents run as under gVisor, the one supplementary group a member runs
    with on a ``none`` box. Each kernel still runs as a uid of its own."""
    from alkera_cli.harness.sandbox_uid import member_identity

    sandbox = rig.sandbox()
    tree = rig.ensure_uid(rig.config.workspace)
    uid, share = member_identity(tree, "chat_member", member=True, mode=mode, ensure=rig.ensure_uid)
    group = tree if share is None else share
    kernels = sandbox.slot_identities()
    assert {k.gid for k in kernels} == {sandbox.build_identity().gid} == {group}
    assert len({k.uid for k in kernels}) == len(kernels)
    assert all(k.uid not in (tree, uid) for k in kernels)


def test_a_full_sandbox_refuses_and_a_released_slot_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path, monkeypatch, max_kernels=2)
    sandbox = rig.sandbox(stop_grace=0.2)
    try:
        first = sandbox.claim("krn_1")
        sandbox.claim("krn_2")
        with pytest.raises(sb.SandboxRefusedError, match="already runs 2 kernels"):
            sandbox.claim("krn_3")
        sandbox.release("krn_1")
        assert sandbox.claim("krn_3").uid == first.uid
    finally:
        sandbox.stop()


def test_it_stops_ten_minutes_after_its_last_kernel_and_not_before(rig: Rig) -> None:
    clock = Clock()
    sandbox = rig.sandbox(stop_grace=0.2, clock=clock)
    try:
        sandbox.claim("krn_a")
        clock.now += 3600
        assert not sandbox.stop_if_idle()  # a kernel still holds it
        sandbox.release("krn_a")
        clock.now += ks.IDLE_STOP_SECONDS - 1
        assert not sandbox.stop_if_idle()
        clock.now += 1
        assert sandbox.stop_if_idle()
        assert not sandbox.running() and sandbox.cgroup_dir() is None
    finally:
        sandbox.stop()


def test_a_sandbox_that_never_answers_is_refused_and_taken_down(rig: Rig) -> None:
    (rig.state / "not-ready").write_text("")
    clock = Clock()

    def tick(_: float) -> None:
        clock.now += 1

    sandbox = rig.sandbox(stop_grace=0.2, clock=clock, sleep=tick, ready_timeout=5)
    with pytest.raises(sb.SandboxRefusedError, match="did not start"):
        sandbox.ensure_running()
    assert not sandbox.running()
    assert any(a[:2] == ("ip", "netns") and a[2] == "delete" for a in rig.runner.recorded)


def test_a_sandbox_that_died_is_taken_down_and_started_again(rig: Rig) -> None:
    sandbox = rig.sandbox(stop_grace=0.2)
    try:
        sandbox.ensure_running()
        first = sandbox._running
        assert first is not None
        first.proc.kill()  # the host's OOM group kill took it
        first.proc.wait()
        assert not sandbox.running()
        sandbox.ensure_running()
        assert sandbox.running() and sandbox._running is not first
    finally:
        sandbox.stop()


def write_events(cgroup: Path, oom_kill: int, group_kill: int) -> None:
    cgroup.mkdir(parents=True, exist_ok=True)
    (cgroup / "memory.events").write_text(
        f"low 0\nhigh 0\nmax 12\noom 1\noom_kill {oom_kill}\noom_group_kill {group_kill}\n"
    )


def test_oom_kills_are_counted_across_restarts_and_never_go_down(rig: Rig) -> None:
    sandbox = rig.sandbox(stop_grace=0.2)
    try:
        assert sandbox.oom_events() == 0
        sandbox.ensure_running()
        cgroup = sandbox.cgroup_dir()
        assert cgroup is not None
        write_events(cgroup, oom_kill=3, group_kill=1)
        assert sandbox.oom_events() == 4
        sandbox.kill_all()  # the guard's last resort: the sandbox is down
        assert sandbox.cgroup_dir() is None
        assert sandbox.oom_events() == 4  # the gone cgroup's count is kept
        sandbox.ensure_running()
        # The next start makes its cgroup afresh at the same path.
        write_events(cgroup, oom_kill=0, group_kill=0)
        assert sandbox.oom_events() == 4
        write_events(cgroup, oom_kill=1, group_kill=1)
        assert sandbox.oom_events() == 6  # an OOM in the new start is an increment
    finally:
        sandbox.stop()


def test_kill_all_writes_cgroup_kill_on_the_sandbox(rig: Rig) -> None:
    sandbox = rig.sandbox(stop_grace=0.2)
    sandbox.ensure_running()
    cgroup = sandbox.cgroup_dir()
    assert cgroup is not None
    sandbox.kill_all()
    assert (cgroup / "cgroup.kill").read_text() == "1"
    assert not sandbox.running()
    sandbox.kill_all()  # nothing to kill: no error


def test_paths_are_spelled_as_the_sandbox_sees_them_or_not_at_all(rig: Rig) -> None:
    sandbox = rig.sandbox()
    config = rig.config
    assert sandbox.container_path(config.folder / "nb" / "x.py") == "/home/alkera/nb/x.py"
    assert sandbox.container_path(config.envs_dir / "alkera") == "/opt/alkera/envs/alkera"
    assert sandbox.container_path(config.platform_mount / "boot.py") == f"{ks.KERNEL_MOUNT}/boot.py"
    run = config.runtime_dir / "sock" / "krn_a" / "k.sock"
    assert sandbox.container_path(run) == f"{ks.RUN_MOUNT}/sock/krn_a/k.sock"
    assert sandbox.container_path(rig.root / "elsewhere") is None


def test_every_bind_lands_on_a_mountpoint_the_rootfs_already_has(rig: Rig) -> None:
    """runsc makes a missing mountpoint inside the shared rootfs: under an org
    worker that rootfs is read-only and the start fails, and as root the path
    shows in every sandbox after. So each bind of the kind lands on the home
    or on a mountpoint the box's prerequisites make, in both copies of them."""
    import re

    root = Path(__file__).resolve().parents[3]
    made: list[set[str]] = []
    for script in (
        root / "ops" / "box" / "sandbox-prereqs.sh",
        root / "packages" / "api-core" / "alkera_core" / "compute" / "sandbox_prereqs.sh",
    ):
        text = script.read_text()
        block = text[
            text.index("for mountpoint in") : text.index("; do", text.index("for mountpoint in"))
        ]
        made.append(set(re.findall(r"/opt/alkera/[a-z/]+", block)))
    assert made[0] == made[1]
    mounts = oci(compose(rig))["mounts"]
    assert isinstance(mounts, list)
    binds = {
        m["destination"]
        for m in mounts
        if m["type"] == "bind" and not m["destination"].startswith("/etc/")
    }
    assert binds - {"/home/alkera"} <= made[0]


# -- a folder whose lease is in doubt -------------------------------------------------


def test_a_fenced_sandbox_is_frozen_and_starts_no_kernel_until_confirmed(rig: Rig) -> None:
    """Its kernels write the folder no more while another box may take it; a
    confirmed lease thaws them where they stopped."""
    sandbox = rig.sandbox(stop_grace=0.2)
    try:
        first = sandbox.claim("krn_a")
        cgroup = sandbox.cgroup_dir()
        assert cgroup is not None
        sandbox.fence()
        assert (cgroup / "cgroup.freeze").read_text() == "1"
        with pytest.raises(ks.FolderFencedError):
            sandbox.claim("krn_b")
        with pytest.raises(ks.FolderFencedError):
            sandbox.claim("krn_a")
        sandbox.unfence()
        assert (cgroup / "cgroup.freeze").read_text() == "0"
        assert sandbox.claim("krn_a") == first
        assert sandbox.claim("krn_b").uid != first.uid
    finally:
        sandbox.stop()


def test_a_fence_before_the_sandbox_starts_still_refuses_its_first_kernel(rig: Rig) -> None:
    sandbox = rig.sandbox(stop_grace=0.2)
    sandbox.fence()
    with pytest.raises(ks.FolderFencedError):
        sandbox.claim("krn_a")
    assert not sandbox.running()


def test_a_fenced_sandbox_is_ended_through_its_cgroup_never_thawed(rig: Rig) -> None:
    sandbox = rig.sandbox(stop_grace=0.2)
    sandbox.claim("krn_a")
    cgroup = sandbox.cgroup_dir()
    assert cgroup is not None
    sandbox.fence()
    sandbox.stop()
    assert (cgroup / "cgroup.kill").read_text() == "1"
    assert (cgroup / "cgroup.freeze").read_text() == "1"
    assert not sandbox.running()
