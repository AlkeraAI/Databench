"""The per-chat sandbox the harness puts in front of the agent server.

A cloud chat's agent server is a child of a daemon that runs as root. Without
this module every chat on a box would share one user, one view of the disk and
the whole machine's CPU and memory. This module composes the launch that bounds
it, as plain data.

A node runs in one of two sandbox states:

* ``gvisor``: the agent runs under gVisor (``runsc``), a user-space kernel that
  intercepts the workload's syscalls. This is a real boundary against untrusted
  code and is required on a multi-tenant node. The container roots at a rootfs
  the box staged at first boot (verified by digest), sees the chat's folder at
  ``/home/alkera`` and the agent server's own trees under ``/opt/alkera``, and
  has its own network namespace. A veth pair links it to the host with NAT out;
  only the daemon's tool server is reachable on the host end, and the metadata
  service and every other chat's namespace are not.
* ``none``: no sandbox boundary. Allowed only on a single-tenant node. macOS,
  the tests and a host without the capabilities also run here.

The per-chat Unix uid and cgroup are kept in both states. They control file
ownership and CPU/memory, not isolation; the boundary itself is ``runsc`` or
nothing.

:class:`GvisorRuntime` and :class:`NoneRuntime` implement the
:class:`SandboxRuntime` seam, so the spawn path does not care which is active.
Each turns a :class:`SandboxSpec` (one chat's folder, uid and limits) into a
:class:`SandboxLaunch` (the command, the environment, and the steps to run
before the spawn, after it with the pid, and after the exit). Composition is
pure and pinned by golden tests; :func:`run_steps` is the one place a step is
executed, with the runner injectable.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from alkera_core.sandbox_tiers import DEDICATED_GUARD, POOL_FLOOR

from alkera_cli.harness.sandbox_env import (
    env_exit_steps,
    env_steps,
    session_default_env,
    session_envs_dir,
)
from alkera_cli.harness.sandbox_explain import explain_sandbox, wrapper_argv
from alkera_cli.harness.sandbox_firewall import (
    DEFAULT_CHAT_NET,
    METADATA_IPV4,
    METADATA_IPV6,
    NFT_CHAT_PORTS_SET,
    NFT_TABLE,
    VETH_PREFIX,
    chat_network_rules,
    metadata_block_rules,
)
from alkera_cli.harness.sandbox_layout import (
    AGENT_DATA_SUBDIR,
    AGENT_MOUNT,
    CONTAINER_ENV,
    CONTAINER_TMP,
    DEFAULT_ENV_NAME,
    ENVS_MOUNT,
    ENVS_SUBDIR,
    HARNESS_CONFIG_MOUNT,
    HARNESS_DATA_MOUNT,
    HARNESS_MOUNT,
    HARNESS_STATE_MOUNT,
    INTERNAL_PREFIX,
    RELOCATE_SOURCE,
    RIPGREP_MOUNT,
    RUNTIME_STATE_SUBDIR,
    Bind,
    BindKind,
    agent_view,
    chat_binds,
    chat_cgroup_dir,
    chat_cgroup_path,
    chat_container,
    chat_netns,
    chat_slice,
    chat_slice_cgroup_path,
    chat_slug,
    chat_user,
    command_tmp_dir,
    default_env_path,
    host_path,
    legacy_mountpoint_argv,
    python_ro_binds,
    relative_to_container,
    relocate_argv,
    tool_binds,
)
from alkera_cli.harness.sandbox_memory import (
    SIGKILL_EXIT_STATUSES,
    memory_events_path,
    memory_limit_detail,
    memory_limit_exceeded,
    oom_kills,
    read_oom_kills,
    read_oom_kills_under,
)
from alkera_cli.harness.sandbox_ownership import (
    OWNED_MKDIR_SHELL,
    SHARED_TREE_UMASK,
    host_steps_checked,
    read_access_steps,
    spec_umask,
    uid_prefix,
    uid_steps,
    umask_prefix,
)
from alkera_cli.harness.sandbox_steps import (
    RepairStep,
    SandboxRefusedError,
    ShellStep,
    Step,
    WriteStep,
    argv_text,
    run_steps,
)
from alkera_cli.harness.sandbox_uid import (
    UID_MAX,
    UID_MIN,
    TreeIdentity,
    chat_identity,
    ensure_chat_uid,
    owner_session,
    useradd_argv,
)

if TYPE_CHECKING:  # the probe imports this module at runtime
    from alkera_cli.harness.sandbox_probe import SandboxCapability

logger = logging.getLogger(__name__)

#: The two states a node's sandbox can be in. ``gvisor`` is the real boundary
#: the multi-tenant pool requires; ``none`` is a single-tenant box or dev.
SandboxMode = Literal["gvisor", "none"]
CgroupDriver = Literal["systemd", "cgroupfs", "none"]

SANDBOX_MODES: tuple[SandboxMode, ...] = ("gvisor", "none")

ENV_MODE = "ALKERA_SANDBOX_MODE"
ENV_HOME = "ALKERA_SANDBOX_HOME"
ENV_DEDICATED = "ALKERA_SANDBOX_DEDICATED"
ENV_ROOTFS = "ALKERA_SANDBOX_ROOTFS"
ENV_PYTHON = "ALKERA_SANDBOX_PYTHON"
ENV_NET = "ALKERA_SANDBOX_NET"
ENV_POOL_VCPU = "SANDBOX_POOL_VCPU"
ENV_POOL_MEMORY_MB = "SANDBOX_POOL_MEMORY_MB"
ENV_DEDICATED_VCPU = "SANDBOX_DEDICATED_VCPU"
ENV_DEDICATED_MEMORY_MB = "SANDBOX_DEDICATED_MEMORY_MB"

DEFAULT_HOME = "/home/alkera"
#: The staged rootfs a gVisor container roots at: ``sandbox-prereqs.sh`` builds
#: it under ``/opt/alkera/rootfs/<recipe digest>`` and points ``current`` at it.
DEFAULT_ROOTFS = "/opt/alkera/rootfs/current"
#: The interpreter the default environment is made from, installed by the
#: prerequisites on the host AND at the same path inside the rootfs, so a venv
#: made on the host resolves inside the container.
DEFAULT_PYTHON_HOME = "/opt/alkera/python/current"
#: The box's default for a chat whose row names no limit: the smallest plan
#: tier on a pool box, a large leak guard on a dedicated one. The pool floor
#: is the free tier from the server's own table, so the two ends agree.
DEFAULT_POOL_VCPU, DEFAULT_POOL_MEMORY_MB = POOL_FLOOR
DEFAULT_DEDICATED_VCPU, DEFAULT_DEDICATED_MEMORY_MB = DEDICATED_GUARD


#: Tasks (threads + processes) one chat may hold. Enough for an agent server,
#: its tool subprocesses and a build; a fork bomb stops here.
TASKS_MAX = 512

#: The file the prerequisites write at the top of a staged rootfs once it is
#: complete, holding the recipe digest it was built from. A rootfs without it
#: is a half-built one and is never used.
ROOTFS_STAMP = ".alkera-rootfs"
#: Where ``ip netns`` keeps a named network namespace.
NETNS_DIR = "/run/netns"

#: Names an agent process must never inherit inside the container: they
#: describe the DAEMON's host environment (the reverse diff its shell tool
#: applies to restore it, the directory the daemon happened to be started in,
#: the box's own hostname and the daemon shell's bookkeeping), which means
#: nothing where the host environment is not there. The adapter spells the
#: first name; a test pins the two together.
HOST_ONLY_ENV: tuple[str, ...] = (
    "ALKERA_SHELL_ENV_RESTORE",
    "PWD",
    "OLDPWD",
    "HOSTNAME",
    "SHLVL",
    "_",
)

#: The container's own PATH. The default environment and the agent's tool
#: directories go in front of it.
_CONTAINER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    """Unset, blank, unparseable and non-positive all read as ``default`` so a
    typo can never remove a limit."""
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_mode(env: Mapping[str, str], name: str, default: SandboxMode) -> SandboxMode:
    """The box's sandbox mode from the environment. Only the two known words are
    honoured; anything else (blank, a typo, the old level names) reads as the
    default rather than a silent downgrade to ``none``."""
    raw = env.get(name, "").strip().lower()
    return "gvisor" if raw == "gvisor" else "none" if raw == "none" else default


def _env_abs_path(env: Mapping[str, str], name: str, default: str) -> str:
    """An absolute path from the environment; a relative or blank value is the
    default, since neither names one place on the box."""
    raw = env.get(name, "").strip()
    return raw if raw.startswith("/") else default


def chat_network_block(raw: str) -> ipaddress.IPv4Network:
    """The address block chats are carved from: an IPv4 network wide enough for
    a /30 per uid in the reserved range. Anything else reads as the default
    rather than as a block the range would overflow."""
    try:
        block = ipaddress.ip_network(raw.strip(), strict=True)
    except ValueError:
        block = ipaddress.ip_network(DEFAULT_CHAT_NET)
    needed = (UID_MAX - UID_MIN + 1) * 4
    if not isinstance(block, ipaddress.IPv4Network) or block.num_addresses < needed:
        block = ipaddress.ip_network(DEFAULT_CHAT_NET)
    assert isinstance(block, ipaddress.IPv4Network)
    return block


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    """Everything the launcher reads from the box, passed in — never a global."""

    mode: SandboxMode = "none"
    home: str = DEFAULT_HOME
    dedicated: bool = False
    rootfs: str = DEFAULT_ROOTFS
    python_home: str = DEFAULT_PYTHON_HOME
    chat_net: str = DEFAULT_CHAT_NET
    pool_vcpu: int = DEFAULT_POOL_VCPU
    pool_memory_mb: int = DEFAULT_POOL_MEMORY_MB
    dedicated_vcpu: int = DEFAULT_DEDICATED_VCPU
    dedicated_memory_mb: int = DEFAULT_DEDICATED_MEMORY_MB

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SandboxSettings:
        source = os.environ if env is None else env
        return cls(
            mode=_env_mode(source, ENV_MODE, "none"),
            home=_env_abs_path(source, ENV_HOME, DEFAULT_HOME),
            dedicated=source.get(ENV_DEDICATED, "").strip().lower() in {"1", "true", "yes"},
            rootfs=_env_abs_path(source, ENV_ROOTFS, DEFAULT_ROOTFS),
            python_home=_env_abs_path(source, ENV_PYTHON, DEFAULT_PYTHON_HOME),
            chat_net=str(chat_network_block(source.get(ENV_NET, ""))),
            pool_vcpu=_env_int(source, ENV_POOL_VCPU, DEFAULT_POOL_VCPU),
            pool_memory_mb=_env_int(source, ENV_POOL_MEMORY_MB, DEFAULT_POOL_MEMORY_MB),
            dedicated_vcpu=_env_int(source, ENV_DEDICATED_VCPU, DEFAULT_DEDICATED_VCPU),
            dedicated_memory_mb=_env_int(
                source, ENV_DEDICATED_MEMORY_MB, DEFAULT_DEDICATED_MEMORY_MB
            ),
        )

    def default_limits(self) -> tuple[int, int]:
        """``(vcpu, memory_mb)`` for a chat whose row names no limits of its own:
        the dedicated leak guard on a dedicated box, the smallest tier's figures
        (the pool floor) anywhere else."""
        if self.dedicated:
            return self.dedicated_vcpu, self.dedicated_memory_mb
        return self.pool_vcpu, self.pool_memory_mb

    def limits_for(self, vcpu: int | None, memory_mb: int | None) -> tuple[int, int]:
        """The chat's own limits where the row carries them — the server resolves
        the org's override and its plan tier into the row — and the box default
        where it carries ``None`` or nonsense."""
        default_vcpu, default_memory = self.default_limits()
        chosen_vcpu = vcpu if isinstance(vcpu, int) and vcpu > 0 else default_vcpu
        chosen_memory = (
            memory_mb if isinstance(memory_mb, int) and memory_mb > 0 else default_memory
        )
        return chosen_vcpu, chosen_memory


def agent_home(settings: SandboxSettings, *, gvisor: bool, mount_alias: bool) -> str | None:
    """The path the agent sees its folder at, when it is not the host path:
    ``settings.home`` under gVisor (the folder is mounted there) and on a
    ``none`` box that can bind it there; ``None`` where the agent sees the host
    path itself. The fence accepts that alias — and only then, so a real
    ``/home/alkera`` on a box that never mounted one is judged as the foreign
    path it is."""
    if settings.mode == "gvisor":
        return settings.home if gvisor else None
    return settings.home if mount_alias else None


def agent_home_for(settings: SandboxSettings, cap: SandboxCapability) -> str | None:
    """:func:`agent_home` read from what the probe found on this host."""
    return agent_home(settings, gvisor=cap.gvisor, mount_alias=cap.controls and cap.mount_ns)


def mount_aliases(
    settings: SandboxSettings,
    cap: SandboxCapability,
    *,
    working_dir: Path,
    runtime_dir: Path,
    agent_config_root: Path,
    envs_dir: Path | None = None,
) -> tuple[tuple[str, Path], ...]:
    """Every path the agent sees a host tree at that is not its host path, with
    the host tree it is bound to: the working directory at the agent's home,
    and under gVisor the agent server's own trees at their fixed internal
    paths, from the same :func:`chat_binds` the launch mounts from."""
    home = agent_home_for(settings, cap)
    if home is None:
        return ()
    aliases: list[tuple[str, Path]] = [(home, working_dir)]
    if settings.mode == "gvisor" and cap.gvisor:
        binds = chat_binds(
            runtime_dir=runtime_dir,
            agent_config_root=agent_config_root,
            default_env=cap.default_env,
            envs_dir=envs_dir,
        )
        aliases.extend((bind.destination, bind.source) for bind in binds)
    return tuple(aliases)


# ---------------------------------------------------------------------------
# The per-chat network
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SandboxNetwork:
    """One chat's network namespace: a veth pair between the host and the
    namespace, addressed from the chat's own /30. The container's stack (gVisor's
    netstack, under ``--network=sandbox``) takes over the namespace end."""

    namespace: str
    host_if: str
    host_ip: str
    container_ip: str
    prefix: int = 30
    container_if: str = "eth0"

    @property
    def namespace_path(self) -> str:
        return f"{NETNS_DIR}/{self.namespace}"


def plan_network(chat_id: str, uid: int, *, chat_net: str = DEFAULT_CHAT_NET) -> SandboxNetwork:
    """The container's network, from the uid of the user it is named after (a
    workspace member running as the workspace's uid still has its own): the
    ``i``-th uid of the range takes the ``i``-th /30 (host ``.1``, container
    ``.2``) and names the host interface, under fifteen characters always."""
    if not UID_MIN <= uid <= UID_MAX:
        raise SandboxRefusedError(f"uid {uid} is outside the chat range {UID_MIN}-{UID_MAX}")
    block = chat_network_block(chat_net)
    base = int(block.network_address) + (uid - UID_MIN) * 4
    return SandboxNetwork(
        namespace=chat_netns(chat_id),
        host_if=f"{VETH_PREFIX}{uid}",
        host_ip=str(ipaddress.IPv4Address(base + 1)),
        container_ip=str(ipaddress.IPv4Address(base + 2)),
    )


# ---------------------------------------------------------------------------
# Spec → launch
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """One chat's sandbox, as facts."""

    chat_id: str
    folder: Path
    """The chat's working directory: the one writable tree it owns."""
    uid: int
    vcpu: int
    memory_mb: int
    home: str
    mode: SandboxMode
    cgroup: CgroupDriver = "systemd"
    """How the cgroup is made: ``systemd`` (``systemd-run --scope`` + a slice),
    ``cgroupfs`` (files under ``/sys/fs/cgroup``), ``none`` (no cgroup)."""
    binds: tuple[Bind, ...] = ()
    """Every other host tree the container sees, each with the path it sees it
    at and who owns it (:class:`Bind`): the chat's runtime state and agent
    state (owned, read-write), its agent config (root's, read-only) and the
    agent's binaries (read-only). The folder itself is not among them: it is
    bound at :attr:`home` and nowhere else."""
    inherited: tuple[Bind, ...] = ()
    """Binds another launch made that this one runs inside: a command exec'd
    into the agent's live container sees the agent's mounts without mounting
    or owning anything itself. Part of the view its paths are spelled in
    (:meth:`agent_path`) and nothing else."""
    private_dirs: tuple[Path, ...] = ()
    """Directories the chat's trees sit in that hold what no other chat may
    reach, such as the chat's records beside its folder. Closed to group and
    other; this uid gets traverse only, like every ancestor."""
    bundle: Path | None = None
    """The daemon-owned directory a gVisor launch writes its OCI ``config.json``
    and the container's per-chat ``/etc`` files into. ``None`` for a shell
    command (it ``exec``s into the agent's existing container) and for the
    ``none`` mode."""
    rootfs: Path | None = None
    """The staged rootfs a gVisor container roots at (read-only, shared by every
    chat on the box; each container's writes to it land in its own overlay)."""
    overlay_dir: Path | None = None
    """Where the container's root overlay is backed on disk: a daemon-owned
    directory the container never sees. ``None`` keeps it in memory."""
    default_env: Path | None = None
    """The chat's default Python environment (:func:`default_env_path`), made
    before the spawn and active for the agent and its commands. ``None`` when
    the box has no interpreter to make it from."""
    python_home: str = DEFAULT_PYTHON_HOME
    """The interpreter the default environment is made from, at the same path
    on the host and in the rootfs."""
    path_dirs: tuple[str, ...] = ()
    """Directories that go first on the container's ``PATH`` after the default
    environment: the agent's bundled tools (``rg``), spelled as the container
    sees them (a tool bind's destination under gVisor)."""
    daemon_ports: tuple[int, ...] = ()
    """The daemon's TCP ports the container may reach on the host end of its
    veth pair — the parent-hosted tool server — and nothing else."""
    resolvers: tuple[str, ...] = ()
    """The nameservers the container's ``/etc/resolv.conf`` names: the host's
    real upstream, never a host-local stub the namespace cannot reach."""
    chat_net: str = DEFAULT_CHAT_NET
    mount_alias: bool = False
    """``none`` mode only: bind the folder at ``home`` in a private mount
    namespace around the process, so the agent sees the same short path a
    gVisor chat does. Needs a root box that can ``unshare`` a mount namespace."""
    unit: str | None = "agent"
    """The scope's name under the chat's slice. ``None`` lets systemd name it,
    which a shell command needs: a scope name must be unique while it lives,
    and one chat runs many commands beside its one agent server."""
    owns_cgroup: bool = True
    """Whether this launch makes the chat's cgroup, sets its limits and removes
    it after the exit. The agent server's launch does; a command run beside it
    joins the cgroup that launch made and leaves its limits and its lifetime
    alone. Also tells the gVisor runtime whether this launch ``run``s the
    container (the agent server) or ``exec``s into it (a command)."""
    runsc: str = "runsc"
    setpriv: str = "setpriv"
    systemd_run: str = "systemd-run"
    systemctl: str = "systemctl"
    uv: str = "uv"
    ip: str = "ip"
    nft: str = "nft"
    unshare: str = "unshare"
    cgroup_root: Path = Path("/sys/fs/cgroup")
    runsc_root: Path = Path("/run/alkera-runsc")
    """gVisor's own state directory (``runsc --root``); one per box, holding the
    live containers' state."""
    net_uid: int | None = None  #: names the network slot when not :attr:`uid` (plan_network)
    host_uds: bool = False
    """``runsc --host-uds=open``: may open (never make) a host socket bound in.
    A kernel sandbox's, for its kernels' own sockets; never an agent's."""
    writable_root: bool = False
    """Writes to the root land in the overlay (a kernel sandbox's system installs)."""
    share_gid: int | None = None
    """``none`` mode, a workspace member: the identity that owns the shared
    folder (its uid and its group). The folder stays that identity's, and the
    member reaches it through the group alone (its directories setgid, the
    launch's umask keeping what the member makes the group's), while the
    member's own trees (its agent's state, its environment) and every process
    it runs are :attr:`uid`'s, closed to every other member: with no container
    around a process, its uid is all that keeps one member out of another's
    files. ``None``: the folder is :attr:`uid`'s like every other tree."""
    agent_env: Mapping[str, str] = field(default_factory=dict, compare=False)
    """The environment the agent runs with. The ``none`` mode inherits the
    daemon's process environment at spawn, so this is unused there; gVisor runs
    ``runsc``, whose own environment the container does NOT inherit, so the
    agent's whole environment must be baked into the OCI ``config.json`` — this
    is where it comes from."""

    def folder_and_binds(self) -> tuple[Path, ...]:
        """The trees the chat owns: its folder and every owned bind."""
        return (self.folder, *(b.source for b in self.binds if b.kind == "owned"))

    @property
    def config_trees(self) -> tuple[Path, ...]:
        return tuple(b.source for b in self.binds if b.kind == "config")

    @property
    def tool_trees(self) -> tuple[Path, ...]:
        return tuple(b.source for b in self.binds if b.kind == "tool")

    @property
    def relocates(self) -> bool:
        """Whether the agent sees the chat's trees somewhere other than their
        host paths: under gVisor always (the container is composed from the
        binds), elsewhere only the folder, and only when it is aliased."""
        return self.mode == "gvisor"

    def agent_path(self, host: Path) -> str:
        """``host`` spelled as the agent sees it: under :attr:`home` when it
        lies in the folder and the launch mounts the folder there, under a
        bind's destination when it lies in a bound tree and this is a
        container, and the host path itself everywhere else. The folder wins
        over a bind that contains it, and a deeper bind over a shallower one."""
        return agent_view(
            host,
            folder=self.folder,
            home=self.home,
            binds=(*self.binds, *self.inherited),
            aliased=self.relocates or self.mount_alias,
            relocates=self.relocates,
        )

    @property
    def network(self) -> SandboxNetwork:
        return plan_network(self.chat_id, self.net_uid or self.uid, chat_net=self.chat_net)


