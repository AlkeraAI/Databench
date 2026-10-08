"""What the host can do for the chat sandbox, found out once per daemon.

The box is CONFIGURED to run one of two sandbox modes (``ALKERA_SANDBOX_MODE``:
``gvisor`` or ``none``). This probe answers what the host is CAPABLE of, so the
daemon can fail closed: a box set to ``gvisor`` on a host with no working
``runsc``, no staged rootfs or no network tools refuses every chat rather than
running it unsandboxed. It runs the real mechanisms rather than reading version
strings only: a ``systemd-run --scope`` of ``/bin/true`` proves the cgroup
driver, ``runsc --version`` (plus the binary being present) proves gVisor is
installed, an ``unshare --mount`` of ``/bin/true`` proves a private mount
namespace can be made, and the rootfs stamp proves the staging finished. The
per-chat uid and cgroup, kept in both modes, need root and a writable cgroup;
the probe reports those too, and what the default Python environment can be
made from, and which resolvers a container can be handed. The daemon says
which capability it found once in its log.

Every verdict here is an exit status, so the probe first makes sure it can
read one: a daemon whose ``SIGCHLD`` an ancestor left ignored sees every child
exit as success (:func:`alkera_core.process.reclaim_children`), so a container
with no systemd would report a working ``systemd-run``. The default
disposition is restored before anything runs; where it cannot be, nothing an
exit status would prove is reported.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from alkera_core.host_isolation import (
    LIMIT_FILES,
    Runner,
    cgroup_limits_missing,
    private_mount_namespace,
    run_status,
    systemd_scope,
)
from alkera_core.process import reclaim_children

from alkera_cli.harness.sandbox import ROOTFS_STAMP, CgroupDriver, SandboxSettings

logger = logging.getLogger(__name__)

Which = Callable[[str], str | None]
Reader = Callable[[str], str | None]

#: Where the host's resolvers are read from, most truthful first: the file
#: systemd-resolved keeps with the real upstream servers, then the classic
#: one, which on a resolved host names only the loopback stub a container
#: cannot reach.
RESOLV_CONF_PATHS: tuple[str, ...] = ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf")


@dataclass(frozen=True, slots=True)
class SandboxCapability:
    """What this host can do for the sandbox."""

    platform: str
    root: bool
    setpriv: str | None
    runsc: str | None
    """The ``runsc`` binary whose ``--version`` ran, or ``None``."""
    cgroup: CgroupDriver
    reason: str
    """One line saying what was found and why."""
    rootfs: str | None = None
    """The staged rootfs whose completion stamp was found, or ``None``."""
    ip: str | None = None
    nft: str | None = None
    unshare: str | None = None
    mount_ns: bool = False
    """Whether a private mount namespace can be made (root + a working
    ``unshare --mount``): what lets a ``none`` box bind the folder at the
    agent's home."""
    uv: str | None = None
    python_home: str | None = None
    """The managed interpreter directory whose ``bin/python3`` exists, or
    ``None``: what the default environment is made from."""
    resolvers: tuple[str, ...] = ()
    """The host's non-loopback nameservers, for a container's ``resolv.conf``."""
    setfacl: str | None = None
    """The ``setfacl`` that lends a chat's uid the traversal into its trees, or
    ``None``: without it the uid could not open its own folder."""
    exit_status: bool = True
    """Whether this daemon reads its children's exit status. ``False`` only
    when ``SIGCHLD`` is ignored and could not be restored: then no mechanism's
    verdict is real, and nothing an exit status would prove is reported."""

    @property
    def uid(self) -> bool:
        """Whether a chat can run as its own uid: root, a ``setpriv`` to drop to
        it, a ``setfacl`` to lend it its trees, and children whose exit status
        the steps that do it can read."""
        return (
            self.root and self.setpriv is not None and self.setfacl is not None and self.exit_status
        )

    @property
    def controls(self) -> bool:
        """Whether the per-chat controls apply on this host at all: the uid,
        which every other control hangs off — the trees are owned by it, the
        cgroup holds its processes, the alias is bound around its drop. The
        cgroup (:attr:`cgroup`) and the mount alias (:attr:`mount_ns`) are each
        applied where the host has them and skipped where it does not; a host
        without the uid runs the agent as the daemon (only sensible in dev /
        tests / a single-tenant box that does not need the controls)."""
        return self.uid

    @property
    def network(self) -> bool:
        """Whether the per-chat network namespace can be made and policed."""
        return self.ip is not None and self.nft is not None

    @property
    def gvisor(self) -> bool:
        """Whether the host can run the agent under gVisor: Linux, root, a
        working cgroup and uid, a ``runsc`` binary, a staged rootfs, and the
        tools that make the container's network. The true end-to-end (a real
        sandboxed container) is only provable on a Linux host with gVisor
        installed; this is the installed-and-usable check the daemon fails
        closed on."""
        return (
            self.uid
            and self.cgroup != "none"
            and self.runsc is not None
            and self.rootfs is not None
            and self.network
        )

    @property
    def default_env(self) -> bool:
        """Whether the chat's default Python environment can be made."""
        return self.uv is not None and self.python_home is not None


