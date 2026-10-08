"""How one org's worker is started, pinned as argv."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from alkera_cli.supervisor.launch import (
    HARDEN_SHELL,
    ORGS_SLICE,
    RESET_SHELL,
    RW_CHECK,
    UNIT_PROPERTIES,
    WorkerLaunch,
    cgroup_steps,
    network_steps,
    spawn_argv,
    unit_argv,
    worker_env,
)
from alkera_cli.supervisor.service import WORKER_ENV_NAMES
from alkera_cli.supervisor.slots import ORG_UID_SPAN, Slot
from alkera_core.compute.box_isolation import IsolationProfile

ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
SLOT = Slot(2, ORG)
ROOT = Path("/opt/alkera-work/orgs/2")
ARGV = (
    "/opt/alkera/alkera",
    "cloud-mirror",
    "worker",
    "--org-root",
    str(ROOT),
    "--control-fd",
    "7",
)


def _launch() -> WorkerLaunch:
    return WorkerLaunch(slot=SLOT, org_root=ROOT, argv=ARGV)


def test_the_worker_joins_its_cgroup_then_hardens_then_enters_its_user_namespace() -> None:
    argv = spawn_argv(_launch())
    assert argv[:5] == (
        "/bin/sh",
        "-c",
        'echo $$ > "$1" || exit 90; shift; exec "$@"',
        "alkera-org-join",
        "/sys/fs/cgroup/alkera.orgs/org-2/cgroup.procs",
    )
    assert argv[5:10] == ("unshare", "--mount", "--propagation", "private", "--")
    harden = argv.index(HARDEN_SHELL)
    assert argv[harden + 1 :] == (
        "alkera-org-harden",
        str(ROOT),
        str(SLOT.uid_base),
        str(ORG_UID_SPAN),
        "unshare",
        *ARGV,
    )


def test_the_user_namespace_maps_the_slot_s_range_and_owns_every_namespace_the_worker_uses() -> (
    None
):
    assert '--map-users="0:$base:$span"' in HARDEN_SHELL
    assert '--map-groups="0:$base:$span"' in HARDEN_SHELL
    tail = HARDEN_SHELL[HARDEN_SHELL.index('exec "$unshare" --user') :]
    for flag in ("--setuid 0", "--setgid 0", "--mount", "--net", "--cgroup"):
        assert flag in tail


def test_the_hardening_closes_every_mount_but_the_org_s_and_hides_other_processes() -> None:
    """Order matters: the read-only pass first, then the org's root alone back
    read-write, private ``/tmp`` and ``/dev/shm``, and a ``/proc`` that shows
    no other org's process; all before the user namespace, which locks them."""
    steps = [
        "remount,bind,ro",
        'mount --bind "$root" "$root" && mount -o remount,bind,rw "$root"',
        "tmpfs /tmp",
        "tmpfs /dev/shm",
        "hidepid=invisible proc /proc",
        'exec "$unshare" --user',
    ]
    at = [HARDEN_SHELL.index(step) for step in steps]
    assert at == sorted(at)
    assert "/proc|/proc/*|/sys|/sys/*) continue" in HARDEN_SHELL


@pytest.mark.skipif(shutil.which("sh") is None or sys.platform == "win32", reason="needs sh")
@pytest.mark.parametrize("script", [HARDEN_SHELL, RESET_SHELL, RW_CHECK])
def test_the_shells_parse(script: str) -> None:
    assert subprocess.run(["sh", "-n", "-c", script], check=False).returncode == 0


def test_the_org_cgroup_is_reset_then_delegated_to_the_slot_alone() -> None:
    owner = f"{SLOT.uid_base}:{SLOT.uid_base}"
    org = "/sys/fs/cgroup/alkera.orgs/org-2"
    steps = cgroup_steps(_launch())
    assert steps[0] == ("/bin/sh", "-c", RESET_SHELL, "reset", org)
    assert steps[1] == ("mkdir", "-p", org)
    assert steps[-1] == (
        "chown",
        owner,
        org,
        f"{org}/cgroup.procs",
        f"{org}/cgroup.subtree_control",
        f"{org}/cgroup.threads",
    )
    # Nothing above the org's cgroup changes hands.
    assert all("alkera.orgs/" in path for path in steps[-1][2:])