@dataclass(frozen=True, slots=True)
class SandboxLaunch:
    """How to spawn one chat's agent server (or a command on its behalf): the
    command to run (or a prefix to wrap the caller's argv), what to add to the
    environment, and the steps to run around it."""

    mode: SandboxMode
    prefix: tuple[str, ...] = ()
    """A wrapper the caller's argv is appended to (``none``: the uid + cgroup
    launcher; gVisor ``exec``: the ``runsc exec`` invocation up to its flags).
    Empty when :attr:`command` carries the whole command."""
    tail: tuple[str, ...] = ()
    """What sits between the prefix (and the environment flags, when the
    wrapper carries them) and the caller's argv: the container id of a
    ``runsc exec``."""
    inner: tuple[str, ...] = ()
    """What the argv runs under inside an ``exec`` wrapper, after its environment:
    the shared umask (:func:`umask_prefix`), so its files stay the group's."""
    exec_env: bool = False
    """Whether the wrapper carries the command's environment itself: a process
    ``runsc exec`` starts begins with the container's own environment (the agent
    server's, its loopback password and inline config included), so the command
    is started through ``env -i`` with exactly the names the caller composed."""
    exec_cwd: str | None = None
    """The directory an ``exec`` wrapper enters the container in when the
    caller names none: the agent's home. A caller that runs a command
    somewhere under the root passes that place, spelled as the container
    sees it, to :meth:`wrap`."""
    command: tuple[str, ...] | None = None
    """The complete command, when the launch owns the whole invocation rather
    than wrapping the caller's argv — the gVisor agent server, whose argv lives
    in the OCI ``config.json`` and so cannot be appended."""
    env: Mapping[str, str] = field(default_factory=dict)
    """Names set over the caller's environment: ``HOME``, and under gVisor the
    container's ``PATH`` and the default environment's variables."""
    path_prefix: tuple[str, ...] = ()
    """Directories to put in front of the caller's own ``PATH`` (``none`` mode:
    the default environment's ``bin``). Under gVisor ``env`` carries the whole
    ``PATH`` instead."""
    cwd: Path | None = None
    before: tuple[Step, ...] = ()
    after_exit: tuple[Step, ...] = ()
    cgroup_procs: Path | None = None
    """When set, the spawned pid is written here after the spawn (the cgroupfs
    driver; systemd places the process itself)."""
    network: SandboxNetwork | None = None
    """The container's network, when it has one of its own: the daemon reaches
    the agent server at ``container_ip`` and the agent reaches the daemon at
    ``host_ip``."""
    agent_home: str | None = None
    """The path the agent sees its folder at when that is not the host path."""
    container: str | None = None
    """The container an ``exec`` wrapper enters, with the ``runsc`` that enters
    it, its state root and the chat's uid: what a kill of the command's tree
    inside the container is composed from (:func:`sandbox_processes.kill_tree`)."""
    runsc: str | None = None
    runsc_root: str | None = None
    uid: int | None = None
    memory_mb: int | None = None
    """The memory limit this launch bounds the chat to, in MiB, so a kill for
    memory can be named to the reader with the figure. ``None`` for a launch
    that bounds nothing (no cgroup on this host)."""
    umask: int | None = None
    """The umask the spawned command starts with, when not the daemon's 077:
    ``runsc`` makes the container's mountpoints with its own, and a chat's
    uid must walk them."""
    memory_events: Path | None = None
    """The chat cgroup's ``memory.events`` file, read after the agent server
    exits and before :attr:`after_exit` removes the cgroup: a non-zero
    ``oom_kill`` there means the kernel killed the chat's process for memory,
    and the reader is told the limit rather than "stopped answering"."""

    def wrap(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        *,
        cwd: str | None = None,
        pid_file: Path | None = None,
    ) -> list[str]:
        """The command to spawn: the baked command when the launch owns the
        whole invocation (the gVisor agent server, whose argv is in its OCI
        config), else the prefix and the caller's argv, with ``env`` as the
        command's whole environment where the wrapper carries it
        (:attr:`exec_env`) and ``cwd`` (the container's spelling of where it
        runs, the home when omitted) where the wrapper enters a container."""
        if self.command is not None:
            return list(self.command)
        flags: list[str] = []
        clean: list[str] = []
        if self.exec_env:
            where = cwd or self.exec_cwd
            if where is not None:
                flags.append(f"--cwd={where}")
            if pid_file is not None:
                # The command's pid inside the container, for the kill of its
                # tree there: the host-side client's death ends nothing inside.
                flags.append(f"--internal-pid-file={host_path(pid_file)}")
            clean = [CONTAINER_ENV, "-i", *(f"{n}={v}" for n, v in (env or {}).items())]
        return [*self.prefix, *flags, *self.tail, *clean, *self.inner, *argv]

    def apply_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """``env`` with this launch's names set and its ``PATH`` prefix applied.
        The ``PATH`` is the Linux box's, joined with its separator whatever the
        composer's own is."""
        out = {**env, **self.env}
        if self.path_prefix:
            existing = out.get("PATH", "")
            joined = ":".join(self.path_prefix)
            out["PATH"] = f"{joined}:{existing}" if existing else joined
        return out

    def after_spawn(self, pid: int) -> tuple[Step, ...]:
        if self.cgroup_procs is None:
            return ()
        return (WriteStep(self.cgroup_procs, str(pid)),)


