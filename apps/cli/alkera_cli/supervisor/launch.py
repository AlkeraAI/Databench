"""How the supervisor starts one org's worker, composed as data.

Which launch a worker gets is chosen from the box's probed isolation profile
by :func:`plan_launch`, never assumed. Under
``org_namespaces`` the worker is started as a chain of ``exec``s, so it keeps
the pid the supervisor spawned and nothing of the supervisor stays behind it:

1. ``sh``, still root on the host, joins the org's cgroup (made and delegated
   to the org's base uid by :func:`cgroup_steps`), then
2. ``unshare --mount`` gives it a private mount namespace in which, still as
   root, every mount is made read-only, the org's root alone is bound back
   read-write, ``/tmp`` and ``/dev/shm`` become private tmpfs, and ``/proc``
   is mounted with ``hidepid=invisible`` (another org's processes do not
   exist for it); a mount still writable after that is refused
   (:data:`RW_CHECK`); then
3. ``unshare --user --mount --net --cgroup`` maps the namespace's ids
   ``0..ORG_UID_SPAN`` to the slot's host range and becomes its uid 0, which
   on the host is the slot's base uid and nothing more. The mounts made in
   step 2 are locked under the new user namespace: the worker cannot undo
   them. The worker's network namespace is empty until :func:`network_steps`
   gives it a link to the host.

On a systemd host the chain runs as a transient unit of its own,
``alkera-org-<slot>.service`` under ``alkera-orgs.slice`` (:func:`unit_argv`):
systemd delegates the unit's cgroup (which step 1 hands to the slot's base
uid in place of the cgroup this module would make), and the unit's own
hardening (:data:`UNIT_PROPERTIES`) holds under everything above. Under
``single_org`` the worker is spawned on its own. The socketpair reaches the
worker as its standard input either way.

Everything here is pure; :mod:`alkera_cli.supervisor.service` runs it. The
golden tests pin the argv.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from alkera_core.compute.box_isolation import ENV_ORG_ISOLATION, IsolationProfile, IsolationReport

from alkera_cli.supervisor.slots import ORG_UID_SPAN, Slot

#: Where the org cgroups hang, under the cgroup root.
ORGS_CGROUP: Final = "alkera.orgs"
#: The controllers an org's worker may set limits with in its subtree.
ORG_CONTROLLERS: Final = ("cpu", "cpuset", "io", "memory", "pids")
#: How a worker learns the profile it was started under: only one started in
#: namespaces of its own has any to prepare.
#: The files a delegated cgroup's owner must hold to manage its subtree.
DELEGATED_FILES: Final = ("cgroup.procs", "cgroup.subtree_control", "cgroup.threads")

#: Step 1: join the org's cgroup, then the private mount namespace. ``$1`` the
#: cgroup's ``cgroup.procs``; the rest is step 2's arguments. Positional, never
#: interpolated.
_JOIN_SHELL = 'echo $$ > "$1" || exit 90; shift; exec "$@"'

#: Step 2, as root in the new mount namespace. ``$1`` the org root, ``$2`` the
#: slot's base uid, ``$3`` the span, ``$4`` unshare; the rest is the worker's
#: argv. Every mount read-only, deepest first (a mount point ``mountinfo``
#: escapes is unescaped with ``printf %b``), except ``/proc`` (replaced below)
#: and ``/sys``: the worker mounts its own and a tool in its namespace (``ip
#: -n``) mounts another, which a read-only lock on the host's would refuse;
#: nothing in the host's is writable by an unmapped uid anyway. Then the few
#: mounts that are the org's.
#: Then no mount but the org's root, ``/tmp``, ``/dev/shm``, ``/proc`` and
#: ``/sys`` may still be writable (the topmost mount at each point, the one a
#: path reaches, in ``$mi``): a remount the kernel refused would leave a path
#: other orgs share writable, so the worker is refused (96) instead.
RW_CHECK: Final = (
    'left="$(awk \'{o[$5]=$6} END {for (m in o) if (o[m] ~ /(^|,)rw(,|$)/) print m}\' "$mi" '
    '| while read -r m; do m="$(printf %b "$m")"; case "$m" in '
    '("$root"|"$real"|/tmp|/dev/shm|/proc|/proc/*|/sys|/sys/*) ;; (*) echo "$m" ;; esac; '
    'done)"; [ -z "$left" ] || { echo "writable after hardening: $left" >&2; exit 96; }; '
)
HARDEN_SHELL: Final = (
    'root="$1"; base="$2"; span="$3"; unshare="$4"; shift 4; '
    "awk '{print $5}' /proc/self/mountinfo | sort -r | while read -r m; do "
    'case "$m" in /proc|/proc/*|/sys|/sys/*) continue ;; esac; '
    'mount -o remount,bind,ro "$(printf %b "$m")" 2>/dev/null; done; '
    'mount --bind "$root" "$root" && mount -o remount,bind,rw "$root" || exit 91; '
    "mount -t tmpfs -o nosuid,nodev,mode=1777,size=256m tmpfs /tmp || exit 92; "
    "mount -t tmpfs -o nosuid,nodev,mode=1777,size=64m tmpfs /dev/shm || exit 93; "
    "mount -t proc -o nosuid,nodev,noexec,hidepid=invisible proc /proc || exit 94; "
    'mi=/proc/self/mountinfo; real="$(readlink -f "$root")"; '
    + RW_CHECK
    + 'exec "$unshare" --user --map-users="0:$base:$span" --map-groups="0:$base:$span" '
    '--setuid 0 --setgid 0 --mount --net --cgroup -- "$@"'
)


#: Before a worker starts, whatever an earlier one left in the org's cgroup
#: goes: every process in it is killed, its child cgroups removed and its
#: controllers taken back, so the new worker can join it (a cgroup handing
#: controllers to children cannot take a process). ``$1`` the org cgroup.
RESET_SHELL: Final = (
    'cg="$1"; [ -d "$cg" ] || exit 0; echo 1 > "$cg/cgroup.kill" 2>/dev/null; '
    "for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do "
    'find "$cg" -mindepth 1 -depth -type d -exec rmdir {} + 2>/dev/null; '
    '[ -z "$(find "$cg" -mindepth 1 -type d)" ] && [ -z "$(cat "$cg/cgroup.procs")" ] && break; '
    "sleep 0.2; done; "
    'for c in $(cat "$cg/cgroup.subtree_control"); do '
    'echo "-$c" > "$cg/cgroup.subtree_control"; done; '
    '[ -z "$(cat "$cg/cgroup.subtree_control")" ]'
)


#: Step 1 on a systemd host: the unit's own cgroup, delegated by systemd, is
#: handed to the slot's base uid (``$1``) so the worker can manage its subtree
#: from inside its user namespace; then step 2.
_DELEGATE_SHELL = (
    'cg="/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)"; '
    'chown "$1:$1" "$cg" "$cg/cgroup.procs" "$cg/cgroup.subtree_control" '
    '"$cg/cgroup.threads" || exit 95; shift; exec "$@"'
)
#: The slice every org's unit runs under.
ORGS_SLICE: Final = "alkera-orgs.slice"
#: How an org's unit is hardened: on top of the namespaces the chain makes,
#: systemd itself keeps the unit's view of the host read-only but for the org's
#: root, its /tmp and devices private, its /proc showing no process it may not
#: trace, the kernel's modules, log and clock out of reach, and no privilege
#: gained on exec (so a set-id bit means nothing). Not RestrictSUIDSGID: its
#: seccomp filter answers openat2 with ENOSYS, and the folder code resolves
#: every path beneath a chat's root with openat2. Not ProtectKernelTunables: it locks /sys
#: read-only, and the worker mounts a sysfs of its own network namespace (and
#: ``ip -n`` another), which a locked read-only one refuses; an unmapped uid
#: can write nothing in the host's /sys anyway. Its cgroup is delegated with the
#: controllers the chats are limited by; each chat's own cgroup bounds its
#: tasks, so the unit's are not capped twice.
UNIT_PROPERTIES: Final = (
    "Delegate=cpu cpuset io memory pids",
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "PrivateTmp=yes",
    "PrivateDevices=yes",
    "ProtectProc=invisible",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectClock=yes",
    "ProtectHostname=yes",
    "LockPersonality=yes",
    "NoNewPrivileges=yes",
    "KeyringMode=private",
    "TasksMax=infinity",
    "CPUWeight=100",
)


@dataclass(frozen=True, slots=True)
class WorkerLaunch:
    """One org worker's start: the steps before the spawn, the argv, its
    environment, and the link to make once its network namespace exists."""

    slot: Slot
    org_root: Path
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cgroup_root: Path = Path("/sys/fs/cgroup")
    unshare: str = "unshare"
    ip: str = "ip"
    #: How long systemd waits for the worker to drain when it stops the unit
    #: itself (the box shutting down) before it kills it: past the worker's
    #: own drain ceiling and the margin it ends itself in.
    stop_timeout_seconds: int = 0
    #: The org's memory, its worker and chats together (``memory.max``);
    #: ``0`` sets none.
    memory_max_bytes: int = 0

    @property
    def unit(self) -> str:
        """The worker's unit on a systemd host."""
        return f"alkera-org-{self.slot.index}.service"

    @property
    def cgroup(self) -> Path:
        return self.cgroup_root / ORGS_CGROUP / f"org-{self.slot.index}"


