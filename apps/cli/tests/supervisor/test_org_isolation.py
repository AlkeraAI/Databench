"""The box finds out how far apart it can keep its orgs, by trying, and every
worker launch follows what it found.

A RunPod pod is the host this was written against: its root cgroup holds the
container's own processes (the kernel refuses to hand controllers down), it
has no ``CAP_SYS_ADMIN`` (no mount namespace), its util-linux predates
``--map-users``, and it has no ``CAP_NET_ADMIN``. Each is one failing
primitive here."""

from __future__ import annotations

import ast
import errno
from collections.abc import Sequence
from pathlib import Path

import alkera_cli
import pytest
from alkera_cli.supervisor.launch import ORGS_CGROUP, WorkerLaunch, plan_launch
from alkera_cli.supervisor.service import probe_isolation as probe
from alkera_cli.supervisor.slots import Slot
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.box_isolation import (
    IsolationMechanism,
    IsolationProfile,
    IsolationReport,
)
from alkera_core.host_isolation import LIMIT_FILES, Host

ORG_A = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
ORG_B = "be0a3393-082f-4acf-918d-6eac00904c9b"
ALL = frozenset(IsolationMechanism)


class FakeHost:
    """A host whose primitives answer as told, recording what was run."""

    def __init__(
        self,
        cgroup_root: Path,
        *,
        systemd: bool = False,
        controllers: bool = True,
        refuse: Sequence[str] = (),
    ) -> None:
        self.cgroup_root = cgroup_root
        self.systemd = systemd
        self.controllers = controllers
        self.refuse = refuse
        self.ran: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> int:
        self.ran.append(tuple(argv))
        joined = " ".join(argv)
        return 1 if any(word in joined for word in self.refuse) else 0

    def write(self, path: Path, text: str) -> None:
        if not self.controllers:
            # What the kernel answers when the root cgroup holds processes.
            raise OSError(errno.EIO, "Input/output error")
        for child in path.parent.glob("**/probe-*"):
            for name in LIMIT_FILES:
                (child / name).touch()

    def host(self, *, root: bool = True) -> Host:
        return Host(
            run=self.run,
            write=self.write,
            which=lambda name: f"/usr/bin/{name}",
            systemd=lambda: self.systemd,
            root=root,
            cgroup_root=self.cgroup_root,
            pid=4242,
        )


def runpod(tmp_path: Path) -> FakeHost:
    """The pod: undelegatable cgroups, no mount or network namespace, an
    ``unshare`` with no ``--map-users``."""
    return FakeHost(
        tmp_path / "cgroup",
        controllers=False,
        refuse=("--mount", "--map-users", "--net", "veth"),
    )


def test_a_runpod_like_host_is_single_org_with_every_reason_named(tmp_path: Path) -> None:
    report = probe(runpod(tmp_path).host())
    assert report.profile == IsolationProfile.SINGLE_ORG
    assert report.mechanisms == frozenset()
    assert set(report.missing) == ALL
    assert (
        "would not hand its controllers down"
        in report.missing[IsolationMechanism.CGROUP_DELEGATION]
    )
    assert report.capabilities() == ()
    assert report.heartbeat() == {"profile": "single_org", "mechanisms": []}
    # The throwaway cgroup is gone again.
    assert not list((tmp_path / "cgroup" / ORGS_CGROUP).glob("probe-*"))


def test_a_whole_host_without_systemd_isolates_orgs(tmp_path: Path) -> None:
    host = FakeHost(tmp_path / "cgroup")
    report = probe(host.host())
    assert report.profile == IsolationProfile.ORG_NAMESPACES
    assert report.mechanisms == ALL - {IsolationMechanism.SYSTEMD}
    assert not report.units
    assert report.capabilities() == (BoxCapability.ORG_ISOLATION,)
    # The id map is tried the way a worker gets it.
    assert any("--map-users=0:10000000:1" in argv for argv in host.ran)