def test_the_worker_s_link_lands_in_its_namespace_and_a_leftover_goes_first() -> None:
    assert network_steps(_launch(), 4242) == (
        ("ip", "link", "delete", "vo2"),
        ("ip", "link", "add", "vo2", "type", "veth", "peer", "name", "eth0", "netns", "4242"),
        ("ip", "addr", "add", f"{SLOT.host_ip}/30", "dev", "vo2"),
        ("ip", "link", "set", "vo2", "up"),
    )


def test_the_worker_s_environment_is_its_own_and_never_carries_the_machine_credential() -> None:
    passthrough = {
        "ALKERA_API_URL": "https://api",
        "ALKERA_MACHINE_CREDENTIAL": "alkm_secret",
        "ALKERA_HOME": "/opt/alkera-home",
        "HOME": "/root",
        "AWS_SECRET_ACCESS_KEY": "nope",
    }
    env = worker_env(
        ROOT,
        SLOT,
        passthrough=passthrough,
        names=(*WORKER_ENV_NAMES, "ALKERA_MACHINE_CREDENTIAL"),
        profile=IsolationProfile.ORG_NAMESPACES,
    )
    assert "ALKERA_MACHINE_CREDENTIAL" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["ALKERA_API_URL"] == "https://api"
    assert env["HOME"] == env["ALKERA_HOME"] == f"{ROOT}/home"
    assert env["TMPDIR"] == f"{ROOT}/tmp"
    assert env["ALKERA_SANDBOX_UID_LEDGER"] == f"{ROOT}/home/sandbox-uids.json"
    assert env["ALKERA_SANDBOX_TREES_FLOOR"] == str(ROOT)
    assert (env["ALKERA_ORG_WORKER_IP"], env["ALKERA_ORG_HOST_IP"]) == (
        SLOT.worker_ip,
        SLOT.host_ip,
    )
    assert "ALKERA_MACHINE_CREDENTIAL" not in WORKER_ENV_NAMES


def test_on_systemd_the_worker_is_a_hardened_unit_writing_its_own_root_alone() -> None:
    launch = WorkerLaunch(
        slot=SLOT, org_root=ROOT, argv=ARGV, env={"HOME": f"{ROOT}/home", "PATH": "/usr/bin"}
    )
    argv = unit_argv(launch)
    end = argv.index("--")
    head = argv[:end]
    assert head[:5] == (
        "systemd-run",
        "--pipe",
        "--quiet",
        "--collect",
        "--unit=alkera-org-2.service",
    )
    assert f"--slice={ORGS_SLICE}" in head
    props = {a.split("=", 1)[1] for a in head if a.startswith("--property=")}
    for wanted in (
        "ProtectSystem=strict",
        "ProtectProc=invisible",
        "PrivateTmp=yes",
        "NoNewPrivileges=yes",
        "Delegate=cpu cpuset io memory pids",
        f"ReadWritePaths={ROOT}",
    ):
        assert wanted in props, wanted
    assert set(UNIT_PROPERTIES) <= props
    # The only writable path the unit is given is the org's root.
    assert [p for p in props if p.startswith("ReadWritePaths=")] == [f"ReadWritePaths={ROOT}"]
    assert f"--setenv=HOME={ROOT}/home" in head
    # Inside: the unit's own delegated cgroup to the slot, then the same chain.
    tail = argv[end + 1 :]
    assert tail[3:5] == ("alkera-org-delegate", str(SLOT.uid_base))
    assert 'chown "$1:$1" "$cg"' in tail[2]
    assert tail[5:10] == ("unshare", "--mount", "--propagation", "private", "--")
    assert tail[-len(ARGV) :] == ARGV


def test_the_unit_is_never_given_the_machine_credential() -> None:
    env = worker_env(
        ROOT,
        SLOT,
        passthrough={"ALKERA_MACHINE_CREDENTIAL": "alkm_x"},
        names=WORKER_ENV_NAMES,
        profile=IsolationProfile.ORG_NAMESPACES,
    )
    argv = unit_argv(WorkerLaunch(slot=SLOT, org_root=ROOT, argv=ARGV, env=env))
    assert not any("alkm_x" in a or "MACHINE_CREDENTIAL" in a for a in argv)


def test_the_unit_keeps_openat2_and_a_writable_sysfs() -> None:
    """RestrictSUIDSGID's filter answers openat2 with ENOSYS, which every folder
    take resolves paths with (a box refused every chat that way), and
    ProtectKernelTunables locks /sys read-only under the worker's own sysfs."""
    assert "RestrictSUIDSGID=yes" not in UNIT_PROPERTIES
    assert "ProtectKernelTunables=yes" not in UNIT_PROPERTIES