def _cpu_quota(vcpu: int) -> str:
    return f"{vcpu * 100}%"


def _cpu_max(vcpu: int) -> str:
    """cgroup v2 ``cpu.max``: ``<quota> <period>`` in microseconds."""
    return f"{vcpu * 100_000} 100000"


def _memory_bytes(memory_mb: int) -> int:
    return memory_mb * 1024 * 1024


def cgroup_steps(spec: SandboxSpec) -> tuple[Step, ...]:
    """Create the chat's cgroup with its limits, before the spawn.

    The memory limit is a hard ceiling with no swap behind it: a chat that
    could page out past ``MemoryMax`` would take the box's swap (and its disk
    bandwidth) from every other chat before the kernel stopped it, and a
    box with no swap configured today may have some tomorrow."""
    if spec.cgroup == "systemd":
        return (
            ShellStep(
                (
                    spec.systemctl,
                    "set-property",
                    "--runtime",
                    chat_slice(spec.chat_id),
                    f"MemoryMax={spec.memory_mb}M",
                    "MemorySwapMax=0",
                    f"CPUQuota={_cpu_quota(spec.vcpu)}",
                    f"TasksMax={TASKS_MAX}",
                ),
            ),
        )
    if spec.cgroup == "cgroupfs":
        cg = chat_cgroup_path(spec.chat_id, spec.cgroup_root)
        return (
            ShellStep(("mkdir", "-p", host_path(cg))),
            WriteStep(cg / "memory.max", str(_memory_bytes(spec.memory_mb))),
            # A kernel built without swap accounting has no such file; the
            # limit above still stands, so its absence refuses nothing.
            WriteStep(cg / "memory.swap.max", "0", check=False),
            WriteStep(cg / "cpu.max", _cpu_max(spec.vcpu)),
            WriteStep(cg / "pids.max", str(TASKS_MAX)),
            # One OOM kills the chat's processes together, never one at a time
            # and never anything outside the group.
            WriteStep(cg / "memory.oom.group", "1", check=False),
        )
    return ()


