"""The kernel sandbox: one gVisor sandbox per workspace holding its notebook kernels.

Notebook kernels never run in an agent's container. A kernel's memory there can
take the agent down (gVisor honours neither ``oom_score_adj`` nor a nested
cgroup, and a host OOM kills the whole sandbox), so each workspace gets one
sandbox of its own for all its kernels. It is a workspace sandbox of the kind
``kernel`` (:mod:`alkera_cli.harness.sandbox_kinds`): the seam that starts every
kind decides who it runs as (the workspace's tree identity), what it is named
after (a container, cgroup and network namespace of its own) and whether the
box can run it; this kind says only what is its own:

* **gVisor only**: a box in ``none`` mode runs no kernel sandbox;
* **exactly four binds**: the workspace's files tree at the path the agents
  see it at, the workspace's environments, the platform mount holding
  ``boot.py`` and ``_alkera_kernel`` (read-only), and one runtime directory
  for the kernels' sockets and data, never re-owned by a launch. Each lands
  on a fixed mountpoint the box's rootfs already has. No chat records, no
  agent state, no agent secret: nothing of an agent is reachable from here;
* **its own cgroup** under the org's slice, with its own ``memory.max``,
  ``memory.swap.max 0``, ``memory.oom.group 1`` and ``pids.max``, and never
  ``memory.high`` (the box's cgroup steps, which set none);
* **the agents' network**: its own namespace and veth, NAT out, never the
  metadata service or another sandbox;
* the staged rootfs **resolved** (the probe's), host Unix sockets **openable**
  (``--host-uds=open``) so a kernel reaches the engine through its own socket,
  and **PID 1 a reaper**.

The :class:`KernelSandbox` shell starts it on the first kernel, probes it
ready with ``runsc exec <sandbox> true`` (polling ``runsc state`` while ``runsc
run`` holds the container stalls for seconds), hands every kernel a uid of its
own from the org's range with the workspace's files group as its primary group
(only once every process that uid ran before is gone), and stops it when the
workspace is put away and ten minutes after its last kernel stopped.

A notebook gets a kernel only in the sandbox of the workspace whose tree holds
it (:func:`place_notebook`); a private chat folder is a workspace of one.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final

from alkera_core.process import SpawnSpec, spawn
from alkera_notebook.kernels.launch_local import KERNELS_SUBDIR, kernel_data_dir

from alkera_cli.harness.sandbox import (
    DEFAULT_HOME,
    RUNSC_UMASK,
    SandboxLaunch,
    SandboxRefusedError,
    SandboxSettings,
    SandboxSpec,
)
from alkera_cli.harness.sandbox_kinds import (
    SandboxIdentity,
    WorkspaceSandboxRequest,
    box_tools,
    plan_workspace_sandbox,
    register_workspace_sandbox,
    workspace_container,
    workspace_sandbox_kinds,
)
from alkera_cli.harness.sandbox_layout import (
    CONTAINER_ENV,
    ENVS_MOUNT,
    INTERNAL_PREFIX,
    Bind,
    chat_container,
    host_path,
    relative_to_container,
)
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.harness.sandbox_steps import (
    RepairStep,
    Runner,
    ShellStep,
    Step,
    run_argv,
    run_steps,
)
from alkera_cli.harness.sandbox_uid import workspace_files_gid
from alkera_cli.notebooks.memory_cgroup import read_oom_events

logger = logging.getLogger(__name__)

#: The workspace sandbox kind a notebook kernel runs in.
KERNEL_KIND: Final = "kernel"
#: Where the platform mount (``boot.py``, ``_alkera_kernel``) is seen in the
#: kernel sandbox; the kernel's command names ``<KERNEL_MOUNT>/boot.py``.
KERNEL_MOUNT: Final = f"{INTERNAL_PREFIX}/py"
#: Where the runtime directory (the kernels' sockets and data) is seen. Like
#: every bind's, its mountpoint is made in the staged rootfs by the box's
#: prerequisites, never by runsc at launch (an org worker's rootfs is
#: read-only, and a host path made there would show in every sandbox).
RUN_MOUNT: Final = f"{INTERNAL_PREFIX}/run"
#: Under the runtime directory: one directory per kernel holding its socket,
#: and (the engine's own layout) one data directory per kernel for the
#: file-backed results it writes the kernel.
SOCKET_SUBDIR: Final = "sock"
DATA_SUBDIR: Final = KERNELS_SUBDIR
#: How long the sandbox outlives its last kernel.
IDLE_STOP_SECONDS: Final = 600.0
#: How long a stop waits for ``runsc run`` to take the container down on
#: ``SIGTERM`` before the cgroup is killed.
TERMINATE_GRACE_SECONDS: Final = 2.0
#: How long a start may take to answer ``runsc exec true``.
READY_TIMEOUT_SECONDS: Final = 30.0
#: Processes one kernel uid may hold (``RLIMIT_NPROC``, which gVisor enforces):
#: enough for a kernel, its threads' helpers and a build it runs.
KERNEL_NPROC: Final = 256
#: The refusal a run of a notebook outside any workspace's tree gets.
OPEN_IN_WORKSPACE: Final = "Open in a workspace to run"
#: A kernel id as it may name a file: the engine's ids are ``krn_<hex>``.
KERNEL_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: PID 1 of the kernel sandbox: reaps whatever is reparented to it, holds
#: nothing, and ends on ``SIGTERM``. Run by the rootfs's own interpreter,
#: isolated and without ``site``.
REAPER_SOURCE: Final = (
    "import os, time\n"
    "while True:\n"
    "    try:\n"
    "        os.waitpid(-1, 0)\n"
    "    except ChildProcessError:\n"
    "        time.sleep(1)\n"
)


class PlacementRefusedError(SandboxRefusedError):
    """A notebook outside every workspace's tree on this box gets no kernel."""

    def __init__(self, message: str = OPEN_IN_WORKSPACE) -> None:
        super().__init__(message)


