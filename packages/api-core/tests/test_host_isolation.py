"""The trials that find out what a host lets root do, shared by the chat
sandbox's probe and the box supervisor's."""

from __future__ import annotations

import shutil
import signal
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from alkera_core import host_isolation
from alkera_core.compute.box_isolation import IsolationMechanism, IsolationProfile
from alkera_core.host_isolation import (
    LIMIT_FILES,
    Host,
    cgroup_limits_missing,
    linked_network_namespace,
    mapped_user_namespace,
    private_mount_namespace,
    probe,
    run_status,
    systemd_scope,
)

needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX signals and /bin")


class Recorder:
    """A runner that answers 1 for any argv holding a refused word."""

    def __init__(self, *refuse: str) -> None:
        self.refuse = refuse
        self.ran: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        self.ran.append(tuple(argv))
        return 1 if any(word in " ".join(argv) for word in self.refuse) else 0


@pytest.fixture
def ignored_sigchld() -> Iterator[None]:
    before = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGCHLD, before)


@needs_posix
def test_a_trial_reads_a_real_exit_status_where_sigchld_was_left_ignored(
    ignored_sigchld: None,
) -> None:
    """Under an ignored ``SIGCHLD`` the kernel reaps every child itself and a
    plain ``subprocess.run`` reads a failing command as 0, so a host that
    refuses a namespace would read as offering it."""
    false = shutil.which("false")
    assert false is not None
    assert run_status([false]) == 1


def test_a_command_that_cannot_be_run_is_a_failure(tmp_path: Path) -> None:
    assert run_status([str(tmp_path / "no-such-binary")]) == 127


@pytest.mark.parametrize(
    ("delegate", "argv"),
    [
        pytest.param(False, ("systemd-run", "--scope", "--quiet", "--", "/bin/true"), id="a-scope"),
        pytest.param(
            True,
            ("systemd-run", "--scope", "--quiet", "--property=Delegate=yes", "--", "/bin/true"),
            id="a-delegated-scope",
        ),
    ],
)
def test_the_scope_trial_runs_true_in_a_transient_scope(
    delegate: bool, argv: tuple[str, ...]
) -> None:
    run = Recorder()
    assert systemd_scope(run, delegate=delegate) is True
    assert run.ran == [argv]
    assert systemd_scope(Recorder("--scope"), delegate=delegate) is False


def test_a_cgroup_that_offers_every_limit_file_can_be_limited(tmp_path: Path) -> None:
    parent = tmp_path / "alkera.slice"
    trial = parent / "probe-7"
    trial.mkdir(parents=True)
    for name in LIMIT_FILES:
        (trial / name).touch()
    assert cgroup_limits_missing(parent, pid=7) is None


def test_a_cgroup_missing_a_limit_file_says_which_and_is_removed(tmp_path: Path) -> None:
    parent = tmp_path / "alkera.slice"
    assert cgroup_limits_missing(parent, pid=7) == (
        f"a cgroup under {parent} offers no cpu.max, memory.max, pids.max"
    )
    assert list(parent.iterdir()) == []


def test_a_cgroup_that_cannot_be_made_says_so(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    why = cgroup_limits_missing(blocker, pid=7)
    assert why is not None
    assert why.startswith(f"no cgroup could be made under {blocker}")


def test_controllers_that_cannot_be_enabled_are_the_reason_and_the_group_is_removed(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "alkera.orgs"
    assert cgroup_limits_missing(parent, pid=7, enable=lambda: "refused") == "refused"
    assert list(parent.iterdir()) == []


def test_each_namespace_trial_asks_unshare_the_way_a_launch_does() -> None:
    run = Recorder()
    assert private_mount_namespace(run, "/usr/bin/unshare")
    assert mapped_user_namespace(run, "/usr/bin/unshare", uid_base=500_000)
    assert linked_network_namespace(run, "/usr/bin/unshare", "/sbin/ip", tag=123_456) is None
    assert run.ran == [
        ("/usr/bin/unshare", "--mount", "--propagation", "private", "--", "/bin/true"),
        (
            "/usr/bin/unshare",
            "--user",
            "--map-users=0:500000:1",
            "--map-groups=0:500000:1",
            "--setuid",
            "0",
            "--setgid",
            "0",
            "--",
            "/bin/true",
        ),
        ("/usr/bin/unshare", "--net", "--", "/bin/true"),
        ("/sbin/ip", "link", "add", "alkp23456", "type", "veth", "peer", "name", "alkp23456b"),
        ("/sbin/ip", "link", "delete", "alkp23456"),
    ]


@pytest.mark.parametrize(
    ("refuse", "why"),
    [
        pytest.param("--net", "a network namespace is not permitted", id="no-namespace"),
        pytest.param("veth", "a veth link cannot be made", id="no-link"),
    ],
)
def test_a_network_namespace_that_cannot_be_linked_says_why(refuse: str, why: str) -> None:
    run = Recorder(refuse)
    assert linked_network_namespace(run, "unshare", "ip", tag=1) == why
    assert not any("delete" in argv for argv in run.ran)


def test_a_refused_namespace_is_not_offered() -> None:
    assert not private_mount_namespace(Recorder("--mount"), "unshare")
    assert not mapped_user_namespace(Recorder("--map-users"), "unshare", uid_base=1)


def _host(tmp_path: Path, run: Recorder, *, tools: tuple[str, ...]) -> Host:
    return Host(
        run=run,
        write=lambda path, text: None,
        which=lambda name: f"/usr/bin/{name}" if name in tools else None,
        systemd=lambda: True,
        root=True,
        cgroup_root=tmp_path,
        pid=9,
    )


def test_a_host_without_unshare_has_no_namespace_and_tries_none(tmp_path: Path) -> None:
    run = Recorder()
    report = probe(_host(tmp_path, run, tools=("ip",)), cgroup="g", controllers=(), uid_base=1)
    assert report.mechanisms == {IsolationMechanism.SYSTEMD, IsolationMechanism.CGROUP_DELEGATION}
    assert report.profile == IsolationProfile.SINGLE_ORG
    assert {report.missing[m] for m in report.missing} == {"no unshare"}
    assert all(argv[0] == "systemd-run" for argv in run.ran)


def test_a_host_without_ip_has_no_network_namespace(tmp_path: Path) -> None:
    run = Recorder()
    report = probe(_host(tmp_path, run, tools=("unshare",)), cgroup="g", controllers=(), uid_base=1)
    assert report.missing == {IsolationMechanism.NETWORK_NAMESPACE: "no ip"}
    assert not any("--net" in argv for argv in run.ran)


def test_the_probe_enables_the_callers_controllers_above_its_cgroups(tmp_path: Path) -> None:
    wrote: list[tuple[Path, str]] = []
    host = Host(
        run=Recorder(),
        write=lambda path, text: wrote.append((path, text)),
        which=lambda name: f"/usr/bin/{name}",
        systemd=lambda: False,
        root=True,
        cgroup_root=tmp_path,
        pid=9,
    )
    report = probe(host, cgroup="alkera.orgs", controllers=("cpu", "pids"), uid_base=1)
    assert wrote == [
        (tmp_path / "cgroup.subtree_control", "+cpu +pids"),
        (tmp_path / "alkera.orgs" / "cgroup.subtree_control", "+cpu +pids"),
    ]
    # A plain directory offers no limit file: enabling was not enough here.
    assert IsolationMechanism.CGROUP_DELEGATION in report.missing


def test_the_default_runner_is_the_one_that_goes_through_the_spawn_seam() -> None:
    assert Host().run is host_isolation.run_status