def cgroup_cleanup_steps(spec: SandboxSpec) -> tuple[Step, ...]:
    """Remove the chat's cgroup after its agent server exits; anything still in
    it goes with it."""
    if spec.cgroup == "systemd":
        return (ShellStep((spec.systemctl, "stop", chat_slice(spec.chat_id)), check=False),)
    if spec.cgroup == "cgroupfs":
        cg = chat_cgroup_path(spec.chat_id, spec.cgroup_root)
        return (ShellStep(("rmdir", host_path(cg)), check=False),)
    return ()


def cgroup_prefix(spec: SandboxSpec) -> tuple[str, ...]:
    """The argv that places the process in its cgroup from the start (systemd
    only; ``systemd-run --scope`` execs the command in place)."""
    if spec.cgroup != "systemd":
        return ()
    argv = [
        spec.systemd_run,
        "--scope",
        "--quiet",
        "--collect",
        f"--slice={chat_slice(spec.chat_id)}",
    ]
    if spec.unit is not None:
        argv.append(f"--unit=alkera-chat-{chat_slug(spec.chat_id)}-{spec.unit}")
    argv.append("--")
    return tuple(argv)


#: The shell line that binds the folder at the agent's home inside a private
#: mount namespace and then becomes the rest of the argv. Positional, never
#: interpolated: ``$1`` is the folder, ``$2`` the home, ``"$@"`` after the
#: shift is the command.
_ALIAS_SHELL = 'mount --bind "$1" "$2" && shift 2 && exec "$@"'