#: What a run is refused with while the folder lease is in doubt.
FENCED_MESSAGE: Final = "This workspace lost contact with the server. Runs resume when it is back."


class FolderFencedError(SandboxRefusedError):
    """The workspace's folder lease is in doubt: no kernel may run until it is
    confirmed (``alkera_cli.files.folder_fence``)."""

    def __init__(self) -> None:
        super().__init__(FENCED_MESSAGE)


def place_notebook(notebook: Path, trees: Mapping[str, Path]) -> str:
    """The workspace whose files tree holds ``notebook``: the only one whose
    kernel sandbox may run it. ``trees`` maps each workspace held here
    (custody key) to its files tree; a private chat folder is in it as the
    workspace of one it is. Paths are compared resolved, so a link inside a
    tree that points into another tree, or out of every tree, places the
    notebook where it really is. Refuses with :data:`OPEN_IN_WORKSPACE`."""
    try:
        where = notebook.resolve(strict=True)
    except OSError as exc:
        raise PlacementRefusedError() from exc
    found: list[tuple[int, str]] = []
    for key, tree in trees.items():
        try:
            root = tree.resolve(strict=True)
        except OSError:
            continue
        if where != root and root in where.parents:
            found.append((len(root.parts), key))
    if not found:
        raise PlacementRefusedError()
    # Nested trees cannot happen on one box, but the deepest is the holder.
    return max(found)[1]


def checked_kernel_id(kernel_id: str) -> str:
    """``kernel_id`` when it can name a file under the runtime directory."""
    if not KERNEL_ID_RE.match(kernel_id):
        raise ValueError(f"refusing kernel id {kernel_id!r}")
    return kernel_id


@dataclass(frozen=True, slots=True)
class KernelIdentity:
    """Who one kernel runs as: its own uid, the workspace's files group."""

    uid: int
    gid: int
    slot: int


@dataclass(frozen=True, slots=True)
class KernelTrees:
    """The trees a kernel sandbox binds beside the workspace's files tree, as
    the kind reads them from its request (``options["trees"]``), and where the
    box keeps its cgroups and gVisor's state."""

    envs_dir: Path
    platform_mount: Path
    runtime_dir: Path
    cgroup_root: Path = Path("/sys/fs/cgroup")
    runsc_root: Path = Path("/run/alkera-runsc")