def cgroup_steps(launch: WorkerLaunch) -> tuple[tuple[str, ...], ...]:
    """Make the org's cgroup under :data:`ORGS_CGROUP` with the controllers its
    worker limits chats with delegated to it, and hand it to the slot's base
    uid: the directory and the files a delegate writes, nothing above it. Each
    is an argv run as root before the spawn; a failure refuses the worker."""
    root = launch.cgroup_root
    parent = root / ORGS_CGROUP
    enable = " ".join(f"+{name}" for name in ORG_CONTROLLERS)
    owner = f"{launch.slot.uid_base}:{launch.slot.uid_base}"
    org = launch.cgroup
    return (
        ("/bin/sh", "-c", RESET_SHELL, "reset", str(org)),
        ("mkdir", "-p", str(org)),
        (
            "/bin/sh",
            "-c",
            'echo "$1" > "$2"',
            "enable",
            enable,
            str(root / "cgroup.subtree_control"),
        ),
        (
            "/bin/sh",
            "-c",
            'echo "$1" > "$2"',
            "enable",
            enable,
            str(parent / "cgroup.subtree_control"),
        ),
        *_memory_limit(launch),
        ("chown", owner, str(org), *(str(org / name) for name in DELEGATED_FILES)),
    )


def _memory_limit(launch: WorkerLaunch) -> tuple[tuple[str, ...], ...]:
    """Write the org's ``memory.max`` while root still owns it: the slot is
    handed the cgroup's delegated files, never its limits."""
    return memory_steps(launch, launch.memory_max_bytes, systemd=False)