def alias_prefix(spec: SandboxSpec) -> tuple[str, ...]:
    """``none`` mode: the argv that gives the process a private mount namespace
    with the chat's folder bound at ``home``, so the agent sees the same short
    path a gVisor chat does. Runs as root, before the uid drop; the namespace is
    private, so the bind is invisible to the host and every other chat."""
    return (
        spec.unshare,
        "--mount",
        "--propagation",
        "private",
        "--",
        "/bin/sh",
        "-c",
        _ALIAS_SHELL,
        "alkera-home",
        host_path(spec.folder),
        spec.home,
    )


# ---------------------------------------------------------------------------
# gVisor (runsc)
# ---------------------------------------------------------------------------

#: The OCI version the config.json declares.
OCI_VERSION = "1.0.2"
#: What ``runsc run`` starts under: the mountpoints it makes for the binds
#: (``/opt/alkera/harness`` and below) take its umask, and under the daemon's
#: 077 they come out 0700 root, which the chat's uid cannot walk.
RUNSC_UMASK = 0o022


def _oci_mount(destination: str, source: str, *, readonly: bool) -> dict[str, Any]:
    options = ["rbind", "ro" if readonly else "rw"]
    return {"destination": destination, "source": source, "type": "bind", "options": options}


def _tmpfs(destination: str, mode: str) -> dict[str, Any]:
    return {
        "destination": destination,
        "source": "tmpfs",
        "type": "tmpfs",
        "options": ["nosuid", "nodev", f"mode={mode}"],
    }


def container_path(spec: SandboxSpec) -> str:
    """The container's ``PATH``: the default environment first, then the agent's
    bundled tools, then the OS."""
    dirs: list[str] = []
    if spec.default_env is not None:
        dirs.append(f"{spec.agent_path(spec.default_env)}/bin")
    dirs.extend(spec.path_dirs)
    dirs.append(_CONTAINER_PATH)
    return ":".join(dict.fromkeys(dirs))


def environment_names(spec: SandboxSpec) -> dict[str, str]:
    """The variables that make the default environment the default: active
    (``VIRTUAL_ENV``), what ``uv run`` / ``uv sync`` use in the working
    directory, the interpreter uv reaches for first, and caches that live
    beside the environment under the chat's own state rather than in a home
    that is the user's folder. Every path is spelled as the agent sees it
    (:meth:`SandboxSpec.agent_path`). Empty when there is no default
    environment.

    ``UV_PYTHON`` is the environment's OWN interpreter, never the base one it
    was made from: uv reads it as ``--python`` for every ``uv pip`` command,
    and that flag outranks ``VIRTUAL_ENV``, so naming the base interpreter
    sent a plain ``uv pip install`` at the shared, externally managed install
    (exit 2) instead of the chat's environment. A new ``uv venv`` made from
    the environment's interpreter is still made from the base one behind it."""
    if spec.default_env is None:
        return {}
    env = spec.agent_path(spec.default_env)
    beside = spec.agent_path(spec.default_env.parent)
    return {
        "VIRTUAL_ENV": env,
        "UV_PROJECT_ENVIRONMENT": env,
        "UV_PYTHON": f"{env}/bin/python",
        "UV_CACHE_DIR": f"{beside}/.uv-cache",
        "PIP_CACHE_DIR": f"{beside}/.pip-cache",
        "MAMBA_ROOT_PREFIX": f"{beside}/mamba",
    }


def container_env(spec: SandboxSpec) -> dict[str, str]:
    """What the container's processes — the agent server and every command —
    have set over whatever else they carry: the agent's home (also where it
    stands), the container's ``PATH``, a UTF-8 locale, and the default
    environment's names. Where temporary files go differs: the agent server's
    are its own and go to the container's :data:`CONTAINER_TMP`; a command's
    are the chat's work and stay in the root, which :func:`command_env` says."""
    return {
        "HOME": spec.home,
        "PWD": spec.home,
        "PATH": container_path(spec),
        "LANG": "C.UTF-8",
        **environment_names(spec),
    }


def command_env(spec: SandboxSpec) -> dict[str, str]:
    """:func:`container_env` with a command's temporary files outside the
    synced tree: beside the default environment, else the container's tmpfs."""
    env = spec.default_env
    tmp = spec.agent_path(command_tmp_dir(env)) if env is not None else CONTAINER_TMP
    return {**container_env(spec), "TMPDIR": tmp}


def container_etc_files(spec: SandboxSpec) -> dict[str, str]:
    """The per-chat files the container sees under ``/etc``, written into the
    bundle and bound read-only: a ``passwd``/``group`` naming the chat's uid as
    ``alkera`` with its home (so ``whoami``, git and pip all have an identity),
    ``hosts`` and ``hostname`` for the container's own name, and a
    ``resolv.conf`` naming the host's real upstream resolvers — never the
    host's own stub, which the container's namespace cannot reach."""
    net = spec.network
    name = chat_container(spec.chat_id)
    return {
        "passwd": (
            "root:x:0:0:root:/root:/bin/sh\n"
            "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
            f"alkera:x:{spec.uid}:{spec.uid}:Alkera chat:{spec.home}:/bin/bash\n"
        ),
        "group": f"root:x:0:\nnogroup:x:65534:\nalkera:x:{spec.uid}:\n",
        "hosts": f"127.0.0.1 localhost\n::1 localhost\n{net.container_ip} {name}\n",
        "hostname": f"{name}\n",
        "resolv.conf": "".join(f"nameserver {server}\n" for server in spec.resolvers),
    }


