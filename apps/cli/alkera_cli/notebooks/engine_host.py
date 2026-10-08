"""The notebook engines an org worker hosts: one per workspace, built from its tenancy.

Each workspace this worker holds gets one ``NotebookEngine`` (made on first
use) with the box's pieces in its seams: the workspace's kernel sandbox
behind a :class:`~alkera_cli.notebooks.launch_container.ContainerLauncher`,
the per-kernel Unix socket transport, the sandbox's host cgroup as the memory
source, environment builds run in the sandbox, and the document store and SQL
providers the integrator passes. Everything a workspace's notebooks keep on
the box lives under the org's root, partitioned by workspace (or by chat for
what is a chat's): the store kinds below say where.

The host takes the workspace down with the workspace: it registers itself as
a teardown of the workspace's put-away
(:func:`~alkera_cli.harness.workspace_sandbox.register_teardown`), which
suspends the engine and stops the kernel sandbox, and :meth:`sweep` stops a
sandbox whose last kernel stopped ten minutes ago.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol

from alkera_notebook.engine.config import EngineConfig, RandomIds, SystemClock
from alkera_notebook.engine.engine import NotebookEngine
from alkera_notebook.envs import TreeModes
from alkera_notebook.envs.template import TemplatedEnvRegistry

from alkera_cli.files.chat_fs import DIR_MODE, FILE_MODE
from alkera_cli.files.folder_fence import register_fence_holder
from alkera_cli.harness.sandbox import SandboxSettings
from alkera_cli.harness.sandbox_env import workspace_envs_root
from alkera_cli.harness.sandbox_layout import (
    WORKSPACE_KEY_PREFIX,
    chat_slug,
    envs_dir_for,
    workspace_dir_name,
)
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.harness.sandbox_steps import Runner, run_argv
from alkera_cli.harness.workspace_sandbox import register_teardown
from alkera_cli.notebooks.kernel_sandbox import (
    IDLE_STOP_SECONDS,
    KernelSandbox,
    KernelSandboxConfig,
    KernelTrees,
)
from alkera_cli.notebooks.launch_container import ContainerLauncher, SandboxCommandRunner
from alkera_cli.notebooks.memory_cgroup import CgroupSource
from alkera_cli.notebooks.system_install import SystemInstaller
from alkera_cli.notebooks.transport_box import BoxKernelTransport

if TYPE_CHECKING:
    from alkera_notebook.document.store import DocumentStore
    from alkera_notebook.sql.provider import SqlProviderRegistry

logger = logging.getLogger(__name__)

#: The ``PATH`` kernels and builds start from inside the sandbox; the
#: environment's own ``bin`` goes in front of it.
SANDBOX_PATH: Final = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: The shared tree's modes (setgid ``2770`` directories, ``0660`` files), for
#: what the environment registry writes where the build uid reads it.
SHARED_TREE_MODES: Final = TreeModes(directory=DIR_MODE, file=FILE_MODE)

Partition = Literal["org", "workspace", "chat"]


@dataclass(frozen=True, slots=True)
class StoreKind:
    """One kind of state notebooks keep on a box, and whose it is: its
    partition names the directory it lives under (the org's root, one
    workspace's directory, one chat's), and nothing of one partition is ever
    kept in another's."""

    name: str
    partition: Partition
    subdir: str
    purpose: str


#: The notebook state on a box. A new kind is added here, with its partition.
NOTEBOOK_STORE_KINDS: Final[tuple[StoreKind, ...]] = (
    StoreKind(
        "kernel_state",
        "workspace",
        "kernel-state",
        "the kernel sandbox's bundle, root overlay, log and kernel pid files",
    ),
    StoreKind(
        "kernel_runtime",
        "workspace",
        "run",
        "the kernels' sockets and the engine's data directories, bound into the sandbox",
    ),
    StoreKind(
        "workspace_envs",
        "workspace",
        "envs",
        "the workspace's materialized environments, bound into its agents and kernels",
    ),
    StoreKind(
        "package_cache",
        "workspace",
        "envs",
        "the workspace's writable uv and pip caches, beside its environments",
    ),
    StoreKind(
        "notebook_digest_cursor",
        "chat",
        "notebooks",
        "how far an agent's digest of a notebook has read, per chat",
    ),
)


def store_kind(name: str) -> StoreKind:
    for kind in NOTEBOOK_STORE_KINDS:
        if kind.name == name:
            return kind
    raise KeyError(f"no notebook store kind {name!r}")