def test_a_systemd_host_delegates_through_a_scope_and_runs_units(tmp_path: Path) -> None:
    host = FakeHost(tmp_path / "cgroup", systemd=True, controllers=False)
    report = probe(host.host())
    assert report.profile == IsolationProfile.ORG_NAMESPACES
    assert report.units
    assert any("--property=Delegate=yes" in argv for argv in host.ran)


@pytest.mark.parametrize(
    ("refuse", "controllers", "lost"),
    [
        pytest.param((), False, IsolationMechanism.CGROUP_DELEGATION, id="no-cgroup-delegation"),
        pytest.param(("--mount",), True, IsolationMechanism.MOUNT_NAMESPACE, id="no-mount-ns"),
        pytest.param(("--map-users",), True, IsolationMechanism.USER_NAMESPACE, id="old-unshare"),
        pytest.param(("--net",), True, IsolationMechanism.NETWORK_NAMESPACE, id="no-net-ns"),
        pytest.param(("veth",), True, IsolationMechanism.NETWORK_NAMESPACE, id="no-net-admin"),
    ],
)
def test_any_one_mechanism_missing_makes_the_box_single_org(
    tmp_path: Path, refuse: tuple[str, ...], controllers: bool, lost: IsolationMechanism
) -> None:
    report = probe(FakeHost(tmp_path / "cgroup", controllers=controllers, refuse=refuse).host())
    assert report.profile == IsolationProfile.SINGLE_ORG
    assert lost not in report.mechanisms
    assert lost in report.missing
    assert report.capabilities() == ()


def test_a_cgroup_that_offers_no_limits_is_no_delegation(tmp_path: Path) -> None:
    host = FakeHost(tmp_path / "cgroup")
    host.write = lambda path, text: None  # type: ignore[method-assign]  # enabled, nothing appears
    report = probe(host.host())
    assert IsolationMechanism.CGROUP_DELEGATION not in report.mechanisms
    assert "offers no" in report.missing[IsolationMechanism.CGROUP_DELEGATION]


def test_a_supervisor_that_is_not_root_can_isolate_nothing(tmp_path: Path) -> None:
    host = FakeHost(tmp_path / "cgroup")
    report = probe(host.host(root=False))
    assert report.profile == IsolationProfile.SINGLE_ORG
    assert host.ran == []


@pytest.mark.parametrize(
    ("mechanisms", "held", "org", "admitted"),
    [
        pytest.param(frozenset(), set(), ORG_A, True, id="single-org-first-org"),
        pytest.param(frozenset(), {ORG_A}, ORG_A, True, id="single-org-same-org-again"),
        pytest.param(frozenset(), {ORG_A}, ORG_B, False, id="single-org-refuses-a-second"),
        pytest.param(ALL, {ORG_A}, ORG_B, True, id="namespaced-takes-a-second"),
    ],
)
def test_a_box_that_cannot_isolate_holds_one_org(
    mechanisms: frozenset[IsolationMechanism], held: set[str], org: str, admitted: bool
) -> None:
    assert IsolationReport(mechanisms).admits(org, held) is admitted


# -- the launch follows the profile ---------------------------------------------


def _launch() -> WorkerLaunch:
    return WorkerLaunch(
        slot=Slot(index=3, org_id=ORG_A),
        org_root=Path("/opt/alkera-work/orgs/3"),
        argv=("/usr/bin/alkera", "cloud-mirror", "worker", "--control-fd", "0"),
        env={"HOME": "/opt/alkera-work/orgs/3/home"},
    )


NAMESPACE_WORDS = ("unshare", "cgroup", "systemd-run", "/bin/sh")


def test_a_single_org_box_spawns_the_worker_itself_and_touches_no_namespace() -> None:
    plan = plan_launch(_launch(), IsolationReport(frozenset()))
    assert plan.profile == IsolationProfile.SINGLE_ORG
    assert plan.argv == _launch().argv
    assert plan.before == ()
    assert plan.unit is None
    assert not plan.linked
    assert plan.env == {"HOME": "/opt/alkera-work/orgs/3/home"}
    assert not any(word in " ".join(plan.argv) for word in NAMESPACE_WORDS)