def gvisor_oci_spec(spec: SandboxSpec, argv: Sequence[str]) -> dict[str, Any]:
    """The OCI runtime spec (``config.json``) gVisor runs the chat's agent under.

    Pure: plain data in, a dict out. The container roots at the staged rootfs,
    read-only, with its writes to it going to an overlay of its own. The agent
    runs as the chat's uid in its root, the chat's folder bound at ``HOME`` and
    nowhere else; every other tree it needs (:attr:`SandboxSpec.binds`) sits
    under :data:`INTERNAL_PREFIX`, never at a host path, so the container's
    view is the root, the OS and that prefix. It has a private ``/tmp``,
    ``/run`` and ``/dev/shm``, its own ``/proc``, the per-chat ``/etc`` files
    from the bundle, the chat's network namespace, and the chat's cgroup and
    limits. Nothing else of the host is mounted: not the chat's records
    beside the folder, not another chat, not ``.alkera/`` state, not the
    host's OS, and not the host's spelling of anything.
    """
    if spec.rootfs is None or spec.bundle is None:
        raise SandboxRefusedError("the gVisor container needs a staged rootfs and a bundle")
    folder = host_path(spec.folder)
    etc = spec.bundle / "etc"
    mounts: list[dict[str, Any]] = [
        {"destination": "/proc", "source": "proc", "type": "proc", "options": []},
        {"destination": "/dev", "source": "tmpfs", "type": "tmpfs", "options": []},
        {
            "destination": "/sys",
            "source": "sysfs",
            "type": "sysfs",
            "options": ["nosuid", "noexec", "nodev", "ro"],
        },
        # The container's private tmpfs mounts, not host paths.
        _tmpfs("/tmp", "1777"),  # noqa: S108
        _tmpfs("/run", "0755"),
        _tmpfs("/dev/shm", "1777"),  # noqa: S108
    ]
    for name in container_etc_files(spec):
        mounts.append(_oci_mount(f"/etc/{name}", host_path(etc / name), readonly=True))
    for bind in spec.binds:
        mounts.append(_oci_mount(bind.destination, host_path(bind.source), readonly=bind.readonly))
    # The folder at HOME, once: the agent's root, and the only place it is.
    mounts.append(_oci_mount(spec.home, folder, readonly=False))

    # The agent's whole environment, since the container does not inherit
    # runsc's; the sandbox's own names (HOME, the container PATH, the default
    # environment) win over the agent's, and what only describes the host is
    # left out.
    env = {k: v for k, v in spec.agent_env.items() if k not in HOST_ONLY_ENV}
    env.update(container_env(spec))
    env["TMPDIR"] = CONTAINER_TMP
    return {
        "ociVersion": OCI_VERSION,
        "process": {
            "terminal": False,
            "user": {"uid": spec.uid, "gid": spec.uid},
            "args": list(argv),
            "cwd": spec.home,
            "env": [f"{k}={v}" for k, v in env.items()],
            "noNewPrivileges": True,
            # No capabilities in any set: an unprivileged agent, like the uid
            # drop the ``none`` mode does with setpriv.
            "capabilities": {
                "bounding": [],
                "effective": [],
                "inheritable": [],
                "permitted": [],
                "ambient": [],
            },
        },
        "root": {"path": host_path(spec.rootfs), "readonly": not spec.writable_root},
        "hostname": chat_container(spec.chat_id),
        "mounts": mounts,
        "linux": {
            "cgroupsPath": _oci_cgroups_path(spec),
            "resources": {
                "cpu": {"quota": spec.vcpu * 100_000, "period": 100_000},
                # ``swap`` is the OCI memory+swap ceiling: equal to the limit,
                # it leaves the container no swap at all, the same bound the
                # chat's slice carries.
                "memory": {
                    "limit": _memory_bytes(spec.memory_mb),
                    "swap": _memory_bytes(spec.memory_mb),
                },
                "pids": {"limit": TASKS_MAX},
            },
            # The chat's own network namespace, made by the launch's steps: a
            # veth pair to the host and nothing else, which runsc's sandbox
            # netstack takes over. No route to the host loopback, another
            # chat, or the instance metadata service.
            "namespaces": [
                {"type": "pid"},
                {"type": "ipc"},
                {"type": "uts"},
                {"type": "mount"},
                {"type": "network", "path": spec.network.namespace_path},
            ],
        },
    }


def _oci_cgroups_path(spec: SandboxSpec) -> str:
    """Where runsc places the container's cgroup: the chat's systemd slice, or
    its cgroupfs path spelled from the cgroup root (runsc puts its mountpoint
    in front; a host path came out ``/sys/fs/cgroup/sys/fs/cgroup/...``)."""
    if spec.cgroup == "systemd":
        return f"{chat_slice(spec.chat_id)}:alkera:{chat_container(spec.chat_id)}"
    under = chat_cgroup_path(spec.chat_id, spec.cgroup_root).relative_to(spec.cgroup_root)
    return f"/{under.as_posix()}"


def runsc_run_argv(spec: SandboxSpec, bundle: Path) -> tuple[str, ...]:
    """The ``runsc run`` invocation for the chat's agent server. Its own network
    (``--network=sandbox``: gVisor's user-space netstack over the chat's
    namespace) keeps the host loopback and the metadata service unreachable;
    the root overlay keeps the shared rootfs read-only; the mounts are opened
    ``shared`` because the daemon writes into the folder while the container
    runs (a file dropped from the drive) and the container must see it. The
    agent argv is in the bundle's ``config.json``, not on this argv.

    On a systemd box the OCI ``cgroupsPath`` (``slice:prefix:name``) is read
    as one only under ``--systemd-cgroup``; without the flag runsc takes it
    for a cgroupfs path, outside the slice and its ``MemoryMax``."""
    argv = [spec.runsc, f"--root={host_path(spec.runsc_root)}", "--network=sandbox"]
    if spec.cgroup == "systemd":
        argv.append("--systemd-cgroup")
    if spec.overlay_dir is not None:
        argv.append(f"--overlay2=root:dir={host_path(spec.overlay_dir)}")
    else:
        argv.append("--overlay2=root:memory")
    argv.append("--file-access-mounts=shared")
    if spec.host_uds:
        argv.append("--host-uds=open")
    argv += ["run", f"--bundle={host_path(bundle)}", chat_container(spec.chat_id)]
    return tuple(argv)


def runsc_exec_prefix(spec: SandboxSpec) -> tuple[str, ...]:
    """The ``runsc exec`` invocation, up to its directory and environment
    flags, carrying a command run on the chat's behalf into the agent's live
    container as the chat's uid (its kernel, uid, network and cgroup); the
    rest (directory, environment, container id, argv) is :meth:`SandboxLaunch.wrap`'s."""
    return (
        spec.runsc,
        f"--root={host_path(spec.runsc_root)}",
        "exec",
        f"--user={spec.uid}:{spec.uid}",
    )


def legacy_mountpoint_steps(spec: SandboxSpec) -> tuple[Step, ...]:
    """What an earlier build's mounts left inside the shared rootfs, taken down
    before the container starts (:func:`legacy_mountpoint_argv`); unchecked."""
    argv = legacy_mountpoint_argv(spec.rootfs, spec.binds, spec.folder)
    return (ShellStep(argv, check=False),) if argv is not None else ()


def network_steps(spec: SandboxSpec) -> tuple[Step, ...]:
    """Make the chat's network namespace before the container starts: a fresh
    namespace (a leftover from a crashed run is removed first), a veth pair
    with the host end addressed and up, the namespace end addressed, up and
    routed through the host end, and the box firewall told which daemon ports
    this chat's host end may reach. Everything else about what the namespace
    may reach — NAT out, no metadata, no other chat, nothing else on the host —
    is the box ruleset ``sandbox-prereqs.sh`` loads once."""
    net = spec.network
    ns = net.namespace
    steps: list[Step] = [
        ShellStep((spec.ip, "netns", "delete", ns), check=False),
        ShellStep((spec.ip, "link", "delete", net.host_if), check=False),
        ShellStep((spec.ip, "netns", "add", ns)),
        ShellStep(
            (
                spec.ip,
                "link",
                "add",
                net.host_if,
                "type",
                "veth",
                "peer",
                "name",
                net.container_if,
                "netns",
                ns,
            )
        ),
        ShellStep((spec.ip, "addr", "add", f"{net.host_ip}/{net.prefix}", "dev", net.host_if)),
        ShellStep((spec.ip, "link", "set", net.host_if, "up")),
        ShellStep(
            (
                spec.ip,
                "-n",
                ns,
                "addr",
                "add",
                f"{net.container_ip}/{net.prefix}",
                "dev",
                net.container_if,
            )
        ),
        ShellStep((spec.ip, "-n", ns, "link", "set", net.container_if, "up")),
        ShellStep((spec.ip, "-n", ns, "link", "set", "lo", "up")),
        ShellStep((spec.ip, "-n", ns, "route", "add", "default", "via", net.host_ip)),
    ]
    for port in spec.daemon_ports:
        steps.append(ShellStep(_nft_element(spec, "add", net.host_if, port)))
    return tuple(steps)