@dataclass(frozen=True, slots=True)
class WorkspaceTenancy:
    """What the worker knows of one workspace, from its tenancy scope: the
    custody key (whose uid in the org's range owns the tree), its files tree
    on this box and its environments."""

    key: str
    folder: Path
    envs_dir: Path


@dataclass(frozen=True, slots=True)
class NotebookHostSettings:
    """The box's settings for every kernel sandbox this worker starts: the
    box's sandbox settings and what its probe found, the staged platform
    mount, and each sandbox's limits."""

    sandbox: SandboxSettings
    capability: SandboxCapability
    platform_mount: Path
    memory_mb: int
    vcpu: int
    max_kernels: int = 4
    idle_stop_seconds: float = IDLE_STOP_SECONDS
    cgroup_root: Path = Path("/sys/fs/cgroup")
    runsc_root: Path = Path("/run/alkera-runsc")


@dataclass(slots=True)
class WorkspaceParts:
    """The box's pieces for one workspace's engine."""

    tenancy: WorkspaceTenancy
    sandbox: KernelSandbox
    launcher: ContainerLauncher
    transport: BoxKernelTransport
    memory: CgroupSource
    runner: SandboxCommandRunner
    installer: SystemInstaller
    #: ``EngineConfig``'s paths: the files tree, the environments, the data
    #: root (the sandbox's runtime directory) and the platform mount.
    engine_paths: Mapping[str, str] = field(default_factory=dict)


class HostedEngine(Protocol):
    """What the host asks of an engine it holds."""

    async def suspend(self) -> object: ...

    async def close(self) -> None: ...


EngineFactory = Callable[[WorkspaceParts], HostedEngine]


def notebook_engine_factory(
    *,
    store_for: Callable[[WorkspaceTenancy], DocumentStore],
    sql: SqlProviderRegistry,
    python_home: str,
    default_template: Path,
    org_id: str = "",
) -> EngineFactory:
    """Builds the engine of each workspace from the box's pieces: the
    launcher, transport and memory source in its seams, environments built in
    the kernel sandbox, and the document store and SQL providers given here
    (the platform's Loro store and connection providers)."""

    def build(parts: WorkspaceParts) -> HostedEngine:
        paths = parts.engine_paths
        home = str(parts.tenancy.folder)
        base_env = {"PATH": SANDBOX_PATH, "HOME": home, "LANG": "C.UTF-8"}
        config = EngineConfig(
            workspace_root=paths["workspace_root"],
            env_root=paths["env_root"],
            data_root=paths["data_root"],
            kernel_mount=paths["kernel_mount"],
            workspace_id=sql_workspace_id(parts.tenancy.key),
            org_id=org_id,
            max_kernels=parts.sandbox.config.max_kernels,
            base_env=base_env,
            # Every member's container and the kernel sandbox bind the one
            # environments directory (a kernel sandbox runs under gVisor).
            shared_envs=True,
        )
        envs = TemplatedEnvRegistry(
            paths["workspace_root"],
            paths["env_root"],
            runner=parts.runner,
            python=f"{python_home}/bin/python3",
            base_env=base_env,
            # Builds run in the kernel sandbox, whose egress is the
            # workspace's policy: installs reach the package index through it.
            offline=False,
            # A workspace's ``default`` environment starts from this.
            default_template=default_template,
            # Builds run as the workspace's build uid, which shares the tree's
            # files group and nothing else: what the registry writes on a
            # build's path follows the tree's shared modes.
            tree_modes=SHARED_TREE_MODES,
        )
        return NotebookEngine(
            config,
            store=store_for(parts.tenancy),
            launcher=parts.launcher,
            transport=parts.transport,
            memory=parts.memory,
            sql=sql,
            envs=envs,
            clock=SystemClock(),
            ids=RandomIds(),
        )

    return build


def sql_workspace_id(key: str) -> str:
    """The id a workspace's kernels act for (``SqlWorkspace.id``): the
    workspace's own id for a workspace's custody key (``ws:<id>``), which
    is what its connections are attached to and leased for; a chat's id for
    a chat on its own."""
    return key.removeprefix(WORKSPACE_KEY_PREFIX) or key


EnsureUid = Callable[[str], int]