def memory_steps(launch: WorkerLaunch, limit: int, *, systemd: bool) -> tuple[tuple[str, ...], ...]:
    """Set a worker's ``memory.max`` to ``limit`` (none when ``limit`` is 0):
    its unit's ``MemoryMax`` on systemd, else its cgroup's file."""
    if limit <= 0:
        return ()
    if systemd:
        return (("systemctl", "set-property", "--runtime", launch.unit, f"MemoryMax={limit}"),)
    target = str(launch.cgroup / "memory.max")
    return (("/bin/sh", "-c", 'echo "$1" > "$2"', "limit", str(limit), target),)


def _harden_argv(launch: WorkerLaunch) -> tuple[str, ...]:
    """Steps 2 and 3: the private mount namespace, then the user namespace."""
    return (
        launch.unshare,
        "--mount",
        "--propagation",
        "private",
        "--",
        "/bin/sh",
        "-c",
        HARDEN_SHELL,
        "alkera-org-harden",
        str(launch.org_root),
        str(launch.slot.uid_base),
        str(ORG_UID_SPAN),
        launch.unshare,
        *launch.argv,
    )


def spawn_argv(launch: WorkerLaunch) -> tuple[str, ...]:
    """The argv the supervisor spawns where there is no systemd: the join, the
    hardening, the user namespace, then the worker itself."""
    return (
        "/bin/sh",
        "-c",
        _JOIN_SHELL,
        "alkera-org-join",
        str(launch.cgroup / "cgroup.procs"),
        *_harden_argv(launch),
    )


def unit_argv(launch: WorkerLaunch, *, systemd_run: str = "systemd-run") -> tuple[str, ...]:
    """The argv the supervisor spawns on a systemd host: the same chain, as
    the transient unit :attr:`WorkerLaunch.unit`, hardened by
    :data:`UNIT_PROPERTIES` and able to write its own root alone.
    ``--pipe`` hands the unit this process's standard streams, so the
    socketpair on standard input reaches the worker and no path does."""
    return (
        systemd_run,
        "--pipe",
        "--quiet",
        "--collect",
        f"--unit={launch.unit}",
        f"--slice={ORGS_SLICE}",
        *(f"--property={prop}" for prop in UNIT_PROPERTIES),
        f"--property=ReadWritePaths={launch.org_root}",
        *(
            (f"--property=MemoryMax={launch.memory_max_bytes}",)
            if launch.memory_max_bytes > 0
            else ()
        ),
        *_stop_properties(launch),
        *(f"--setenv={name}={value}" for name, value in sorted(launch.env.items())),
        "--",
        "/bin/sh",
        "-c",
        _DELEGATE_SHELL,
        "alkera-org-delegate",
        str(launch.slot.uid_base),
        *_harden_argv(launch),
    )


def _stop_properties(launch: WorkerLaunch) -> tuple[str, ...]:
    """How systemd stops the unit when it does so itself: the worker alone is
    signalled (a drain of its chats, which its agent servers must outlive),
    the network stays up until it is done (units stop in reverse of their
    start order), and the kill waits past the worker's own bound."""
    props: tuple[str, ...] = (
        "KillMode=mixed",
        "After=network-online.target",
        "Wants=network-online.target",
    )
    if launch.stop_timeout_seconds > 0:
        props += (f"TimeoutStopSec={launch.stop_timeout_seconds}",)
    return tuple(f"--property={prop}" for prop in props)