def network_cleanup_steps(spec: SandboxSpec) -> tuple[Step, ...]:
    """Take the chat's network down after the container is gone: its port
    grants, then the namespace (which takes the veth pair with it)."""
    net = spec.network
    steps: list[Step] = [
        ShellStep(_nft_element(spec, "delete", net.host_if, port), check=False)
        for port in spec.daemon_ports
    ]
    steps.append(ShellStep((spec.ip, "netns", "delete", net.namespace), check=False))
    return tuple(steps)


def _nft_element(spec: SandboxSpec, verb: str, host_if: str, port: int) -> tuple[str, ...]:
    table = NFT_TABLE.split()
    return (spec.nft, verb, "element", *table, NFT_CHAT_PORTS_SET, f'{{ "{host_if}" . {port} }}')


# ---------------------------------------------------------------------------
# Runtimes: the seam the spawn path calls
# ---------------------------------------------------------------------------


@runtime_checkable
class SandboxRuntime(Protocol):
    """Turns a :class:`SandboxSpec` into the :class:`SandboxLaunch` that runs the
    chat's agent under this node's sandbox. The spawn path calls
    :meth:`compose_launch` without knowing which implementation is active."""

    @property
    def mode(self) -> SandboxMode: ...

    def compose_launch(self, spec: SandboxSpec, argv: Sequence[str] = ()) -> SandboxLaunch:
        """The launch for ``spec``. ``argv`` is the agent server's own argv,
        needed when the runtime owns the whole command (gVisor bakes it into the
        OCI config); a launch that only wraps the caller's argv ignores it."""
        ...


@dataclass(frozen=True, slots=True)
class NoneRuntime:
    """No sandbox boundary — a single-tenant node. The per-chat controls are
    still applied, each where the host has it (they are resource/ownership
    controls, not the boundary): the chat runs as its own uid always, under its
    own cgroup where the box has a driver (``spec.cgroup`` is ``systemd`` or
    ``cgroupfs``) and with no cgroup where it has none, just without runsc in
    front. On a box that can, the folder is also bound at the agent's home in a
    private mount namespace, so the agent sees the same short path a gVisor
    chat does; elsewhere its home is the folder's own host path. A bare
    container with root and no cgroup, no systemd and no ``unshare`` therefore
    runs the agent as the chat's uid and nothing more."""

    mode: SandboxMode = "none"

    def compose_launch(self, spec: SandboxSpec, argv: Sequence[str] = ()) -> SandboxLaunch:
        return host_steps_checked(spec, self._launch(spec))

    def _launch(self, spec: SandboxSpec) -> SandboxLaunch:
        cgroup_before = cgroup_steps(spec) if spec.owns_cgroup else ()
        # The agent server's launch lends the chat's uid the agent's own trees
        # once per spawn; a command beside it needs nothing more than the
        # agent already has.
        lend = read_access_steps(spec) if spec.owns_cgroup else ()
        before: list[Step] = [*cgroup_before, *uid_steps(spec), *env_steps(spec), *lend]
        alias: tuple[str, ...] = ()
        if spec.mount_alias:
            before.append(ShellStep(("mkdir", "-p", spec.home)))
            alias = alias_prefix(spec)
        # The agent server and every command start under the tree's shared
        # umask: another uid of the workspace (a kernel, a member) writes
        # where they wrote. A member on this box keeps everyone else out.
        prefix = (
            *cgroup_prefix(spec),
            *alias,
            *uid_prefix(spec),
            *umask_prefix(umask=spec_umask(spec)),
        )
        cgroup_procs = None
        if spec.cgroup == "cgroupfs":
            cgroup_procs = chat_cgroup_path(spec.chat_id, spec.cgroup_root) / "cgroup.procs"
        home = spec.home if spec.mount_alias else host_path(spec.folder)
        env = {"HOME": home, **environment_names(spec)}
        path_prefix: tuple[str, ...] = ()
        if spec.default_env is not None:
            path_prefix = (f"{host_path(spec.default_env)}/bin",)
        bounded = spec.owns_cgroup and spec.cgroup != "none"
        return SandboxLaunch(
            mode="none",
            umask=SHARED_TREE_UMASK if spec.share_gid is not None else None,
            prefix=prefix,
            env=env,
            path_prefix=path_prefix,
            cwd=spec.folder,
            before=tuple(before),
            after_exit=(
                (*env_exit_steps(spec), *cgroup_cleanup_steps(spec)) if spec.owns_cgroup else ()
            ),
            cgroup_procs=cgroup_procs,
            agent_home=spec.home if spec.mount_alias else None,
            memory_mb=spec.memory_mb if bounded else None,
            memory_events=memory_events_path(spec) if bounded else None,
        )


@dataclass(frozen=True, slots=True)
class GvisorRuntime:
    """The agent runs under gVisor (``runsc``): a user-space kernel, a real
    boundary against untrusted code, which the multi-tenant pool requires.

    The agent server ``runsc run``s a fresh container whose OCI ``config.json``
    (written to the launch's bundle) carries the staged rootfs, the folder
    mount, the chat's uid, its own network namespace, and the cgroup and
    limits. A command run on the chat's behalf ``runsc exec``s into that live
    container, so it shares the container's kernel, uid, network and cgroup."""

    mode: SandboxMode = "gvisor"

    def compose_launch(self, spec: SandboxSpec, argv: Sequence[str] = ()) -> SandboxLaunch:
        if spec.owns_cgroup:
            return host_steps_checked(spec, self._run_launch(spec, argv))
        return host_steps_checked(spec, self._exec_launch(spec))

    def _run_launch(self, spec: SandboxSpec, argv: Sequence[str]) -> SandboxLaunch:
        if not argv:
            raise SandboxRefusedError(
                "the gVisor agent server needs its command to write the OCI config"
            )
        if spec.bundle is None:
            raise SandboxRefusedError("the gVisor agent server needs a bundle directory")
        if spec.rootfs is None:
            raise SandboxRefusedError(
                "the gVisor agent server needs the staged rootfs; this box has none"
            )
        if not spec.resolvers:
            raise SandboxRefusedError(
                "the gVisor container needs a resolver it can reach; the host names none "
                "outside its own loopback"
            )
        net = spec.network
        config = spec.bundle / "config.json"
        etc = spec.bundle / "etc"
        # The agent server starts under the tree's shared umask, as every
        # command exec'd beside it does (:attr:`SandboxLaunch.inner`).
        oci = gvisor_oci_spec(spec, (*umask_prefix(), *argv))
        before: list[Step] = [
            *cgroup_steps(spec),
            *uid_steps(spec),
            *env_steps(spec),
            *legacy_mountpoint_steps(spec),
            ShellStep(("mkdir", "-p", host_path(etc))),
        ]
        if spec.overlay_dir is not None:
            before.append(ShellStep(("mkdir", "-p", host_path(spec.overlay_dir))))
        before += [
            WriteStep(etc / name, content, mode=0o644)
            for name, content in container_etc_files(spec).items()
        ]
        before.append(WriteStep(config, json.dumps(oci, indent=2, sort_keys=True)))
        before += network_steps(spec)
        after_exit: list[Step] = [
            *env_exit_steps(spec),
            ShellStep(
                (
                    spec.runsc,
                    f"--root={host_path(spec.runsc_root)}",
                    "delete",
                    "--force",
                    chat_container(spec.chat_id),
                ),
                check=False,
            ),
            *network_cleanup_steps(spec),
            *cgroup_cleanup_steps(spec),
        ]
        if spec.overlay_dir is not None:
            after_exit.append(ShellStep(("rm", "-rf", host_path(spec.overlay_dir)), check=False))
        bounded = spec.cgroup != "none"
        return SandboxLaunch(
            mode="gvisor",
            command=runsc_run_argv(spec, spec.bundle),
            env={"HOME": spec.home},
            cwd=spec.bundle,
            before=tuple(before),
            after_exit=tuple(after_exit),
            network=net,
            agent_home=spec.home,
            umask=RUNSC_UMASK,
            memory_mb=spec.memory_mb if bounded else None,
            memory_events=memory_events_path(spec) if bounded else None,
        )

    def _exec_launch(self, spec: SandboxSpec) -> SandboxLaunch:
        return SandboxLaunch(
            mode="gvisor",
            prefix=runsc_exec_prefix(spec),
            tail=(chat_container(spec.chat_id),),
            inner=umask_prefix(),
            exec_env=True,
            exec_cwd=spec.home,
            env=command_env(spec),
            cwd=spec.folder,
            before=uid_steps(spec),
            network=spec.network,
            agent_home=spec.home,
            container=chat_container(spec.chat_id),
            runsc=spec.runsc,
            runsc_root=host_path(spec.runsc_root),
            uid=spec.uid,
        )