def kernel_binds(trees: KernelTrees) -> tuple[Bind, ...]:
    """The binds beside the files tree: the environments (shared with the
    agents, repaired with the tree), the platform mount (read-only) and the
    runtime directory (as it is)."""
    return (
        Bind(trees.envs_dir, ENVS_MOUNT),
        Bind(trees.platform_mount, KERNEL_MOUNT, readonly=True, kind="tool"),
        Bind(trees.runtime_dir, RUN_MOUNT, kind="runtime"),
    )


def reaper_argv(python_home: str) -> tuple[str, ...]:
    return (f"{python_home}/bin/python3", "-I", "-S", "-c", REAPER_SOURCE)


@dataclass(frozen=True, slots=True)
class KernelSandboxKind:
    """The ``kernel`` workspace sandbox kind (see the module docstring)."""

    name: str = KERNEL_KIND

    def spec(
        self,
        request: WorkspaceSandboxRequest,
        *,
        identity: SandboxIdentity,
        settings: SandboxSettings,
        cap: SandboxCapability,
    ) -> SandboxSpec:
        if settings.mode != "gvisor":
            raise SandboxRefusedError("notebook kernels run only in a gVisor sandbox")
        trees = request.options.get("trees")
        if not isinstance(trees, KernelTrees):
            raise SandboxRefusedError("a kernel sandbox request names its trees")
        if cap.rootfs is None:
            raise SandboxRefusedError("the kernel sandbox needs the staged rootfs")
        vcpu, memory_mb = settings.limits_for(request.vcpu, request.memory_mb)
        return SandboxSpec(
            chat_id=identity.container,
            folder=request.folder,
            uid=identity.uid,
            net_uid=identity.net_uid,
            vcpu=vcpu,
            memory_mb=memory_mb,
            home=settings.home,
            mode="gvisor",
            cgroup=cap.cgroup,
            binds=kernel_binds(trees),
            bundle=request.state_dir / "bundle",
            rootfs=Path(cap.rootfs),
            # The root is writable (below) over gVisor's memory overlay, the
            # form an org worker's sandbox runs system installs with (runsc
            # refuses every write to a root marked read-only, overlay or
            # not). It is discarded when the sandbox stops and counts against
            # the sandbox's own memory limit.
            overlay_dir=None,
            python_home=cap.python_home or settings.python_home,
            resolvers=cap.resolvers,
            chat_net=settings.chat_net,
            host_uds=True,
            # System installs land in this sandbox's own overlay, which the
            # stop discards: the shared rootfs itself stays read-only.
            writable_root=True,
            cgroup_root=trees.cgroup_root,
            runsc_root=trees.runsc_root,
            **box_tools(cap),
        )

    def argv(self, request: WorkspaceSandboxRequest, spec: SandboxSpec) -> tuple[str, ...]:
        return reaper_argv(spec.python_home)


def register_kernel_kind() -> None:
    """Make the ``kernel`` kind startable on this box (once). A box registers
    it when it makes its first kernel sandbox; importing this module does not."""
    if KERNEL_KIND not in workspace_sandbox_kinds():
        register_workspace_sandbox(KernelSandboxKind())


@dataclass(frozen=True, slots=True)
class KernelSandboxConfig:
    """One workspace's kernel sandbox, as facts the org worker resolved."""

    workspace: str
    """The workspace's custody key (``ws:<id>``, or a chat id for a workspace
    of one)."""
    folder: Path
    """The workspace's files tree, seen at the home as the agents see it."""
    trees: KernelTrees
    state_dir: Path
    """The daemon's own directory for the sandbox: its OCI bundle, its log and
    the kernels' pid files. Never bound in."""
    memory_mb: int
    vcpu: int
    max_kernels: int = 4
    kernel_nproc: int = KERNEL_NPROC
    idle_stop_seconds: float = IDLE_STOP_SECONDS
    home: str = DEFAULT_HOME
    python_home: str = "/opt/alkera/python/current"

    @property
    def container(self) -> str:
        return chat_container(workspace_container(self.workspace, KERNEL_KIND))

    @property
    def runtime_dir(self) -> Path:
        return self.trees.runtime_dir

    @property
    def platform_mount(self) -> Path:
        return self.trees.platform_mount

    @property
    def envs_dir(self) -> Path:
        return self.trees.envs_dir

    def request(self) -> WorkspaceSandboxRequest:
        return WorkspaceSandboxRequest(
            workspace=self.workspace,
            folder=self.folder,
            state_dir=self.state_dir,
            vcpu=self.vcpu,
            memory_mb=self.memory_mb,
            options={"trees": self.trees},
        )