def _geteuid() -> int:
    getter = getattr(os, "geteuid", None)
    return getter() if getter is not None else -1


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


#: The limit files a chat's cgroup must offer for the launch to bound it.
CGROUP_LIMIT_FILES: tuple[str, ...] = LIMIT_FILES


def cgroupfs_holds_limits(root: str = "/sys/fs/cgroup") -> bool:
    """Whether a chat's cgroup can be made with its limits under the parent
    slice (:func:`alkera_core.host_isolation.cgroup_limits_missing`): a
    writable cgroup v2 whose controllers cannot be delegated to the slice is
    no cgroup for a chat."""
    return cgroup_limits_missing(Path(root) / "alkera.slice") is None


def host_resolvers(read: Reader = _read_text) -> tuple[str, ...]:
    """The nameservers a container can be handed: the ``nameserver`` lines of
    the first file in :data:`RESOLV_CONF_PATHS` that names any address outside
    the loopback range. A host-local stub (``127.0.0.53``) is dropped — the
    container's namespace has no route to the host's loopback — so a host that
    names only one hands the container nothing, and the launch refuses rather
    than starting an agent that cannot resolve its gateway."""
    for path in RESOLV_CONF_PATHS:
        text = read(path)
        if not text:
            continue
        found: list[str] = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] != "nameserver":
                continue
            try:
                address = ipaddress.ip_address(parts[1])
            except ValueError:
                continue
            if address.is_loopback or str(address) in found:
                continue
            found.append(str(address))
        if found:
            return tuple(found)
    return ()