def test_the_writable_check_runs_after_every_mount_and_before_the_user_namespace() -> None:
    """A mount the read-only pass could not remount used to stay writable to
    the worker without a word; the check after every mount refuses it."""
    at = [
        HARDEN_SHELL.index("hidepid=invisible proc /proc"),
        HARDEN_SHELL.index(RW_CHECK),
        HARDEN_SHELL.index('exec "$unshare" --user'),
    ]
    assert at == sorted(at)
    assert "exit 96" in RW_CHECK


def _mountinfo(*mounts: tuple[str, str]) -> str:
    return "".join(
        f"{n} 1 0:{n} / {point} {opts} shared:{n} - ext4 /dev/x rw\n"
        for n, (point, opts) in enumerate(mounts, start=20)
    )


ROOT_SPACE = "/opt/alkera work/orgs/2"


@pytest.mark.skipif(shutil.which("sh") is None or sys.platform == "win32", reason="needs sh")
@pytest.mark.parametrize(
    ("mounts", "status"),
    [
        pytest.param(
            [
                ("/", "ro,relatime"),
                ("/var/tmp", "ro"),
                (ROOT_SPACE.replace(" ", "\\040"), "rw,relatime"),
                ("/tmp", "rw,nosuid"),
                ("/dev/shm", "rw"),
                ("/proc", "rw"),
                ("/proc/sys/fs/binfmt_misc", "rw"),
                ("/sys", "rw"),
            ],
            0,
            id="all-closed-but-the-org-s-own",
        ),
        pytest.param([("/", "ro"), ("/var/tmp", "rw,nosuid")], 96, id="a-remount-refused"),
        pytest.param([("/", "rw")], 96, id="the-host-root"),
        pytest.param(
            [("/", "ro"), ("/home", "rw"), ("/home", "ro,nosuid")],
            0,
            id="a-writable-mount-shadowed-by-a-closed-one",
        ),
        pytest.param(
            [("/", "ro"), ("/home", "ro"), ("/home", "rw")], 96, id="a-closed-mount-shadowed-open"
        ),
        pytest.param(
            [("/", "ro"), ("/opt/alkera\\040work/orgs/3", "rw")], 96, id="another-org-s-root"
        ),
        pytest.param([("/", "ro"), ("/tmpfoo", "rw")], 96, id="a-prefix-is-not-tmp"),
    ],
)
def test_the_writable_check_refuses_any_mount_left_open(
    tmp_path: Path, mounts: list[tuple[str, str]], status: int
) -> None:
    info = tmp_path / "mountinfo"
    info.write_text(_mountinfo(*mounts))
    script = f'root="$1"; mi="$2"; real="$root"; {RW_CHECK} exit 0'
    done = subprocess.run(
        ["sh", "-c", script, "check", ROOT_SPACE, str(info)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == status, done.stderr
    if status:
        assert "writable after hardening" in done.stderr


GIB = 1 << 30


def test_an_orgs_memory_limit_is_set_before_its_cgroup_is_handed_over() -> None:
    """Written by root while root owns the file: the slot is handed only the
    delegated files, so the worker cannot raise its own limit."""
    org = "/sys/fs/cgroup/alkera.orgs/org-2"
    steps = cgroup_steps(replace(_launch(), memory_max_bytes=2 * GIB))
    limit = ("/bin/sh", "-c", 'echo "$1" > "$2"', "limit", str(2 * GIB), f"{org}/memory.max")
    assert limit in steps
    assert steps.index(limit) < len(steps) - 1 and steps[-1][0] == "chown"
    assert f"{org}/memory.max" not in steps[-1]


def test_an_orgs_unit_carries_its_memory_limit() -> None:
    argv = unit_argv(replace(_launch(), memory_max_bytes=2 * GIB))
    assert f"--property=MemoryMax={2 * GIB}" in argv
    assert argv.index(f"--property=MemoryMax={2 * GIB}") < argv.index("--")


def test_a_launch_with_no_known_memory_sets_no_limit() -> None:
    assert not any("memory.max" in " ".join(step) for step in cgroup_steps(_launch()))
    assert not any(arg.startswith("--property=MemoryMax") for arg in unit_argv(_launch()))