@dataclass(frozen=True, slots=True)
class RunscTarget:
    """What a ``runsc exec`` into the kernel sandbox is composed from."""

    runsc: str
    root: Path
    container: str


def exec_argv(
    target: RunscTarget,
    user: tuple[int, int],
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    caps: Sequence[str] = (),
    pid_file: Path | None = None,
) -> list[str]:
    """``runsc exec`` into the kernel sandbox as ``user`` (``uid, gid``) with
    exactly ``env`` (through ``env -i``: a process ``runsc exec`` starts would
    otherwise begin with PID 1's environment), the capabilities named and no
    other (none unless named), in ``cwd`` (the container's spelling)."""
    uid, gid = user
    flags = [f"--user={uid}:{gid}", *(f"--cap={cap}" for cap in caps)]
    if cwd is not None:
        flags.append(f"--cwd={cwd}")
    if pid_file is not None:
        flags.append(f"--internal-pid-file={host_path(pid_file)}")
    clean = [CONTAINER_ENV, "-i", *(f"{k}={v}" for k, v in (env or {}).items())]
    return [
        target.runsc,
        f"--root={host_path(target.root)}",
        "exec",
        *flags,
        target.container,
        *clean,
        *argv,
    ]


def runtime_steps(runtime_dir: Path, gid: int) -> tuple[Step, ...]:
    """The runtime directory and its two levels: the worker's, with the files
    group and 0750, so a kernel can walk to its own socket and data directory
    (each its own, 0700) and to no other kernel's. Never emptied here: the
    engine may already have bound the socket of the kernel whose launch is
    starting the sandbox, and it removes each endpoint itself."""
    run = host_path(runtime_dir)
    parts = [run, f"{run}/{SOCKET_SUBDIR}", f"{run}/{DATA_SUBDIR}"]
    return (
        ShellStep(("mkdir", "-p", *parts)),
        ShellStep(("chown", f"0:{gid}", *parts)),
        ShellStep(("chmod", "0750", *parts)),
    )


#: Spawns ``runsc run`` with its output going to the log.
Spawn = Callable[[Sequence[str], IO[bytes]], "subprocess.Popen[bytes]"]
#: Finds or makes the uid a name holds in the org's range (the worker's uid
#: ledger, :func:`~alkera_cli.harness.sandbox_uid.ensure_chat_uid`).
EnsureUid = Callable[[str], int]


def _spawn(argv: Sequence[str], log: IO[bytes]) -> subprocess.Popen[bytes]:
    # runsc makes the sandbox's mountpoints under its own umask; the child's
    # alone, so no other thread of this process writes under it meanwhile.
    return spawn(
        SpawnSpec(
            argv=list(argv),
            env=os.environ,
            stdout=log.fileno(),
            stderr=log.fileno(),
            umask=RUNSC_UMASK,
        )
    )


@dataclass(slots=True)
class _Running:
    proc: subprocess.Popen[bytes]
    launch: SandboxLaunch
    cgroup_dir: Path | None


@dataclass(slots=True)
class _Ledger:
    """Which kernel holds which slot, and when the last one stopped."""

    held: dict[str, KernelIdentity] = field(default_factory=dict)
    idle_since: float | None = None


