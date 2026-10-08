"""The local developer box: a pool box run as a Docker container on the
developer's own machine, with every chat under gVisor.

An EC2 box costs money and a RunPod box cannot run gVisor, so ``localdev`` is
the place to iterate on the box, the harness and the sandbox. It is a
privileged container on the developer's Docker (OrbStack or Docker Desktop on
an arm64 Mac, or Docker on Linux). The backend renders the same node bootstrap
as for EC2 (``bootstrap.py``'s ``localdev`` branch) and runs it as the
container's command, so the sandbox prerequisites, the ``runsc`` gate and the
claim all run as on a pool box, with the daemon from the developer's source
tree mounted read-only.

It refuses outside ``APP_ENV=local``: every operation raises
:class:`ComputeProviderUnavailableError`.

The container runs under Docker's ``unless-stopped`` restart policy, and
everything needed to start it again (the rendered bootstrap, the node secrets,
the launch) is kept in a per-allocation state directory, so
:meth:`LocaldevProvider.ensure_running` can start a stopped container or
recreate a deleted one. The backend's keep-alive pass calls it for every box
that should be serving. While a box can still be brought back,
:meth:`~LocaldevProvider.describe` reports ``starting`` rather than ``gone``,
so the reconcile never marks it lost before the keep-alive restarts it.

Docker is reached through one injectable runner (:class:`DockerRunner`), so
tests need no Docker.

A box runs the Linux opencode harness staged in the source tree by
``ops/scripts/dev/build-localdev-agent.sh``. When it is missing the provider
runs that script once through an injectable :data:`AgentBuilder`, and refuses
the start with an ``invalid`` provider error naming the script if the build
does not stage it, rather than booting a box that restarts forever.

Every box is labelled with this checkout's ``COMPOSE_PROJECT_NAME`` and every
listing filters by it, so two worktrees with their own backends never see,
adopt or reap each other's boxes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

from alkera_core.compute.availability import Availability, SizeQuery
from alkera_core.compute.bootstrap import (
    BOX_HOME,
    DATA_MOUNT,
    LOCALDEV_AGENT_DIR,
    LOCALDEV_AGENT_SOURCE,
    LOCALDEV_CONTAINER_ENV,
    LOCALDEV_FORWARD_ENV,
    LOCALDEV_HOST,
    LOCALDEV_SOURCE,
    LOCALDEV_VENV,
)
from alkera_core.compute.disk import DiskBounds, DiskRules, DiskRulesTable, GrowRule
from alkera_core.compute.org_machines import ProviderTimings, register_timings
from alkera_core.compute.provider import (
    GONE,
    INVALID_FAILURE,
    LOCALDEV,
    RUNNING,
    STARTING,
    UNKNOWN,
    ComputeProvider,
    ComputeProviderError,
    ComputeProviderUnavailableError,
    NodeDescription,
    NodeLaunch,
    PodPhase,
    PodStatus,
    ProviderPod,
    ProviderTraits,
    register_provider,
)
from alkera_core.db.locking import io_boundary_class
from alkera_core.process import SpawnSpec, run_async

if TYPE_CHECKING:
    from alkera_core.config import Settings
    from alkera_core.models.compute import ComputeMachineType

#: The one machine type a local box is minted against.
LOCALDEV_TYPE_CODE = "local"
#: How many boxes the catalog says one machine may run; a developer rarely
#: needs more than one, and each holds a gVisor rootfs and a venv.
LOCALDEV_MAX_BOXES = 4
#: The image every local box runs, tagged with a digest of its build context so
#: a change to the Dockerfile builds a new image instead of reusing a stale one.
IMAGE_REPOSITORY = "alkera-localdev-box"
#: The build context, relative to the source root.
IMAGE_CONTEXT = Path("deploy/docker/localdev-box")
#: Where the bootstrap is mounted inside the box.
BOOT_SCRIPT = "/opt/alkera-boot/bootstrap.sh"
#: Volumes every local box shares: uv's download cache and its Python builds.
SHARED_VOLUMES: tuple[tuple[str, str], ...] = (
    ("alkera-localdev-uv-cache", "/opt/uv-cache"),
    ("alkera-localdev-uv-python", "/opt/uv-python"),
)
#: Volumes each box owns, by suffix, and where they mount. They outlive the
#: container, so a recreated box keeps its staged rootfs, its venv, its chats'
#: folders and its login; a terminate removes them.
BOX_VOLUMES: tuple[tuple[str, str], ...] = (
    ("root", "/opt/alkera"),
    ("home", BOX_HOME),
    ("work", DATA_MOUNT),
    ("venv", LOCALDEV_VENV),
)
#: Labels every box carries. The project label scopes every listing to this
#: checkout's backend.
LABEL_LOCALDEV = "alkera.localdev"
LABEL_PROJECT = "alkera.project"
LABEL_ALLOCATION = "alkera.allocation_id"
LABEL_POD_NAME = "alkera.pod_name"
#: A digest of the bootstrap the container was started with. With its image,
#: it says whether the box runs what the source tree renders now.
LABEL_BOOT = "alkera.boot_digest"
#: How long ``docker stop`` lets the daemon hand its chats on before it kills
#: it. A developer's stop should not wait out the six-hour drain ceiling a pool
#: box honours; a minute hands every chat back.
STOP_TIMEOUT_SECONDS = 60
#: How long one Docker call may take before it is a provider error. A build
#: pulls Ubuntu and installs packages, so it gets its own, longer budget.
DOCKER_TIMEOUT_SECONDS = 60.0
#: ``docker run`` and ``docker start`` bring a privileged container up, which a
#: Docker busy with other worktrees' stacks can take minutes to do.
DOCKER_START_TIMEOUT_SECONDS = 300.0
BUILD_TIMEOUT_SECONDS = 1800.0
#: The script that stages the box's Linux harness, relative to the source root.
AGENT_BUILD_SCRIPT = Path("ops/scripts/dev/build-localdev-agent.sh")
#: The secret names a box's env file may carry: the node secrets the
#: provisioning mints, and nothing else.
_SECRET_NAME = re.compile(r"^ALKERA_[A-Z0-9_]+$")
_NO_SUCH_OBJECT = re.compile(r"no such (object|container)", re.IGNORECASE)

_PHASES: dict[str, PodPhase] = {
    "running": RUNNING,
    "created": STARTING,
    "restarting": STARTING,
    "paused": UNKNOWN,
    "removing": GONE,
    "exited": STARTING,
    "dead": STARTING,
}
"""Docker's container states onto :data:`PodPhase`. A stopped (``exited``) or
``dead`` container is ``starting``, not ``gone``: the box can be started again
from what it keeps, and the keep-alive does exactly that for a box that should
be serving. Only a container that no longer exists, and that cannot be
recreated, is ``gone``."""


@dataclass(frozen=True)
class DockerResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def missing(self) -> bool:
        """Docker answered that the container (or object) does not exist."""
        return not self.ok and bool(_NO_SUCH_OBJECT.search(self.stderr))


DockerRunner = Callable[[Sequence[str], float], Awaitable[DockerResult]]
"""Runs ``docker <args>`` with a timeout. The one seam a test replaces."""


class DockerTimeoutError(ComputeProviderError):
    """The ``docker`` CLI gave up waiting. Docker itself may still have done
    what it was asked, so the caller looks again before calling it a failure."""


async def run_docker(args: Sequence[str], deadline_s: float) -> DockerResult:
    """The real :data:`DockerRunner`: the ``docker`` CLI on ``PATH``."""
    if shutil.which("docker") is None:
        raise ComputeProviderError("the docker CLI is not on PATH; start OrbStack or Docker")
    spec = SpawnSpec(argv=["docker", *args], env=os.environ, stdout="pipe", stderr="pipe")
    try:
        done = await run_async(spec, time_limit=deadline_s)
    except subprocess.TimeoutExpired as exc:
        raise DockerTimeoutError(f"docker {args[0]} did not answer in {deadline_s:.0f}s") from exc
    return DockerResult(
        done.returncode, done.stdout.decode(errors="replace"), done.stderr.decode(errors="replace")
    )


AgentBuilder = Callable[[Path, float], Awaitable[DockerResult]]
"""Runs the harness build script in a source tree with a timeout. The seam a
test replaces, so a launch is driven with no Docker or bun."""


async def run_agent_build(source_root: Path, deadline_s: float) -> DockerResult:
    """The real :data:`AgentBuilder`: ``bash`` on the script in the source tree."""
    spec = SpawnSpec(
        argv=["bash", str(source_root / AGENT_BUILD_SCRIPT)],
        env=os.environ,
        cwd=source_root,
        stdout="pipe",
        stderr="pipe",
    )
    try:
        done = await run_async(spec, time_limit=deadline_s)
    except subprocess.TimeoutExpired:
        return DockerResult(124, "", f"the harness build did not finish in {deadline_s:.0f}s")
    return DockerResult(
        done.returncode, done.stdout.decode(errors="replace"), done.stderr.decode(errors="replace")
    )


def default_source_root() -> Path:
    """The checkout this backend runs from: the workspace root above
    ``packages/api-core/alkera_core``."""
    import alkera_core

    return Path(alkera_core.__file__).resolve().parents[3]


def loopback_targets(urls: Sequence[str | None]) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """The ports a box must forward to the developer's machine, and the
    ``*.localhost`` names it must resolve to its own loopback, for the
    ``localhost`` URLs the backend hands a box (a presigned Files URL names the
    store at ``localhost:<port>``; the Files content origin is
    ``files.localhost:<port>``). A URL on any other host needs neither."""
    ports: set[int] = set()
    names: set[str] = set()
    for url in urls:
        if not url:
            continue
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host in ("localhost", "127.0.0.1") or host.endswith(".localhost"):
            ports.add(parts.port or (443 if parts.scheme == "https" else 80))
            if host.endswith(".localhost"):
                names.add(host)
    return tuple(sorted(ports)), tuple(sorted(names))


def _keep_script(box: Path, script: str) -> None:
    """Keep ``script`` as the box's bootstrap, rewriting the file in place
    (the container mounts it) only when it changed."""
    path = box / "bootstrap.sh"
    if path.is_file() and path.read_text() == script:
        return
    box.mkdir(parents=True, exist_ok=True)
    path.write_text(script)


def _digest(path: Path) -> str:
    """The kept bootstrap's digest, or ``""`` when none is kept."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def image_tag(context: Path) -> str:
    """``alkera-localdev-box:<digest>``, the digest taken over every file in
    the build context, so an edited Dockerfile is a new image."""
    digest = hashlib.sha256()
    for path in sorted(p for p in context.rglob("*") if p.is_file()):
        digest.update(path.relative_to(context).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return f"{IMAGE_REPOSITORY}:{digest.hexdigest()[:12]}"


@dataclass
@io_boundary_class("compute.localdev")
class LocaldevProvider:
    """The ``localdev`` compute provider. See the module docstring."""

    #: Whether this deployment is local; every operation refuses otherwise.
    enabled: bool
    #: This checkout's Docker project; it scopes every name and listing.
    project: str
    #: The source tree a box mounts read-only and runs its daemon from.
    source_root: Path
    #: Where each allocation's bootstrap, secrets and launch are kept.
    state_dir: Path
    #: Ports and ``*.localhost`` names a box forwards to the developer's machine.
    forward_ports: tuple[int, ...] = ()
    loopback_names: tuple[str, ...] = ()
    docker: DockerRunner = run_docker
    build_agent: AgentBuilder = run_agent_build
    stop_timeout: int = STOP_TIMEOUT_SECONDS
    kind: str = field(default=LOCALDEV, init=False)

    # -- guards and names ------------------------------------------------------

    def configured(self) -> bool:
        return self.enabled and (self.source_root / "pyproject.toml").is_file()

    def _require(self) -> None:
        if not self.enabled:
            raise ComputeProviderUnavailableError(
                "localdev boxes run only in a local deployment (APP_ENV=local)"
            )
        if not (self.source_root / "pyproject.toml").is_file():
            raise ComputeProviderUnavailableError(
                f"no source tree at {self.source_root} for a localdev box to run"
            )

    def container_name(self, allocation_id: UUID) -> str:
        return f"{self.project}-box-{allocation_id.hex[:12]}"

    def _box_dir(self, allocation_id: UUID) -> Path:
        return self.state_dir / str(allocation_id)

    def _allocation_of(self, machine_id: str) -> UUID | None:
        """The allocation a container name was started for, from its state."""
        if not self.state_dir.is_dir():
            return None
        for launch in self.state_dir.glob("*/launch.json"):
            try:
                data = json.loads(launch.read_text())
            except (OSError, ValueError):
                continue
            if data.get("container") == machine_id:
                try:
                    return UUID(launch.parent.name)
                except ValueError:
                    return None
        return None

    def _recoverable(self, allocation_id: UUID | None) -> bool:
        """Whether everything needed to start the box again is kept."""
        if allocation_id is None:
            return False
        box = self._box_dir(allocation_id)
        return all(
            (box / name).is_file() for name in ("launch.json", "bootstrap.sh", "secrets.env")
        )

    async def _docker(self, *args: str, deadline_s: float = DOCKER_TIMEOUT_SECONDS) -> DockerResult:
        return await self.docker(args, deadline_s)

    # -- the node's credential -------------------------------------------------

    async def store_credential(self, allocation_id: UUID, secrets: dict[str, str]) -> None:
        """Keep the node secrets in the box's env file (0600), which ``docker
        run`` reads, so a recreated box boots with the same credential."""
        self._require()
        lines = []
        for name, value in secrets.items():
            if not _SECRET_NAME.match(name) or any(c in value for c in "\r\n\0"):
                raise ComputeProviderError(f"refusing node secret {name!r}")
            lines.append(f"{name}={value}\n")
        box = self._box_dir(allocation_id)
        box.mkdir(parents=True, exist_ok=True)
        path = box / "secrets.env"
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text("".join(lines))

    async def delete_credential(self, allocation_id: UUID) -> None:
        (self._box_dir(allocation_id) / "secrets.env").unlink(missing_ok=True)

    async def bind_credential(self, allocation_id: UUID, machine_id: str) -> None:
        """Nothing to bind: the secrets travel with the container."""

    # -- the image ------------------------------------------------------------

    async def ensure_image(self) -> str:
        """The image tag, building it first if this machine does not have it."""
        context = self.source_root / IMAGE_CONTEXT
        tag = image_tag(context)
        if (await self._docker("image", "inspect", tag)).ok:
            return tag
        built = await self._docker(
            "build", "-t", tag, str(context), deadline_s=BUILD_TIMEOUT_SECONDS
        )
        if not built.ok:
            raise ComputeProviderError(f"building {tag} failed: {built.stderr.strip()[-400:]}")
        return tag

    # -- the harness ----------------------------------------------------------

    def agent_staged(self) -> bool:
        """Whether the Linux harness the box installs is in the source tree."""
        staged = self.source_root / LOCALDEV_AGENT_SOURCE
        return all(
            (staged / name).is_file() and os.access(staged / name, os.X_OK)
            for name in ("opencode", "rg")
        )

    async def ensure_agent(self) -> None:
        """Stage the harness when it is missing, by running its build once.
        Raises an ``invalid`` :class:`ComputeProviderError` naming the script
        when it is still missing after that: a box without it cannot serve."""
        if self.agent_staged():
            return
        built = await self.build_agent(self.source_root, BUILD_TIMEOUT_SECONDS)
        if built.ok and self.agent_staged():
            return
        said = (built.stderr.strip() or built.stdout.strip())[-300:]
        raise ComputeProviderError(
            f"no Linux harness at {LOCALDEV_AGENT_SOURCE}; run {AGENT_BUILD_SCRIPT}"
            + (f" (the automatic build failed: {said})" if said and not built.ok else ""),
            kind=INVALID_FAILURE,
        )

    # -- start ----------------------------------------------------------------

    async def run(self, launch: NodeLaunch) -> str:
        """Keep what the box needs to start again, then start it."""
        self._require()
        box = self._box_dir(launch.allocation_id)
        if not (box / "secrets.env").is_file():
            raise ComputeProviderError("the node secrets were not stored before the box started")
        await self.ensure_agent()
        name = self.container_name(launch.allocation_id)
        _keep_script(box, launch.script)
        (box / "launch.json").write_text(
            json.dumps({"container": name, "pod_name": launch.name, "tags": launch.tags})
        )
        await self.ensure_running(name)
        return name

    def _run_args(self, allocation_id: UUID, name: str, pod_name: str, image: str) -> list[str]:
        box = self._box_dir(allocation_id)
        args = [
            "run",
            "--detach",
            "--name",
            name,
            "--hostname",
            name[:63],
            # gVisor builds a network namespace, a veth pair and cgroups per chat
            # and loads an nftables ruleset: a pool box is a whole host, and a
            # container stands in for one only when it is privileged.
            "--privileged",
            "--restart",
            "unless-stopped",
            "--stop-timeout",
            str(self.stop_timeout),
            "--add-host",
            f"{LOCALDEV_HOST}:host-gateway",
            "--label",
            f"{LABEL_LOCALDEV}=true",
            "--label",
            f"{LABEL_PROJECT}={self.project}",
            "--label",
            f"{LABEL_ALLOCATION}={allocation_id}",
            "--label",
            f"{LABEL_POD_NAME}={pod_name}",
            "--label",
            f"{LABEL_BOOT}={_digest(box / 'bootstrap.sh')}",
            "--env-file",
            str(box / "secrets.env"),
            "--env",
            f"{LOCALDEV_CONTAINER_ENV}={name}",
            "--env",
            f"{LOCALDEV_FORWARD_ENV}={' '.join(str(p) for p in self.forward_ports)}",
            # The daemon's harness resolver takes these first, ahead of the
            # host-built binary it would otherwise find in the source tree; the
            # boot installs them there (see the bootstrap's localdev branch).
            "--env",
            f"ALKERA_OPENCODE_BIN={LOCALDEV_AGENT_DIR}/opencode",
            "--env",
            f"ALKERA_RIPGREP_BIN={LOCALDEV_AGENT_DIR}/rg",
            "--volume",
            f"{self.source_root}:{LOCALDEV_SOURCE}:ro",
            "--volume",
            f"{box / 'bootstrap.sh'}:{BOOT_SCRIPT}:ro",
        ]
        for host in self.loopback_names:
            args += ["--add-host", f"{host}:127.0.0.1"]
        for suffix, target in BOX_VOLUMES:
            args += ["--volume", f"{name}-{suffix}:{target}"]
        for volume, target in SHARED_VOLUMES:
            args += ["--volume", f"{volume}:{target}"]
        return [*args, image, "bash", BOOT_SCRIPT]

    async def ensure_running(
        self, machine_id: str, *, script: str | None = None, may_restart: bool = False
    ) -> str:
        """Bring the box up whatever state it is in, on what the source tree
        renders now. ``script`` is the bootstrap the backend renders for the
        box today; it replaces the kept one, so no later start runs an older
        one. A box started from another bootstrap or image is stale: stopped,
        it is recreated rather than started; running, it is recreated only
        when ``may_restart`` (nothing is in flight on it), and otherwise keeps
        running until then.

        Returns ``running``, ``stale`` (running, recreated later), ``started``
        (a stopped container started), or ``recreated``. Raises
        :class:`ComputeProviderError` when it cannot be brought back."""
        self._require()
        allocation_id = self._allocation_of(machine_id)
        if script is not None and allocation_id is not None:
            _keep_script(self._box_dir(allocation_id), script)
        inspected = await self._docker("inspect", machine_id)
        if not inspected.ok and not inspected.missing:
            raise ComputeProviderError(f"docker inspect {machine_id}: {inspected.stderr.strip()}")
        if inspected.ok:
            row = (json.loads(inspected.stdout or "[]") or [{}])[0]
            running = (row.get("State") or {}).get("Status") == "running"
            stale = allocation_id is not None and await self._stale(allocation_id, row)
            if running and not (stale and may_restart):
                return "stale" if stale else "running"
            if not stale:
                started = await self._up(machine_id, "start", machine_id)
                if not started.ok:
                    raise ComputeProviderError(
                        f"docker start {machine_id}: {started.stderr.strip()}"
                    )
                return "started"
            removed = await self._docker("rm", "--force", machine_id)
            if not removed.ok:
                raise ComputeProviderError(f"docker rm {machine_id}: {removed.stderr.strip()}")
        if allocation_id is None or not self._recoverable(allocation_id):
            raise ComputeProviderError(f"{machine_id} is gone and nothing is kept to recreate it")
        launch = json.loads((self._box_dir(allocation_id) / "launch.json").read_text())
        await self.ensure_agent()
        image = await self.ensure_image()
        created = await self._up(
            machine_id,
            *self._run_args(allocation_id, machine_id, str(launch.get("pod_name") or ""), image),
        )
        if not created.ok:
            raise ComputeProviderError(f"docker run {machine_id}: {created.stderr.strip()[-400:]}")
        return "recreated"

    async def _stale(self, allocation_id: UUID, row: dict[str, Any]) -> bool:
        """Whether the container was started from another bootstrap or image
        than the ones the box would start from now."""
        config = row.get("Config") or {}
        labels = config.get("Labels") or {}
        script = self._box_dir(allocation_id) / "bootstrap.sh"
        if labels.get(LABEL_BOOT) != _digest(script):
            return True
        return config.get("Image") != image_tag(self.source_root / IMAGE_CONTEXT)

    async def _up(self, machine_id: str, *args: str) -> DockerResult:
        """``docker run`` or ``docker start``. When the CLI gives up but the box
        is up anyway, that is success: failing it would fail a provision whose
        container then runs with no row to account for it."""
        try:
            return await self._docker(*args, deadline_s=DOCKER_START_TIMEOUT_SECONDS)
        except DockerTimeoutError:
            inspected = await self._docker("inspect", "--format", "{{.State.Status}}", machine_id)
            if inspected.ok and inspected.stdout.strip() in ("running", "restarting"):
                return DockerResult(0, "", "")
            raise

    async def create_pod(
        self, *, name: str, machine_type: ComputeMachineType, ssh_public_key: str
    ) -> str:
        raise ComputeProviderUnavailableError(
            "a localdev box is a workspace node; a session pod needs a cloud provider"
        )

    # -- liveness -------------------------------------------------------------

    async def describe(self, machine_id: str) -> NodeDescription:
        self._require()
        inspected = await self._docker("inspect", "--format", "{{.State.Status}}", machine_id)
        if inspected.ok:
            raw = inspected.stdout.strip()
            return NodeDescription(machine_id, self.normalize_status(raw), raw)
        if not inspected.missing:
            raise ComputeProviderError(f"docker inspect {machine_id}: {inspected.stderr.strip()}")
        if self._recoverable(self._allocation_of(machine_id)):
            # Deleted, but everything to start it again is kept: the keep-alive
            # recreates it, so the reconcile must not mark the box lost meanwhile.
            return NodeDescription(machine_id, STARTING, "missing, recreatable")
        return NodeDescription(machine_id, GONE, "missing")

    async def pod_status(self, pod_id: str) -> PodStatus:
        described = await self.describe(pod_id)
        return PodStatus(pod_id=pod_id, phase=described.phase, raw_status=described.raw_status)

    async def _labelled(self, *filters: str) -> list[dict[str, Any]]:
        """``docker inspect`` of every box of this project matching ``filters``."""
        args = ["ps", "--all", "--quiet", "--filter", f"label={LABEL_PROJECT}={self.project}"]
        args += ["--filter", f"label={LABEL_LOCALDEV}=true"]
        for extra in filters:
            args += ["--filter", extra]
        listed = await self._docker(*args)
        if not listed.ok:
            raise ComputeProviderError(f"docker ps: {listed.stderr.strip()}")
        ids = listed.stdout.split()
        if not ids:
            return []
        inspected = await self._docker("inspect", *ids)
        if not inspected.ok:
            raise ComputeProviderError(f"docker inspect: {inspected.stderr.strip()}")
        rows = json.loads(inspected.stdout or "[]")
        return [row for row in rows if isinstance(row, dict)]

    async def find(self, allocation_id: UUID) -> NodeDescription | None:
        self._require()
        rows = await self._labelled(f"label={LABEL_ALLOCATION}={allocation_id}")
        if not rows:
            return None
        row = rows[0]
        raw = str((row.get("State") or {}).get("Status") or "")
        return NodeDescription(
            str(row.get("Name") or "").lstrip("/"), self.normalize_status(raw), raw
        )

    async def list_pods(self, *, name_prefix: str = "") -> list[ProviderPod]:
        self._require()
        pods = []
        for row in await self._labelled():
            labels = (row.get("Config") or {}).get("Labels") or {}
            pod_name = str(labels.get(LABEL_POD_NAME) or "")
            if name_prefix and not pod_name.startswith(name_prefix):
                continue
            raw = str((row.get("State") or {}).get("Status") or "")
            pods.append(
                ProviderPod(
                    pod_id=str(row.get("Name") or "").lstrip("/"),
                    name=pod_name,
                    created_at=_parse_created(row.get("Created")),
                    phase=self.normalize_status(raw),
                    raw_status=raw,
                )
            )
        return pods

    def normalize_status(self, raw_status: str) -> PodPhase:
        return _PHASES.get(raw_status.strip().lower(), UNKNOWN)

    # -- power / lifecycle ----------------------------------------------------

    async def stop(self, machine_id: str) -> None:
        """Stop the box; its daemon hands every chat on first. A box already
        gone is stopped."""
        self._require()
        result = await self._docker(
            "stop", "--time", str(self.stop_timeout), machine_id, deadline_s=self.stop_timeout + 30
        )
        if not result.ok and not result.missing:
            raise ComputeProviderError(f"docker stop {machine_id}: {result.stderr.strip()}")

    async def start(self, machine_id: str) -> None:
        await self.ensure_running(machine_id)

    async def grow_volume(self, machine_id: str, size_gb: int) -> None:
        """A local box's volume is a docker volume with no size of its own:
        it already holds whatever it is grown to."""
        self._require()

    async def terminate(self, machine_id: str) -> None:
        """Remove the box, its volumes and everything kept to recreate it. A box
        already gone counts as terminated."""
        self._require()
        result = await self._docker(
            "rm", "--force", "--volumes", machine_id, deadline_s=self.stop_timeout + 30
        )
        if not result.ok and not result.missing:
            raise ComputeProviderError(f"docker rm {machine_id}: {result.stderr.strip()}")
        for suffix, _ in BOX_VOLUMES:
            await self._docker("volume", "rm", "--force", f"{machine_id}-{suffix}")
        allocation_id = self._allocation_of(machine_id)
        if allocation_id is not None:
            shutil.rmtree(self._box_dir(allocation_id), ignore_errors=True)

    async def terminate_pod(self, pod_id: str) -> None:
        await self.terminate(pod_id)

    # -- catalog + availability -----------------------------------------------

    async def catalog_prices(self, sizes: list[SizeQuery] | None = None) -> dict[str, int]:
        return {LOCALDEV_TYPE_CODE: 0}

    async def catalog_entries(
        self, sizes: list[SizeQuery] | None = None
    ) -> dict[str, dict[str, Any]]:
        return {
            LOCALDEV_TYPE_CODE: {
                "price_nanos": 0,
                "availability": "HIGH" if self.enabled else "NONE",
                "max_count": LOCALDEV_MAX_BOXES,
                "gpu_name": "",
                "gpu_memory_gb": 0,
                "disk_gb": 0,
                "memory_gb_per_vcpu": 0,
            }
        }

    async def availability(self, sizes: list[SizeQuery]) -> dict[str, Availability]:
        moment = datetime.now(UTC)
        if not self.enabled:
            answer = Availability("unavailable", "local deployments only", moment)
        else:
            try:
                answered = (await self._docker("info", "--format", "{{.ServerVersion}}")).ok
            except ComputeProviderError as exc:
                answered = False
                detail = str(exc)
            else:
                detail = "Docker is not running; start OrbStack or Docker"
            answer = (
                Availability("available", "runs on this machine's Docker", moment)
                if answered
                else Availability("unknown", detail, moment)
            )
        return {size.code: answer for size in sizes}


def _parse_created(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    # Docker writes nanoseconds; datetime takes microseconds.
    trimmed = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(trimmed)
    except ValueError:
        return None


def make_localdev_provider(settings: Settings) -> LocaldevProvider:
    source_root = default_source_root()
    ports, names = loopback_targets(
        [settings.files_store_endpoint, settings.files_content_base_url]
    )
    return LocaldevProvider(
        enabled=settings.is_local,
        project=settings.compose_project_name or "alkera-local",
        source_root=source_root,
        state_dir=source_root / ".alkera-dev" / "localdev",
        forward_ports=ports,
        loopback_names=names,
    )


def _make_provider(settings: Settings) -> ComputeProvider:
    return make_localdev_provider(settings)


#: What starting a local box takes (seconds): no host to find, the image built.
_COMPUTE_CLASSES = ("cpu", "gpu")
for _compute_class in _COMPUTE_CLASSES:
    register_timings(
        LOCALDEV, _compute_class, ProviderTimings(reserving=5, booting=10, installing=60)
    )

#: A docker volume on the developer's disk: nothing to bill, no resize.
_LOCALDEV_DISKS = DiskRules(
    container=DiskBounds(0, 0, 0),
    volume=DiskBounds(min_gb=1, max_gb=1000, default_gb=50),
    volume_billed_while_stopped=False,
    grow=GrowRule.NEVER,
)
DISK_RULES: DiskRulesTable = {(LOCALDEV, c): _LOCALDEV_DISKS for c in _COMPUTE_CLASSES}

register_provider(
    LOCALDEV,
    _make_provider,
    traits=ProviderTraits(
        console_name="The local box",
        unconfigured_reason="Local developer boxes run only in a local deployment (APP_ENV=local)",
        data_disk=(2048, "docker-volume"),
    ),
)


__all__ = [
    "AGENT_BUILD_SCRIPT",
    "IMAGE_CONTEXT",
    "IMAGE_REPOSITORY",
    "LOCALDEV_MAX_BOXES",
    "LOCALDEV_TYPE_CODE",
    "AgentBuilder",
    "DockerResult",
    "DockerRunner",
    "LocaldevProvider",
    "default_source_root",
    "image_tag",
    "loopback_targets",
    "make_localdev_provider",
    "run_agent_build",
    "run_docker",
]