def stage_platform_mount(boot: Path, package: Path, public: Path | None, under: Path) -> Path:
    """A copy of the kernel's platform files (``boot.py``, ``_alkera_kernel``
    and, under ``public/``, the ``alkera`` package) under ``under``, named by
    their content, so a sandbox that is running keeps the files it started
    with. Real files, not links: a link to the host's site-packages means
    nothing inside the sandbox. Readable by every kernel uid and writable by
    none (directories 0755, files 0644, whatever the worker's umask), a copy
    an earlier build left closed included."""
    digest = hashlib.sha256()
    # Named as the mount lays them out: boot.py at its root, the package and
    # the public package beside it.
    digest.update(b"boot.py")
    digest.update(boot.read_bytes())
    for tree, name in ((package, "_alkera_kernel"), *(((public, public.name),) if public else ())):
        for path in sorted(tree.rglob("*.py")):
            digest.update(f"{name}/{path.relative_to(tree).as_posix()}".encode())
            digest.update(path.read_bytes())
    target = under / digest.hexdigest()[:16]
    if (target / "boot.py").is_file():
        _open_to_kernels(target)
        return target
    staging = under / f".{target.name}.staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copy2(boot, staging / "boot.py")
    shutil.copytree(package, staging / "_alkera_kernel", ignore=ignore)
    if public is not None:
        shutil.copytree(public, staging / "public" / public.name, ignore=ignore)
    _open_to_kernels(staging)
    staging.rename(target)
    return target


#: The platform mount's modes: kernels (other uids) read it, only its owner writes.
MOUNT_DIR_MODE: Final = 0o755
MOUNT_FILE_MODE: Final = 0o644


def _open_to_kernels(root: Path) -> None:
    root.chmod(MOUNT_DIR_MODE)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        path.chmod(MOUNT_DIR_MODE if path.is_dir() else MOUNT_FILE_MODE)


def tenancy_of(key: str, folder: Path, *, state_dir: Path | None = None) -> WorkspaceTenancy:
    """The tenancy of the workspace with custody key ``key`` whose files tree
    on this box is ``folder``: its environments where every member's agent
    finds them (a chat on its own, ``state_dir``'s ``envs``)."""
    workspace = None if state_dir is not None else key
    envs = envs_dir_for(
        state_dir=state_dir or folder, workspace=workspace, envs_root=workspace_envs_root()
    )
    return WorkspaceTenancy(key=key, folder=folder, envs_dir=envs)