def probe(
    *,
    which: Which = shutil.which,
    run: Runner = run_status,
    platform: str | None = None,
    euid: int | None = None,
    exists: Callable[[str], bool] = os.path.exists,
    writable: Callable[[str], bool] = lambda p: os.access(p, os.W_OK),
    read: Reader = _read_text,
    realpath: Callable[[str], str] = os.path.realpath,
    settings: SandboxSettings | None = None,
    reclaim: Callable[[], bool] = reclaim_children,
    cgroupfs_trial: Callable[[str], bool] = cgroupfs_holds_limits,
) -> SandboxCapability:
    """Find out what this host can do. Every collaborator is injectable so both
    modes are testable on any machine. ``settings`` names where the rootfs and
    the managed interpreter are expected (the box's environment when omitted).
    ``reclaim`` makes sure a child's exit status reaches this process before any
    mechanism is run, and says whether it does. ``cgroupfs_trial`` proves a
    writable cgroup v2 can hold a chat's limits before it is called a driver.

    The rootfs is reported at its real path: the prerequisites point a
    ``current`` link at the staged build, and runsc refuses to root a container
    at a link (its mount check opens the destination and finds the target),
    so the container is rooted at what the link names."""
    plat = sys.platform if platform is None else platform
    if plat != "linux":
        return SandboxCapability(plat, False, None, None, "none", f"no sandbox on {plat}")
    # First of all, whatever else the host lacks: every child this daemon runs
    # from here on — the probes below, useradd, the launch's steps, the agent
    # — must be its own to reap, or its verdict is a lie.
    exit_status = reclaim()
    box = settings if settings is not None else SandboxSettings.from_env()
    root = (_geteuid() if euid is None else euid) == 0
    setpriv = which("setpriv")
    setfacl = which("setfacl")
    uv = which("uv")
    python_home = box.python_home if exists(f"{box.python_home}/bin/python3") else None
    resolvers = host_resolvers(read)

    def without_controls(reason: str) -> SandboxCapability:
        return SandboxCapability(
            plat,
            root,
            setpriv,
            None,
            "none",
            reason,
            uv=uv,
            python_home=python_home,
            resolvers=resolvers,
            setfacl=setfacl,
            exit_status=exit_status,
        )

    if not root:
        return without_controls("the daemon is not root: no chat uid, no gVisor")
    if setpriv is None:
        return without_controls("no setpriv: no chat uid, no gVisor")
    if setfacl is None:
        return without_controls("no setfacl: no chat uid, no gVisor")
    if not exit_status:
        return without_controls(
            "the daemon cannot read its children's exit status (SIGCHLD is ignored and "
            "could not be restored): no mechanism can be verified, no chat uid, no gVisor"
        )

    cgroup: CgroupDriver = "none"
    undelegated = False
    systemd_run = which("systemd-run")
    if systemd_run is not None and systemd_scope(run, systemd_run):
        cgroup = "systemd"
    elif exists("/sys/fs/cgroup/cgroup.controllers") and writable("/sys/fs/cgroup"):
        if cgroupfs_trial("/sys/fs/cgroup"):
            cgroup = "cgroupfs"
        else:
            undelegated = True
    unshare = which("unshare")
    mount_ns = unshare is not None and private_mount_namespace(run, unshare)
    ip = which("ip")
    nft = which("nft")

    def found(
        runsc: str | None, cgroup: CgroupDriver, reason: str, *, rootfs: str | None = None
    ) -> SandboxCapability:
        return SandboxCapability(
            plat,
            True,
            setpriv,
            runsc,
            cgroup,
            reason,
            rootfs=rootfs,
            ip=ip,
            nft=nft,
            unshare=unshare,
            mount_ns=mount_ns,
            uv=uv,
            python_home=python_home,
            resolvers=resolvers,
            setfacl=setfacl,
        )

    if cgroup == "none":
        if undelegated:
            return found(
                None,
                "none",
                "cgroup v2 is writable but delegates no cpu/memory/pids controller to the "
                "parent slice, and no systemd: chat uid only, no gVisor",
            )
        return found(None, "none", "no writable cgroup v2 and no systemd: chat uid only, no gVisor")

    runsc = which("runsc")
    if runsc is None:
        return found(None, cgroup, f"no runsc: uid and {cgroup} cgroup only, mode none")
    if run((runsc, "--version")) != 0:
        return found(None, cgroup, "runsc present but --version failed: mode none only")
    real_rootfs = realpath(box.rootfs)
    staged = exists(f"{real_rootfs}/{ROOTFS_STAMP}") and exists(f"{real_rootfs}/bin/sh")
    rootfs = real_rootfs if staged else None
    missing: list[str] = []
    if rootfs is None:
        missing.append(f"no staged rootfs at {box.rootfs}")
    if ip is None or nft is None:
        missing.append("no ip/nft for the chat network")
    if missing:
        reason = f"runsc present but {'; '.join(missing)}: gVisor not ready"
    else:
        reason = f"runsc present, rootfs staged, {cgroup} cgroup, per-chat uid: gVisor ready"
    return found(runsc, cgroup, reason, rootfs=rootfs)


_CURRENT: SandboxCapability | None = None


def current_capability(*, prober: Callable[[], SandboxCapability] = probe) -> SandboxCapability:
    """The host's capability, found once per process and logged once."""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = prober()
        logger.info(
            "chat sandbox capability: gvisor=%s uid=%s cgroup=%s mount_ns=%s default_env=%s "
            "exit_status=%s (%s)",
            _CURRENT.gvisor,
            _CURRENT.uid,
            _CURRENT.cgroup,
            _CURRENT.mount_ns,
            _CURRENT.default_env,
            _CURRENT.exit_status,
            _CURRENT.reason,
        )
    return _CURRENT


def reset_probe_for_tests() -> None:
    global _CURRENT
    _CURRENT = None


__all__ = [
    "CGROUP_LIMIT_FILES",
    "RESOLV_CONF_PATHS",
    "SandboxCapability",
    "cgroupfs_holds_limits",
    "current_capability",
    "host_resolvers",
    "probe",
    "reset_probe_for_tests",
]