#: The bare launch a session that is not sandboxed at all runs under — a local
#: CLI or editor session, macOS, the tests, and a host with no uid/cgroup to
#: apply. The command runs directly, exactly as if there were no sandbox module.
NO_SANDBOX = SandboxLaunch(mode="none")


def select_runtime(mode: SandboxMode, *, gvisor_ready: bool) -> SandboxRuntime:
    """The runtime a box in ``mode`` uses. A box set to ``gvisor`` on a host
    that cannot run ``runsc`` refuses every chat rather than running it
    unsandboxed — the same fail-closed posture the old requirement check had."""
    if mode == "gvisor":
        if not gvisor_ready:
            raise SandboxRefusedError(
                "this node is configured to sandbox chats under gVisor (runsc) but the host "
                "cannot provide it; the chat is refused rather than run unsandboxed"
            )
        return GvisorRuntime()
    return NoneRuntime()


def shell_spec(agent: SandboxSpec, *, python: Sequence[Path] = ()) -> SandboxSpec:
    """The spec a command run on the chat's behalf gets: the agent server's own
    chat, folder, uid, mode, cgroup driver, home and default environment — so
    it lands in the same slice (or the same gVisor container) under the same
    uid with the same view — with no agent trees, an unnamed scope, the
    interpreter's trees when the command runs our Python, and no hold on the
    cgroup: the command joins the one the agent server's launch made, under
    the limits that launch set."""
    return SandboxSpec(
        chat_id=agent.chat_id,
        folder=agent.folder,
        uid=agent.uid,
        vcpu=agent.vcpu,
        memory_mb=agent.memory_mb,
        home=agent.home,
        mode=agent.mode,
        cgroup=agent.cgroup,
        binds=tuple(Bind(p, host_path(p), readonly=True, kind="tool") for p in python),
        inherited=(*agent.binds, *agent.inherited),
        bundle=None,
        default_env=agent.default_env,
        python_home=agent.python_home,
        path_dirs=agent.path_dirs,
        chat_net=agent.chat_net,
        mount_alias=agent.mount_alias,
        share_gid=agent.share_gid,
        unit=None,
        owns_cgroup=False,
        runsc=agent.runsc,
        setpriv=agent.setpriv,
        systemd_run=agent.systemd_run,
        systemctl=agent.systemctl,
        uv=agent.uv,
        ip=agent.ip,
        nft=agent.nft,
        unshare=agent.unshare,
        cgroup_root=agent.cgroup_root,
        runsc_root=agent.runsc_root,
    )


# ---------------------------------------------------------------------------
# The firewall rules a box needs, stated once
# ---------------------------------------------------------------------------


__all__ = [
    "AGENT_DATA_SUBDIR",
    "AGENT_MOUNT",
    "CONTAINER_ENV",
    "CONTAINER_TMP",
    "DEFAULT_CHAT_NET",
    "DEFAULT_ENV_NAME",
    "DEFAULT_HOME",
    "DEFAULT_PYTHON_HOME",
    "DEFAULT_ROOTFS",
    "ENVS_MOUNT",
    "ENVS_SUBDIR",
    "HARNESS_CONFIG_MOUNT",
    "HARNESS_DATA_MOUNT",
    "HARNESS_MOUNT",
    "HARNESS_STATE_MOUNT",
    "HOST_ONLY_ENV",
    "INTERNAL_PREFIX",
    "METADATA_IPV4",
    "METADATA_IPV6",
    "NETNS_DIR",
    "NFT_CHAT_PORTS_SET",
    "NFT_TABLE",
    "NO_SANDBOX",
    "OCI_VERSION",
    "OWNED_MKDIR_SHELL",
    "RELOCATE_SOURCE",
    "RIPGREP_MOUNT",
    "ROOTFS_STAMP",
    "RUNSC_UMASK",
    "RUNTIME_STATE_SUBDIR",
    "SANDBOX_MODES",
    "SHARED_TREE_UMASK",
    "SIGKILL_EXIT_STATUSES",
    "TASKS_MAX",
    "UID_MAX",
    "UID_MIN",
    "VETH_PREFIX",
    "Bind",
    "BindKind",
    "CgroupDriver",
    "GvisorRuntime",
    "NoneRuntime",
    "RepairStep",
    "SandboxLaunch",
    "SandboxMode",
    "SandboxNetwork",
    "SandboxRefusedError",
    "SandboxRuntime",
    "SandboxSettings",
    "SandboxSpec",
    "ShellStep",
    "Step",
    "TreeIdentity",
    "WriteStep",
    "agent_home",
    "agent_home_for",
    "alias_prefix",
    "argv_text",
    "cgroup_steps",
    "chat_binds",
    "chat_cgroup_dir",
    "chat_cgroup_path",
    "chat_container",
    "chat_identity",
    "chat_netns",
    "chat_network_block",
    "chat_network_rules",
    "chat_slice",
    "chat_slice_cgroup_path",
    "chat_slug",
    "chat_user",
    "command_env",
    "container_env",
    "container_etc_files",
    "container_path",
    "default_env_path",
    "ensure_chat_uid",
    "env_steps",
    "environment_names",
    "explain_sandbox",
    "gvisor_oci_spec",
    "host_path",
    "legacy_mountpoint_steps",
    "memory_events_path",
    "memory_limit_detail",
    "memory_limit_exceeded",
    "metadata_block_rules",
    "mount_aliases",
    "network_cleanup_steps",
    "network_steps",
    "oom_kills",
    "owner_session",
    "plan_network",
    "python_ro_binds",
    "read_access_steps",
    "read_oom_kills",
    "read_oom_kills_under",
    "relative_to_container",
    "relocate_argv",
    "run_steps",
    "runsc_exec_prefix",
    "runsc_run_argv",
    "select_runtime",
    "session_default_env",
    "session_envs_dir",
    "shell_spec",
    "tool_binds",
    "uid_steps",
    "umask_prefix",
    "useradd_argv",
    "wrapper_argv",
]