class NotebookEngineHost:
    """Every notebook engine one org worker holds, one per workspace."""

    def __init__(
        self,
        *,
        org_root: Path,
        settings: NotebookHostSettings,
        ensure_uid: EnsureUid,
        engine_factory: EngineFactory,
        run: Runner = run_argv,
    ) -> None:
        self.org_root = org_root
        self._run = run
        self.settings = settings
        self._ensure_uid = ensure_uid
        self._factory = engine_factory
        self._parts: dict[str, WorkspaceParts] = {}
        self._engines: dict[str, HostedEngine] = {}
        self._lock = asyncio.Lock()

    # -- where a workspace's state lives ---------------------------------------

    def partition_dir(self, kind: StoreKind, key: str) -> Path:
        """Where ``kind`` lives for the workspace (or chat) ``key``. The
        runtime directory is kept short, since a socket path is."""
        if kind.partition == "chat":
            raise ValueError(f"{kind.name} is a chat's: it lives in the chat's own directory")
        if kind.name == "kernel_runtime":
            return self.org_root / kind.subdir / chat_slug(key)
        if kind.partition == "org":
            return self.org_root / kind.subdir
        return self.org_root / kind.subdir / workspace_dir_name(key)

    def sandbox_config(self, tenancy: WorkspaceTenancy) -> KernelSandboxConfig:
        settings = self.settings
        return KernelSandboxConfig(
            workspace=tenancy.key,
            folder=tenancy.folder,
            trees=KernelTrees(
                envs_dir=tenancy.envs_dir,
                platform_mount=settings.platform_mount,
                runtime_dir=self.partition_dir(store_kind("kernel_runtime"), tenancy.key),
                cgroup_root=settings.cgroup_root,
                runsc_root=settings.runsc_root,
            ),
            state_dir=self.partition_dir(store_kind("kernel_state"), tenancy.key),
            memory_mb=settings.memory_mb,
            vcpu=settings.vcpu,
            max_kernels=settings.max_kernels,
            idle_stop_seconds=settings.idle_stop_seconds,
            home=settings.sandbox.home,
            python_home=settings.capability.python_home or settings.sandbox.python_home,
        )

    # -- the pieces and the engine ---------------------------------------------

    def parts(self, tenancy: WorkspaceTenancy) -> WorkspaceParts:
        """The box's pieces for ``tenancy``'s workspace, made once."""
        held = self._parts.get(tenancy.key)
        if held is not None:
            if held.tenancy != tenancy:
                raise ValueError(f"workspace {tenancy.key} is already held with another tree")
            return held
        config = self.sandbox_config(tenancy)
        sandbox = KernelSandbox(
            config,
            settings=self.settings.sandbox,
            cap=self.settings.capability,
            ensure_uid=self._ensure_uid,
            run=self._run,
        )
        parts = WorkspaceParts(
            tenancy=tenancy,
            sandbox=sandbox,
            launcher=ContainerLauncher(sandbox),
            transport=BoxKernelTransport(config.runtime_dir),
            memory=CgroupSource(sandbox, limit=config.memory_mb * 1024 * 1024),
            runner=SandboxCommandRunner(sandbox),
            installer=SystemInstaller(sandbox),
            engine_paths={
                "workspace_root": str(tenancy.folder),
                "env_root": str(tenancy.envs_dir),
                "data_root": str(config.runtime_dir),
                "kernel_mount": str(config.platform_mount),
            },
        )
        self._parts[tenancy.key] = parts
        return parts

    def engine_for(self, tenancy: WorkspaceTenancy) -> HostedEngine:
        """``tenancy``'s workspace's engine, made on first use. Making it
        starts nothing: the kernel sandbox starts with the first kernel.
        Nothing here awaits, so on the event loop's thread it is made once."""
        engine = self._engines.get(tenancy.key)
        if engine is None:
            engine = self._factory(self.parts(tenancy))
            self._engines[tenancy.key] = engine
        return engine

    async def engine(self, tenancy: WorkspaceTenancy) -> HostedEngine:
        """:meth:`engine_for`, taken in turn with :meth:`put_away`."""
        async with self._lock:
            return self.engine_for(tenancy)

    def held(self) -> frozenset[str]:
        return frozenset(self._parts)

    # -- taking it down ---------------------------------------------------------

    async def put_away(self, key: str) -> None:
        """The workspace was put away: suspend and close its engine, stop its
        kernel sandbox (and with it every system package installed there)."""
        async with self._lock:
            engine = self._engines.pop(key, None)
            parts = self._parts.pop(key, None)
        if engine is not None:
            try:
                await engine.suspend()
            except Exception:
                logger.exception("workspace %s: its notebook engine did not suspend", key)
            try:
                await engine.close()
            except Exception:
                logger.exception("workspace %s: its notebook engine did not close", key)
        if parts is not None:
            await asyncio.to_thread(parts.sandbox.stop)

    async def follow_fences(self, fenced: Callable[[str], bool]) -> None:
        """Freeze the kernels of every workspace whose folder lease is in doubt
        and thaw the rest (``alkera_cli.files.folder_fence``)."""
        for key, parts in list(self._parts.items()):
            should = fenced(key)
            if should and not parts.sandbox.fenced:
                logger.warning("workspace %s: its folder lease is in doubt; kernels frozen", key)
                await asyncio.to_thread(parts.sandbox.fence)
            elif not should and parts.sandbox.fenced:
                logger.info("workspace %s: its folder lease is confirmed; kernels thawed", key)
                await asyncio.to_thread(parts.sandbox.unfence)

    async def sweep(self) -> list[str]:
        """Stop every kernel sandbox whose last kernel stopped longer ago than
        the idle period; the engines stay. Returns the workspaces stopped."""
        stopped: list[str] = []
        for key, parts in list(self._parts.items()):
            if await asyncio.to_thread(parts.sandbox.stop_if_idle):
                stopped.append(key)
        return stopped

    def register(self) -> None:
        """Take each workspace's notebooks down when the workspace is put away,
        and freeze them while its folder lease is in doubt."""
        register_teardown(self.put_away)
        register_fence_holder(self.follow_fences)


__all__ = [
    "NOTEBOOK_STORE_KINDS",
    "SANDBOX_PATH",
    "EngineFactory",
    "HostedEngine",
    "NotebookEngineHost",
    "NotebookHostSettings",
    "StoreKind",
    "WorkspaceParts",
    "WorkspaceTenancy",
    "notebook_engine_factory",
    "sql_workspace_id",
    "stage_platform_mount",
    "store_kind",
    "tenancy_of",
]
