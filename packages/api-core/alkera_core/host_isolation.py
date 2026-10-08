"""What this host lets a root process do to keep other processes apart, found
out by doing it.

Hosts differ: an EC2 instance is a whole kernel the box is root on, while a
RunPod pod is a container whose root cgroup holds the container's own
processes (the kernel refuses to hand its controllers down), whose
capabilities leave out ``CAP_SYS_ADMIN`` and whose ``unshare`` is too old to
map an id range. So nothing here reads a version string or trusts a path that
merely exists. Each trial does the thing on a throwaway object, the way its
caller will: a cgroup is made and must offer its limit files, an ``unshare`` of
``/bin/true`` makes each namespace, a veth pair is added and deleted.

Two callers share the trials, so one host fact has one answer: the chat
sandbox (``alkera_cli.harness.sandbox_probe``), which needs a cgroup and a
mount namespace per chat, and the box supervisor, which asks :func:`probe`
for everything an org worker's launch uses and gets the box's
:class:`~alkera_core.compute.box_isolation.IsolationReport`.

Every verdict is an exit status, and every child goes through
:mod:`alkera_core.process`, which makes sure this process can read one: where
an ancestor left ``SIGCHLD`` ignored, every child reads as a success, which is
how a container with no systemd once reported a working ``systemd-run``.

Every collaborator is injectable, so a test can be any host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport
from alkera_core.process import SpawnSpec, run

Runner = Callable[[Sequence[str]], int]
Writer = Callable[[Path, str], None]

#: The limit files a cgroup offers once its controllers are enabled above it.
LIMIT_FILES: Final = ("cpu.max", "memory.max", "pids.max")
#: How long one trial's child may take.
TRIAL_SECONDS: Final = 20.0


def run_status(argv: Sequence[str]) -> int:
    """The exit status of ``argv``, or 127 when it could not be run."""
    try:
        spec = SpawnSpec(argv=list(argv), env=os.environ, stdout="pipe", stderr="pipe")
        return run(spec, timeout=TRIAL_SECONDS).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def on_systemd() -> bool:
    """Whether this host runs systemd as its init and offers ``systemd-run``."""
    return Path("/run/systemd/system").is_dir() and shutil.which("systemd-run") is not None


def systemd_scope(run: Runner, systemd_run: str = "systemd-run", *, delegate: bool = False) -> bool:
    """Whether systemd starts a transient scope here (``delegate``: one whose
    cgroup it hands to the scope's own processes)."""
    properties = ("--property=Delegate=yes",) if delegate else ()
    return run((systemd_run, "--scope", "--quiet", *properties, "--", "/bin/true")) == 0


def cgroup_limits_missing(
    parent: Path, *, pid: int | None = None, enable: Callable[[], str | None] | None = None
) -> str | None:
    """Why a cgroup made under ``parent`` cannot be limited, or ``None`` when
    it can: a throwaway group is made there and must offer
    :data:`LIMIT_FILES`, which it does only when the controllers were enabled
    for ``parent``'s children (``enable`` does that first where the caller
    hands them down itself, and answers why it could not). A writable cgroup
    v2 whose controllers cannot be handed down (a container whose root group
    holds the container's own processes) offers none. The group is removed
    again."""
    trial = parent / f"probe-{os.getpid() if pid is None else pid}"
    try:
        os.makedirs(trial, exist_ok=True)
    except OSError as exc:
        return f"no cgroup could be made under {parent}: {exc}"
    try:
        if enable is not None and (refused := enable()) is not None:
            return refused
        absent = [name for name in LIMIT_FILES if not (trial / name).exists()]
        return f"a cgroup under {parent} offers no {', '.join(absent)}" if absent else None
    finally:
        try:
            trial.rmdir()
        except OSError:
            pass


def private_mount_namespace(run: Runner, unshare: str) -> bool:
    """Whether a private mount namespace can be made (``CAP_SYS_ADMIN``)."""
    return run((unshare, "--mount", "--propagation", "private", "--", "/bin/true")) == 0


def mapped_user_namespace(run: Runner, unshare: str, *, uid_base: int) -> bool:
    """Whether a user namespace whose uid 0 is the host's ``uid_base`` can be
    made (the right to create one, and util-linux 2.38 for ``--map-users``)."""
    ids = f"0:{uid_base}:1"
    argv = (unshare, "--user", f"--map-users={ids}", f"--map-groups={ids}")
    return run((*argv, "--setuid", "0", "--setgid", "0", "--", "/bin/true")) == 0


def linked_network_namespace(run: Runner, unshare: str, ip: str, *, tag: int) -> str | None:
    """Why a network namespace with a veth link to the host cannot be made
    (``CAP_NET_ADMIN``), or ``None`` when it can."""
    if run((unshare, "--net", "--", "/bin/true")) != 0:
        return "a network namespace is not permitted"
    name = f"alkp{tag % 100_000}"
    if run((ip, "link", "add", name, "type", "veth", "peer", "name", f"{name}b")) != 0:
        return "a veth link cannot be made"
    run((ip, "link", "delete", name))
    return None


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


@dataclass(frozen=True, slots=True)
class Host:
    """The primitives :func:`probe` tries mechanisms with."""

    run: Runner = run_status
    write: Writer = _write
    which: Callable[[str], str | None] = shutil.which
    systemd: Callable[[], bool] = on_systemd
    root: bool = field(default_factory=lambda: getattr(os, "geteuid", lambda: -1)() == 0)
    cgroup_root: Path = Path("/sys/fs/cgroup")
    pid: int = field(default_factory=os.getpid)


def probe(
    host: Host | None = None, *, cgroup: str, controllers: Sequence[str], uid_base: int
) -> IsolationReport:
    """Try every :class:`IsolationMechanism` on this host and report what
    worked. ``cgroup`` is where the caller's cgroups hang under the cgroup
    root, ``controllers`` what each of them must be able to limit, and
    ``uid_base`` a host id its user namespaces map from."""
    host = host or Host()
    if not host.root:
        reason = "the process is not root"
        return IsolationReport(frozenset(), {m: reason for m in IsolationMechanism})
    systemd = host.systemd()
    unshare, ip = host.which("unshare"), host.which("ip")
    no_unshare = None if unshare is not None else "no unshare"
    why_not: dict[IsolationMechanism, str | None] = {
        IsolationMechanism.SYSTEMD: None if systemd else "the host's init is not systemd",
        IsolationMechanism.CGROUP_DELEGATION: _delegation(host, cgroup, controllers, systemd),
        IsolationMechanism.MOUNT_NAMESPACE: no_unshare,
        IsolationMechanism.USER_NAMESPACE: no_unshare,
        IsolationMechanism.NETWORK_NAMESPACE: no_unshare if ip is not None else "no ip",
    }
    if unshare is not None:
        if not private_mount_namespace(host.run, unshare):
            why_not[IsolationMechanism.MOUNT_NAMESPACE] = "a mount namespace is not permitted"
        if not mapped_user_namespace(host.run, unshare, uid_base=uid_base):
            why_not[IsolationMechanism.USER_NAMESPACE] = (
                "a user namespace mapping a host id range is not permitted "
                "(or unshare predates 2.38)"
            )
        if ip is not None:
            why_not[IsolationMechanism.NETWORK_NAMESPACE] = linked_network_namespace(
                host.run, unshare, ip, tag=host.pid
            )
    return IsolationReport(
        frozenset(m for m, why in why_not.items() if why is None),
        {m: why for m, why in why_not.items() if why is not None},
    )


def _delegation(host: Host, cgroup: str, controllers: Sequence[str], systemd: bool) -> str | None:
    """Delegation the way a launch makes it: systemd delegating a scope, or
    the controllers enabled at the root and under ``cgroup`` so a child of it
    offers the limit files."""
    if systemd:
        delegated = systemd_scope(host.run, delegate=True)
        return None if delegated else "systemd would not delegate a scope"
    parent = host.cgroup_root / cgroup
    wanted = " ".join(f"+{name}" for name in controllers)

    def enable() -> str | None:
        for target in (host.cgroup_root, parent):
            try:
                host.write(target / "cgroup.subtree_control", wanted)
            except OSError as exc:
                return f"{target} would not hand its controllers down: {exc}"
        return None

    return cgroup_limits_missing(parent, pid=host.pid, enable=enable)


__all__ = [
    "LIMIT_FILES",
    "TRIAL_SECONDS",
    "Host",
    "Runner",
    "cgroup_limits_missing",
    "linked_network_namespace",
    "mapped_user_namespace",
    "on_systemd",
    "private_mount_namespace",
    "probe",
    "run_status",
    "systemd_scope",
]