def test_a_namespaced_box_makes_the_cgroup_then_the_chain() -> None:
    plan = plan_launch(_launch(), IsolationReport(ALL - {IsolationMechanism.SYSTEMD}))
    assert plan.profile == IsolationProfile.ORG_NAMESPACES
    assert plan.before and plan.unit is None and plan.linked
    assert plan.argv[:3] == ("/bin/sh", "-c", plan.argv[2])
    assert "unshare" in plan.argv
    assert plan.argv[-len(_launch().argv) :] == _launch().argv


def test_a_namespaced_systemd_box_runs_each_worker_as_a_unit() -> None:
    plan = plan_launch(_launch(), IsolationReport(ALL))
    assert plan.unit == "alkera-org-3.service"
    assert plan.argv[0] == "systemd-run"
    assert plan.before == () and plan.env is None and plan.linked


# -- the gate: nothing reaches the cgroup and namespace builders but the plan ----

#: The builders that make an org cgroup or a namespace for a worker.
NAMESPACED_BUILDERS = frozenset(
    {"cgroup_steps", "spawn_argv", "unit_argv", "_harden_argv", "network_steps"}
)
#: The modules that may name them: where they are defined, the one place a
#: launch is chosen from the probed profile, and the supervisor's link step,
#: which runs only for a plan that says the worker has a network namespace.
MAY_NAME_THEM = {
    "supervisor/launch.py": "defines them, and plan_launch, which checks the probed profile first",
    "supervisor/service.py": "network_steps, only when plan.linked",
}


def _names_a_builder(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "alkera_cli.supervisor.launch":
            found |= {alias.name for alias in node.names} & NAMESPACED_BUILDERS
        elif isinstance(node, ast.Attribute) and node.attr in NAMESPACED_BUILDERS:
            found.add(node.attr)
    return found


def test_only_the_profile_check_reaches_the_namespaced_builders() -> None:
    """A launch path that made a cgroup or a namespace without asking the
    probed profile first is how a RunPod box crash-looped: every worker
    start ran ``cgroup_steps`` on a host that could not delegate one."""
    package = Path(alkera_cli.__file__).parent
    reached = {
        str(path.relative_to(package)): names
        for path in package.rglob("*.py")
        if (names := _names_a_builder(path))
    }
    assert set(reached) <= set(MAY_NAME_THEM), reached
    assert reached.get("supervisor/service.py", set()) <= {"network_steps"}


def _callers_of_a_launch(path: Path) -> set[str]:
    """The functions in ``path`` that call a builder which starts a worker in
    a cgroup or namespaces of its own."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    starts = {"cgroup_steps", "spawn_argv", "unit_argv"}
    return {
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in starts
    }


def test_inside_the_launch_module_only_the_plan_calls_the_namespaced_builders() -> None:
    launch = Path(alkera_cli.__file__).parent / "supervisor" / "launch.py"
    assert _callers_of_a_launch(launch) == {"plan_launch"}


def test_the_caller_scan_catches_a_second_way_in(tmp_path: Path) -> None:
    rogue = tmp_path / "launch.py"
    rogue.write_text(
        "def plan_launch(l, i):\n    return spawn_argv(l)\n\n"
        "def quick_start(l):\n    return cgroup_steps(l), unit_argv(l)\n",
        encoding="utf-8",
    )
    assert _callers_of_a_launch(rogue) == {"plan_launch", "quick_start"}


def test_the_gate_scan_catches_a_launch_that_skips_the_profile(tmp_path: Path) -> None:
    rogue = tmp_path / "rogue.py"
    rogue.write_text(
        "from alkera_cli.supervisor.launch import cgroup_steps\n"
        "from alkera_cli.supervisor import launch\n"
        "steps = launch.spawn_argv(None)\n",
        encoding="utf-8",
    )
    assert _names_a_builder(rogue) == {"cgroup_steps", "spawn_argv"}