def network_steps(launch: WorkerLaunch, pid: int) -> tuple[tuple[str, ...], ...]:
    """Give the worker (``pid``, already in its own network namespace) its one
    link: a veth pair with the host end addressed and up, the other end moved
    into the worker's namespace as ``eth0``, which the worker addresses
    itself. A link left by a worker that crashed is removed first. What the
    link may reach (out through NAT; never the host, another org or the
    metadata service) is the box ruleset ``sandbox-prereqs.sh`` loads.

    That is all a worker needs in production: what it talks to (the API, the
    gateway, the drive's content host) is off the box, so its traffic leaves
    by the forward path and the box's NAT, and nothing on the host is opened
    to it. A rig that serves the API from the box itself has to open that one
    port by hand; ``org-isolation-sweep.sh`` checks both halves on a box."""
    slot = launch.slot
    return (
        (launch.ip, "link", "delete", slot.host_if),
        (
            launch.ip,
            "link",
            "add",
            slot.host_if,
            "type",
            "veth",
            "peer",
            "name",
            "eth0",
            "netns",
            str(pid),
        ),
        (launch.ip, "addr", "add", f"{slot.host_ip}/30", "dev", slot.host_if),
        (launch.ip, "link", "set", slot.host_if, "up"),
    )


def worker_env(
    launch_root: Path,
    slot: Slot,
    *,
    passthrough: Mapping[str, str],
    names: Sequence[str],
    profile: IsolationProfile,
) -> dict[str, str]:
    """The worker's environment: the deployment settings it needs (``names``
    picked from ``passthrough``, never the machine credential, which travels
    on the socketpair), its own homes, all beneath its root, and the profile
    it was started under (whether it has namespaces to prepare)."""
    env = {name: passthrough[name] for name in names if name in passthrough}
    home = str(launch_root / "home")
    env.update(
        {
            "PATH": passthrough.get(
                "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ),
            "HOME": home,
            "ALKERA_HOME": home,
            "TMPDIR": str(launch_root / "tmp"),
            "ALKERA_SANDBOX_UID_LEDGER": f"{home}/sandbox-uids.json",
            "ALKERA_SANDBOX_TREES_FLOOR": str(launch_root),
            "ALKERA_ORG_WORKER_IP": slot.worker_ip,
            "ALKERA_ORG_HOST_IP": slot.host_ip,
            ENV_ORG_ISOLATION: profile.value,
        }
    )
    env.pop("ALKERA_MACHINE_CREDENTIAL", None)
    return env


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """How one worker is started under the box's isolation profile."""

    profile: IsolationProfile
    argv: tuple[str, ...]
    #: The spawn's environment, or ``None`` where the unit carries it.
    env: Mapping[str, str] | None
    #: Run as root before the spawn, in order; a failure refuses the worker.
    before: tuple[tuple[str, ...], ...] = ()
    #: The systemd unit the worker runs as, or ``None``.
    unit: str | None = None

    @property
    def linked(self) -> bool:
        """Whether the worker has a network namespace to be linked."""
        return self.profile == IsolationProfile.ORG_NAMESPACES


def plan_launch(launch: WorkerLaunch, isolation: IsolationReport) -> LaunchPlan:
    """The launch ``isolation`` allows: the namespaced chain (a unit on
    systemd) only where the probe proved every mechanism it uses, else the
    worker on its own. Nothing else reaches the cgroup and namespace builders."""
    if isolation.profile != IsolationProfile.ORG_NAMESPACES:
        return LaunchPlan(IsolationProfile.SINGLE_ORG, launch.argv, dict(launch.env))
    if isolation.units:
        return LaunchPlan(isolation.profile, unit_argv(launch), None, unit=launch.unit)
    return LaunchPlan(
        isolation.profile, spawn_argv(launch), dict(launch.env), before=cgroup_steps(launch)
    )


__all__ = [
    "DELEGATED_FILES",
    "ENV_ORG_ISOLATION",
    "HARDEN_SHELL",
    "ORGS_CGROUP",
    "ORGS_SLICE",
    "ORG_CONTROLLERS",
    "RESET_SHELL",
    "RW_CHECK",
    "UNIT_PROPERTIES",
    "LaunchPlan",
    "WorkerLaunch",
    "cgroup_steps",
    "memory_steps",
    "network_steps",
    "plan_launch",
    "spawn_argv",
    "unit_argv",
    "worker_env",
]