class KernelSandbox:
    """One workspace's kernel sandbox on this box: started on the first kernel,
    stopped on the workspace's put-away or after it has been idle."""

    def __init__(
        self,
        config: KernelSandboxConfig,
        *,
        settings: SandboxSettings,
        cap: SandboxCapability,
        ensure_uid: EnsureUid,
        run: Runner = run_argv,
        repair: Callable[[RepairStep], None] | None = None,
        spawn: Spawn = _spawn,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
        stop_grace: float = 10.0,
        files_gid: int | None = None,
    ) -> None:
        if config.max_kernels < 1:
            raise ValueError("a kernel sandbox holds at least one kernel")
        register_kernel_kind()
        self.config = config
        self._settings = settings
        self._cap = cap
        self._ensure_uid = ensure_uid
        self._run = run
        self._repair = repair
        self._spawn = spawn
        self._clock = clock
        self._sleep = sleep
        self._ready_timeout = ready_timeout
        self._stop_grace = stop_grace
        self._files_gid = files_gid
        self.target = RunscTarget(
            runsc=box_tools(cap)["runsc"],
            root=config.trees.runsc_root,
            container=config.container,
        )
        self._lock = threading.RLock()
        self._running: _Running | None = None
        self._ledger = _Ledger()
        #: OOM kills recorded by the starts already taken down: each start
        #: makes the cgroup afresh at the same path, so its final count is
        #: read before its cgroup is removed and carried here.
        self._retired_oom = 0
        #: The folder's lease is in doubt: the sandbox is frozen and starts
        #: no kernel until it is confirmed.
        self._fenced = False

    # -- identities -------------------------------------------------------------

    @property
    def tree_uid(self) -> int:
        """The workspace's tree identity: PID 1 and the shared trees' owner."""
        return self._ensure_uid(self.config.workspace)

    @property
    def gid(self) -> int:
        """The workspace's files group, every kernel's and the build uid's
        primary group: the one the workspace's agents and members write the
        tree through (:func:`~alkera_cli.harness.sandbox_uid.workspace_files_gid`).
        ``files_gid`` stands in for it where a test runs as itself."""
        if self._files_gid is not None:
            return self._files_gid
        return workspace_files_gid(self.config.workspace, ensure=self._ensure_uid)

    def slot_identities(self) -> tuple[KernelIdentity, ...]:
        """One uid per kernel slot, from the org's range, the same across
        starts (a kernel's files keep an owner that means something)."""
        return tuple(
            KernelIdentity(
                uid=self._ensure_uid(f"kernel:{self.config.workspace}:{slot}"),
                gid=self.gid,
                slot=slot,
            )
            for slot in range(self.config.max_kernels)
        )

    def build_identity(self) -> KernelIdentity:
        """Who environment builds and installs run as: a uid of the
        workspace's own beside the kernels', with the files group, so what a
        build writes stays the workspace's to change (slot ``-1``)."""
        return KernelIdentity(
            uid=self._ensure_uid(f"kernel:{self.config.workspace}:build"), gid=self.gid, slot=-1
        )

    def identity_of(self, kernel_id: str) -> KernelIdentity | None:
        with self._lock:
            return self._ledger.held.get(kernel_id)

    def claim(self, kernel_id: str) -> KernelIdentity:
        """A uid for ``kernel_id``: the lowest free slot's, after every process
        that uid still runs in the sandbox is killed, so no process of an
        earlier kernel lives on under the new one's uid. Starts the sandbox
        when it is not running. Refuses when every slot is held."""
        with self._lock:
            if self._fenced:
                raise FolderFencedError()
            held = self._ledger.held.get(kernel_id)
            if held is not None:
                return held
            self.ensure_running()
            taken = {k.slot for k in self._ledger.held.values()}
            free = [k for k in self.slot_identities() if k.slot not in taken]
            if not free:
                raise SandboxRefusedError(
                    f"this workspace already runs {self.config.max_kernels} kernels"
                )
            identity = free[0]
            user = (identity.uid, identity.gid)
            # Nothing to kill is the usual answer, not a failure.
            self._quiet(exec_argv(self.target, user, ("kill", "-KILL", "--", "-1")))
            self._ledger.held[kernel_id] = identity
            self._ledger.idle_since = None
            return identity

    def release(self, kernel_id: str) -> None:
        """``kernel_id`` has exited: its slot is free, and the idle clock starts
        when it was the last."""
        with self._lock:
            self._ledger.held.pop(kernel_id, None)
            if not self._ledger.held:
                self._ledger.idle_since = self._clock()

    def kernels(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._ledger.held)

    # -- lifecycle --------------------------------------------------------------

    def running(self) -> bool:
        with self._lock:
            return self._running is not None and self._running.proc.poll() is None

    def ensure_running(self) -> None:
        """Start the sandbox unless it is up. A sandbox whose ``runsc run``
        exited (the host's OOM group kill, a guard's ``cgroup.kill``) is taken
        down and started again."""
        with self._lock:
            if self._fenced:
                raise FolderFencedError()
            if self.running():
                return
            if self._running is not None:
                self._teardown()
            self._start()

    @property
    def fenced(self) -> bool:
        return self._fenced

    def fence(self) -> None:
        """Freeze every process of the sandbox (``cgroup.freeze``): no kernel
        writes the folder while its lease is in doubt, and none starts."""
        with self._lock:
            self._fenced = True
            self._freeze("1")

    def unfence(self) -> None:
        """The lease is confirmed: the kernels go on where they stopped."""
        with self._lock:
            self._fenced = False
            self._freeze("0")

    def _freeze(self, value: str) -> None:
        running = self._running
        if running is None or running.cgroup_dir is None:
            return
        try:
            (running.cgroup_dir / "cgroup.freeze").write_text(value)
        except OSError as exc:
            logger.warning("kernel sandbox cgroup.freeze=%s failed: %s", value, exc)

    def plan_launch(self) -> SandboxLaunch:
        """The launch the workspace-sandbox seam composes for this sandbox."""
        plan = plan_workspace_sandbox(
            KERNEL_KIND,
            self.config.request(),
            settings=self._settings,
            cap=self._cap,
            ensure=self._ensure_uid,
        )
        return plan.launch({})

    def _start(self) -> None:
        config = self.config
        launch = self.plan_launch()
        if launch.command is None:
            raise SandboxRefusedError("the kernel sandbox has no command to run")
        run_steps(runtime_steps(config.runtime_dir, self.gid), run=self._run, repair=self._repair)
        run_steps(launch.before, run=self._run, repair=self._repair)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        with open(config.state_dir / "runsc.log", "ab") as log:
            proc = self._spawn(launch.command, log)
        cgroup = launch.memory_events.parent if launch.memory_events is not None else None
        self._running = _Running(proc=proc, launch=launch, cgroup_dir=cgroup)
        deadline = self._clock() + self._ready_timeout
        probe = exec_argv(self.target, (self.tree_uid, self.gid), ("true",))
        while self._quiet(probe) != 0:
            if proc.poll() is not None or self._clock() >= deadline:
                self._teardown()
                raise SandboxRefusedError(
                    f"the workspace's kernel sandbox did not start: {self._log_tail()}"
                )
            self._sleep(0.02)
        self._ledger = _Ledger(idle_since=self._clock())
        logger.info("kernel sandbox for %s is up (%s)", config.workspace, config.container)

    def _log_tail(self, limit: int = 600) -> str:
        """The end of ``runsc``'s own account of the start, for the refusal."""
        try:
            data = (self.config.state_dir / "runsc.log").read_bytes()
        except OSError:
            return "runsc wrote nothing"
        return data[-limit:].decode("utf-8", "replace").strip() or "runsc wrote nothing"

    def _quiet(self, argv: Sequence[str]) -> int:
        """``argv``'s exit status, a failure logged only at debug: a probe the
        sandbox is not ready for yet, a kill with nothing to kill."""
        if self._run is run_argv:
            return run_argv(argv, quiet=True)
        return self._run(argv)

    def stop(self) -> None:
        """Take the sandbox down with every kernel in it."""
        with self._lock:
            if self._running is not None:
                self._teardown()
            self._ledger = _Ledger()

    def stop_if_idle(self) -> bool:
        """Stop the sandbox once it has held no kernel for the idle period.
        True when it was stopped."""
        with self._lock:
            idle = self._ledger.idle_since
            if self._running is None or self._ledger.held or idle is None:
                return False
            if self._clock() - idle < self.config.idle_stop_seconds:
                return False
            self._teardown()
            self._ledger = _Ledger()
            return True

    def _teardown(self) -> None:
        running = self._running
        self._running = None
        if running is None:
            return
        proc = running.proc
        if self._fenced and proc.poll() is None:
            # A frozen sandbox answers no signal: ended through its cgroup, and
            # never thawed first, which would let it write once more.
            self._kill_cgroup(running)
        if proc.poll() is None:
            # Asked first (runsc stops the container on SIGTERM), then every
            # process of the sandbox through its cgroup, then runsc itself.
            proc.terminate()
            try:
                proc.wait(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self._kill_cgroup(running)
                try:
                    proc.wait(timeout=self._stop_grace)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
        if running.cgroup_dir is not None:
            self._retired_oom += read_oom_events(running.cgroup_dir) or 0
        run_steps(running.launch.after_exit, run=self._run, repair=self._repair)
        logger.info("kernel sandbox for %s is down", self.config.workspace)

    # -- the cgroup -------------------------------------------------------------

    def cgroup_dir(self) -> Path | None:
        """The sandbox's host cgroup while it runs (what the memory source
        reads and :meth:`kill_all` writes), ``None`` otherwise."""
        with self._lock:
            return self._running.cgroup_dir if self._running is not None else None

    def oom_events(self) -> int:
        """Every ``oom_kill`` and ``oom_group_kill`` the sandbox's cgroups
        recorded, over every start: only ever grows, so an increment between
        two readings is an OOM whether or not the sandbox restarted between
        them."""
        with self._lock:
            current = self._running.cgroup_dir if self._running is not None else None
            live = read_oom_events(current) if current is not None else None
            return self._retired_oom + (live or 0)

    def kill_all(self) -> None:
        """The guard's last resort: kill every process of the sandbox through
        its cgroup (``cgroup.kill`` needs nothing from the sentry, whose kill
        is the one under memory pressure). The sandbox is then down; its
        kernels are gone with it and the next kernel starts it again."""
        with self._lock:
            running = self._running
            if running is None:
                return
            self._kill_cgroup(running)
            self._teardown()
            self._ledger = _Ledger()

    @staticmethod
    def _kill_cgroup(running: _Running) -> None:
        if running.cgroup_dir is None:
            return
        try:
            (running.cgroup_dir / "cgroup.kill").write_text("1")
        except OSError as exc:
            logger.warning("kernel sandbox cgroup.kill failed: %s", exc)

    # -- what kernels see -------------------------------------------------------

    def container_path(self, host: Path) -> str | None:
        """``host`` as a process in the sandbox sees it, or ``None`` when the
        sandbox does not hold it: the files tree at the home, each bind at its
        destination (the folder before a bind, a deeper bind before a
        shallower one)."""
        config = self.config
        views = [(config.folder, config.home)]
        binds = sorted(kernel_binds(config.trees), key=lambda b: len(b.source.parts), reverse=True)
        views += [(b.source, b.destination) for b in binds]
        for root, seen in views:
            spelled = relative_to_container(host, folder=root, home=seen)
            if spelled is not None:
                return spelled
        return None

    def data_path(self, kernel_id: str) -> Path:
        """``kernel_id``'s data directory: the engine makes it at
        ``<data_root>/kernels/<kernel id>``, its data root being this
        sandbox's runtime directory."""
        return kernel_data_dir(self.config.runtime_dir, checked_kernel_id(kernel_id))


__all__ = [
    "DATA_SUBDIR",
    "IDLE_STOP_SECONDS",
    "KERNEL_ID_RE",
    "KERNEL_KIND",
    "KERNEL_MOUNT",
    "KERNEL_NPROC",
    "OPEN_IN_WORKSPACE",
    "READY_TIMEOUT_SECONDS",
    "REAPER_SOURCE",
    "RUN_MOUNT",
    "SOCKET_SUBDIR",
    "FolderFencedError",
    "KernelIdentity",
    "KernelSandbox",
    "KernelSandboxConfig",
    "KernelSandboxKind",
    "KernelTrees",
    "PlacementRefusedError",
    "RunscTarget",
    "checked_kernel_id",
    "exec_argv",
    "kernel_binds",
    "place_notebook",
    "reaper_argv",
    "register_kernel_kind",
    "runtime_steps",
]
