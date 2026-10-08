"""The capability probe reports what the host can do: whether it can run gVisor
(runsc, a staged rootfs, the network tools), whether the per-chat uid + cgroup
controls can be applied, whether a private mount namespace can be made, what
the default Python environment can be made from, and which resolvers a
container can be handed. The daemon fails closed on it — a box configured for
gVisor with no runsc, no rootfs or no network tools refuses every chat rather
than running it unsandboxed."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness import sandbox_probe as sp

TOOLS = {
    "setpriv": "/usr/bin/setpriv",
    "setfacl": "/usr/bin/setfacl",
    "runsc": "/usr/bin/runsc",
    "systemd-run": "/usr/bin/systemd-run",
    "ip": "/usr/sbin/ip",
    "nft": "/usr/sbin/nft",
    "unshare": "/usr/bin/unshare",
    "uv": "/usr/local/bin/uv",
}
ALL_TOOLS = tuple(TOOLS)
ROOTFS = sb.DEFAULT_ROOTFS
PYTHON = sb.DEFAULT_PYTHON_HOME
#: A host with everything: the rootfs staged (stamp + a shell), the managed
#: interpreter, systemd's cgroup files.
STAGED = frozenset(
    {
        "/usr",
        "/lib",
        "/bin",
        f"{ROOTFS}/{sb.ROOTFS_STAMP}",
        f"{ROOTFS}/bin/sh",
        f"{PYTHON}/bin/python3",
    }
)
RESOLV = {
    "/run/systemd/resolve/resolv.conf": "nameserver 172.31.0.2\nsearch ec2.internal\n",
    "/etc/resolv.conf": "nameserver 127.0.0.53\noptions edns0 trust-ad\n",
}


def which_of(*names: str) -> Callable[[str], str | None]:
    return lambda name: TOOLS.get(name) if name in names else None


def run_of(failing: frozenset[str] = frozenset()) -> Callable[[Sequence[str]], int]:
    """A runner whose verdict is by the command's first word: systemd-run for the
    cgroup probe, runsc for the ``runsc --version`` check, unshare for the mount
    namespace check."""

    def run(argv: Sequence[str]) -> int:
        return 1 if argv[0].rsplit("/", 1)[-1] in failing else 0

    return run


def linux_probe(
    *,
    which: Callable[[str], str | None] | None = None,
    run: Callable[[Sequence[str]], int] | None = None,
    platform: str = "linux",
    euid: int = 0,
    exists: Callable[[str], bool] | None = None,
    writable: Callable[[str], bool] | None = None,
    read: Callable[[str], str | None] | None = None,
    reclaim: Callable[[], bool] | None = None,
    cgroupfs_trial: Callable[[str], bool] | None = None,
) -> sp.SandboxCapability:
    """A faked Linux host. Every collaborator is faked — the real path
    resolver included: on a composer whose ``os.path`` is not Linux's it would
    respell the rootfs (``C:\\opt\\...``) and the faked stamp would never be
    found. The ``current`` link's resolution has a test of its own."""
    return sp.probe(
        which=which or which_of(*ALL_TOOLS),
        run=run or run_of(),
        platform=platform,
        euid=euid,
        exists=exists or (lambda p: p in STAGED),
        writable=writable or (lambda p: False),
        read=read or RESOLV.get,
        realpath=lambda p: p,
        settings=sb.SandboxSettings(mode="gvisor"),
        reclaim=reclaim or (lambda: True),
        cgroupfs_trial=cgroupfs_trial or (lambda root: True),
    )


def test_gvisor_ready_when_root_has_setpriv_a_cgroup_runsc_a_rootfs_and_the_net_tools() -> None:
    found = linux_probe()
    assert found.gvisor is True
    assert found.controls is True
    assert found.cgroup == "systemd"
    assert found.runsc == "/usr/bin/runsc" and found.setpriv == "/usr/bin/setpriv"
    assert found.setfacl == "/usr/bin/setfacl" and found.exit_status is True
    assert found.rootfs == ROOTFS
    assert found.ip == "/usr/sbin/ip" and found.nft == "/usr/sbin/nft"
    assert found.mount_ns is True and found.unshare == "/usr/bin/unshare"
    assert found.default_env is True
    assert found.uv == "/usr/local/bin/uv" and found.python_home == PYTHON
    assert found.resolvers == ("172.31.0.2",)
    assert "gVisor ready" in found.reason


def test_the_probes_run_the_real_mechanisms() -> None:
    seen: list[tuple[str, ...]] = []

    def run(argv: Sequence[str]) -> int:
        seen.append(tuple(argv))
        return 0

    linux_probe(run=run)
    assert any(a[0] == "/usr/bin/runsc" and a[1] == "--version" for a in seen)
    assert any(a[0] == "/usr/bin/systemd-run" and "--scope" in a for a in seen)
    assert any(a[0] == "/usr/bin/unshare" and "--mount" in a and a[-1] == "/bin/true" for a in seen)


def test_a_cgroupfs_is_a_driver_only_when_a_chat_group_can_hold_its_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real trial, on a directory standing in for ``/sys/fs/cgroup``. A
    kernel cgroupfs populates a new group with the limit files only where the
    controllers were delegated; a plain directory stands in for one where they
    were not: the trial group is made under the parent slice, offers no limit
    file, and is removed again."""
    root = tmp_path / "cgroup"
    root.mkdir()
    assert sp.cgroupfs_holds_limits(str(root)) is False
    assert (root / "alkera.slice").is_dir()  # the parent slice the launch uses
    assert list((root / "alkera.slice").iterdir()) == []  # the trial group is gone
    # Where the kernel delegated the controllers it populates the files as the
    # group is made.
    real_makedirs = os.makedirs

    def kernel_makedirs(path: str, exist_ok: bool = False) -> None:
        real_makedirs(path, exist_ok=exist_ok)
        for name in sp.CGROUP_LIMIT_FILES:
            Path(path, name).write_text("max\n")

    monkeypatch.setattr(os, "makedirs", kernel_makedirs)
    assert sp.cgroupfs_holds_limits(str(tmp_path / "delegated")) is True
    monkeypatch.undo()
    # A root no group can be made under is no driver.
    blocked = tmp_path / "a-file"
    blocked.write_text("")
    assert sp.cgroupfs_holds_limits(str(blocked)) is False


@pytest.mark.parametrize(
    ("overrides", "gvisor", "controls", "cgroup", "reason"),
    [
        pytest.param(
            {"which": which_of(*(t for t in ALL_TOOLS if t != "runsc"))},
            False,
            True,
            "systemd",
            "no runsc",
            id="no-runsc-is-none-mode-with-controls",
        ),
        pytest.param(
            {"run": run_of(frozenset({"runsc"}))},
            False,
            True,
            "systemd",
            "runsc present but --version failed",
            id="runsc-broken",
        ),
        pytest.param(
            {"exists": lambda p: p in STAGED - {f"{ROOTFS}/{sb.ROOTFS_STAMP}"}},
            False,
            True,
            "systemd",
            "no staged rootfs",
            id="rootfs-without-its-stamp-is-half-built",
        ),
        pytest.param(
            {"exists": lambda p: p in STAGED - {f"{ROOTFS}/bin/sh"}},
            False,
            True,
            "systemd",
            "no staged rootfs",
            id="rootfs-stamp-without-a-shell",
        ),
        pytest.param(
            {"which": which_of(*(t for t in ALL_TOOLS if t != "nft"))},
            False,
            True,
            "systemd",
            "no ip/nft",
            id="no-nft-no-chat-network",
        ),
        pytest.param(
            {"which": which_of(*(t for t in ALL_TOOLS if t != "ip"))},
            False,
            True,
            "systemd",
            "no ip/nft",
            id="no-ip-no-chat-network",
        ),
        pytest.param(
            {
                "which": which_of(*(t for t in ALL_TOOLS if t != "systemd-run")),
                "exists": lambda p: p in STAGED or p == "/sys/fs/cgroup/cgroup.controllers",
                "writable": lambda p: p == "/sys/fs/cgroup",
            },
            True,
            True,
            "cgroupfs",
            "gVisor ready",
            id="no-systemd-but-writable-cgroupfs",
        ),
        pytest.param(
            {"run": run_of(frozenset({"systemd-run"}))},
            False,
            True,
            "none",
            "no writable cgroup v2 and no systemd: chat uid only",
            id="no-cgroup-at-all-is-uid-only",
        ),
        pytest.param(
            {
                "which": which_of(*(t for t in ALL_TOOLS if t != "systemd-run")),
                "exists": lambda p: p in STAGED or p == "/sys/fs/cgroup/cgroup.controllers",
                "writable": lambda p: p == "/sys/fs/cgroup",
                "cgroupfs_trial": lambda root: False,
            },
            False,
            True,
            "none",
            "delegates no cpu/memory/pids controller",
            id="writable-cgroupfs-without-delegated-controllers-is-uid-only",
        ),
        pytest.param({"euid": 1000}, False, False, "none", "not root", id="unprivileged-daemon"),
        pytest.param(
            {"which": which_of(*(t for t in ALL_TOOLS if t != "setpriv"))},
            False,
            False,
            "none",
            "no setpriv",
            id="no-setpriv",
        ),
        pytest.param(
            {"which": which_of(*(t for t in ALL_TOOLS if t != "setfacl"))},
            False,
            False,
            "none",
            "no setfacl",
            id="no-setfacl-no-chat-uid",
        ),
        pytest.param(
            {"reclaim": lambda: False},
            False,
            False,
            "none",
            "cannot read its children's exit status",
            id="exit-status-unreadable",
        ),
        pytest.param(
            {"platform": "darwin"}, False, False, "none", "no sandbox on darwin", id="macos"
        ),
        pytest.param(
            {"platform": "win32"}, False, False, "none", "no sandbox on win32", id="windows"
        ),
    ],
)
def test_the_probe_reports_each_capability_state(
    overrides: dict[str, Any], gvisor: bool, controls: bool, cgroup: str, reason: str
) -> None:
    found = linux_probe(**overrides)
    assert found.gvisor is gvisor
    assert found.controls is controls
    assert found.cgroup == cgroup
    assert reason in found.reason


def test_the_default_environment_needs_both_uv_and_the_interpreter() -> None:
    no_uv = linux_probe(which=which_of(*(t for t in ALL_TOOLS if t != "uv")))
    assert no_uv.default_env is False and no_uv.python_home == PYTHON
    no_python = linux_probe(exists=lambda p: p in STAGED - {f"{PYTHON}/bin/python3"})
    assert no_python.default_env is False and no_python.uv is not None
    # Neither is a reason to refuse gVisor: the environment is a convenience.
    assert no_uv.gvisor is True and no_python.gvisor is True


def test_the_mount_namespace_is_reported_only_when_unshare_actually_works() -> None:
    assert linux_probe(run=run_of(frozenset({"unshare"}))).mount_ns is False
    assert linux_probe(which=which_of(*(t for t in ALL_TOOLS if t != "unshare"))).mount_ns is False
    assert linux_probe().mount_ns is True


def test_the_settings_name_where_the_rootfs_and_the_interpreter_are() -> None:
    elsewhere = sb.SandboxSettings(mode="gvisor", rootfs="/srv/rootfs", python_home="/srv/py")
    staged = {"/srv/rootfs/.alkera-rootfs", "/srv/rootfs/bin/sh", "/srv/py/bin/python3"}
    found = sp.probe(
        which=which_of(*ALL_TOOLS),
        run=run_of(),
        platform="linux",
        euid=0,
        exists=lambda p: p in staged,
        writable=lambda p: False,
        read=RESOLV.get,
        realpath=lambda p: p,
        settings=elsewhere,
        reclaim=lambda: True,
    )
    assert found.rootfs == "/srv/rootfs" and found.python_home == "/srv/py"
    assert found.gvisor is True
    # The default paths are not looked at when the settings name others.
    default_only = sp.probe(
        which=which_of(*ALL_TOOLS),
        run=run_of(),
        platform="linux",
        euid=0,
        exists=lambda p: p in STAGED,
        writable=lambda p: False,
        read=RESOLV.get,
        realpath=lambda p: p,
        settings=elsewhere,
        reclaim=lambda: True,
    )
    assert default_only.rootfs is None and default_only.gvisor is False


def test_the_rootfs_is_reported_at_its_real_path_never_the_current_link() -> None:
    """runsc refuses to root a container at a symlink (its mount check opens
    the destination and finds the link's target), so the probe resolves the
    ``current`` link the prerequisites maintain and looks for the stamp there."""
    digest_dir = "/opt/alkera/rootfs/ded34c88"
    staged = {f"{digest_dir}/{sb.ROOTFS_STAMP}", f"{digest_dir}/bin/sh", f"{PYTHON}/bin/python3"}
    found = sp.probe(
        which=which_of(*ALL_TOOLS),
        run=run_of(),
        platform="linux",
        euid=0,
        exists=lambda p: p in staged,
        writable=lambda p: False,
        read=RESOLV.get,
        realpath=lambda p: digest_dir if p == ROOTFS else p,
        settings=sb.SandboxSettings(mode="gvisor"),
    )
    assert found.rootfs == digest_dir and found.gvisor is True
    # A link that points at nothing staged is nothing staged.
    dangling = sp.probe(
        which=which_of(*ALL_TOOLS),
        run=run_of(),
        platform="linux",
        euid=0,
        exists=lambda p: p in STAGED,  # the stamp exists only at the link's own path
        writable=lambda p: False,
        read=RESOLV.get,
        realpath=lambda p: digest_dir if p == ROOTFS else p,
        settings=sb.SandboxSettings(mode="gvisor"),
    )
    assert dangling.rootfs is None and dangling.gvisor is False


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(RESOLV, ("172.31.0.2",), id="resolved-host-real-upstream"),
        pytest.param(
            {"/etc/resolv.conf": "nameserver 10.0.0.2\nnameserver 10.0.0.3\n"},
            ("10.0.0.2", "10.0.0.3"),
            id="classic-file",
        ),
        pytest.param(
            {"/etc/resolv.conf": "nameserver 127.0.0.53\n"},
            (),
            id="only-a-loopback-stub-is-nothing",
        ),
        pytest.param(
            {
                "/run/systemd/resolve/resolv.conf": "nameserver 127.0.0.1\n",
                "/etc/resolv.conf": "nameserver 192.168.1.1\n",
            },
            ("192.168.1.1",),
            id="a-loopback-only-first-file-falls-through",
        ),
        pytest.param(
            {"/etc/resolv.conf": "nameserver 10.0.0.2\nnameserver 10.0.0.2\nnameserver bogus\n"},
            ("10.0.0.2",),
            id="duplicates-and-garbage-dropped",
        ),
        pytest.param(
            {"/etc/resolv.conf": "nameserver fd00:ec2::253\nnameserver ::1\n"},
            ("fd00:ec2::253",),
            id="ipv6-kept-loopback-dropped",
        ),
        pytest.param({}, (), id="no-file-at-all"),
    ],
)
def test_host_resolvers_hand_the_container_what_it_can_reach(
    files: dict[str, str], expected: tuple[str, ...]
) -> None:
    assert sp.host_resolvers(files.get) == expected


def test_an_unprivileged_container_runs_nothing_that_needs_root() -> None:
    """The RunPod dev box: not root. No useradd, no systemd-run, no runsc probe
    is even attempted, and it is neither gVisor-ready nor has the controls —
    but what the default environment can be made from is still reported."""
    seen: list[str] = []

    def run(argv: Sequence[str]) -> int:
        seen.append(argv[0])
        return 0

    found = linux_probe(euid=1000, run=run)
    assert found.gvisor is False and found.controls is False and found.uid is False
    assert found.mount_ns is False
    assert found.default_env is True
    assert seen == []


def test_a_root_container_with_no_cgroup_and_no_mount_namespace_keeps_the_chat_uid() -> None:
    """A RunPod-style container the daemon runs in as root: setpriv and setfacl
    present, no systemd, a read-only cgroup, ``unshare --mount`` refused. The
    uid is the one control that applies and is reported so; the cgroup and the
    alias are reported absent, never assumed."""
    found = linux_probe(
        run=run_of(frozenset({"systemd-run", "unshare"})),
        exists=lambda p: p in STAGED or p == "/sys/fs/cgroup/cgroup.controllers",
        writable=lambda p: False,
    )
    assert found.uid is True and found.controls is True
    assert found.cgroup == "none" and found.mount_ns is False
    assert found.gvisor is False
    assert found.reason == "no writable cgroup v2 and no systemd: chat uid only, no gVisor"


def test_a_daemon_that_cannot_read_exit_status_runs_no_mechanism_and_reports_none() -> None:
    """A daemon whose ``SIGCHLD`` an ancestor left ignored reads every child
    exit as success — a failed ``systemd-run --scope`` as a working systemd,
    a refused ``unshare --mount`` as a private mount namespace — which is how
    a container with no systemd once wrapped its agent in ``systemd-run``.
    The probe reclaims its children before running anything; where it cannot,
    it runs nothing and reports nothing an exit status would have proved."""
    order: list[str] = []

    def reclaim() -> bool:
        order.append("reclaim")
        return False

    def run(argv: Sequence[str]) -> int:
        order.append(f"run:{argv[0]}")
        return 0  # what a blind runner answers for everything

    found = linux_probe(reclaim=reclaim, run=run, writable=lambda p: True)
    assert order == ["reclaim"]  # nothing ran on a runner that cannot see a failure
    assert found.exit_status is False
    assert found.cgroup == "none" and found.mount_ns is False and found.runsc is None
    assert found.uid is False and found.controls is False and found.gvisor is False
    assert found.root is True and found.setpriv == "/usr/bin/setpriv"  # what was found is kept
    assert "SIGCHLD" in found.reason and "no chat uid, no gVisor" in found.reason
    # What the environment can be made from needs no exit status and is still reported.
    assert found.default_env is True and found.resolvers == ("172.31.0.2",)


def test_the_probe_reclaims_its_children_before_the_first_mechanism_runs() -> None:
    order: list[str] = []

    def reclaim() -> bool:
        order.append("reclaim")
        return True

    def run(argv: Sequence[str]) -> int:
        order.append("run")
        return 0

    found = linux_probe(reclaim=reclaim, run=run)
    assert order[0] == "reclaim" and "run" in order
    assert found.exit_status is True and found.gvisor is True


def test_a_runsc_present_but_no_cgroup_box_is_never_gvisor_ready() -> None:
    """gVisor needs a cgroup to place the container in, so runsc alone (no
    writable cgroup, no systemd) is not enough — fail-closed toward not
    claiming a boundary the box cannot actually enforce."""
    found = linux_probe(
        which=which_of(*(t for t in ALL_TOOLS if t != "systemd-run")),
        run=run_of(frozenset({"systemd-run"})),
    )
    assert found.runsc is None  # not even recorded: the cgroup check short-circuits first
    assert found.gvisor is False


def test_current_capability_runs_once_per_process() -> None:
    sp.reset_probe_for_tests()
    calls = 0

    def prober() -> sp.SandboxCapability:
        nonlocal calls
        calls += 1
        return linux_probe(platform="darwin")

    try:
        first = sp.current_capability(prober=prober)
        second = sp.current_capability(prober=prober)
        assert first is second
        assert calls == 1
    finally:
        sp.reset_probe_for_tests()
