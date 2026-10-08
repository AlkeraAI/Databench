"""The supervisor's loop: claim, route, start and stop one worker per org.

Every tick it reads the routing (ids and states only: which chat of which org
this box serves), makes sure each org with an open chat has a worker, tells
each worker its chats, and lets a worker whose org has no chat left go after a
grace period. It beats for the machine with the counts the workers report,
and removes an org's data once the org has left the box. It never reads a
chat, a file or a store. The machine credential is its own, for the claim, the
beats and minting each worker a credential bound to its org, the only one a
worker gets (on its socketpair).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import signal
import socket
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from alkera_core import host_isolation, host_resources
from alkera_core.compute import box_logs
from alkera_core.compute.box_isolation import IsolationProfile, IsolationReport
from alkera_core.compute.box_layout import ORGS_ROOT
from alkera_core.compute.disk import ENV_MACHINE_TENANCY, org_slot_bytes, orgs_on_box
from alkera_core.compute.liveness import (
    drain_ceiling_seconds,
    unit_stop_timeout_seconds,
)
from alkera_core.compute.worker_faults import WorkerFault
from alkera_core.process import spawn_async

from alkera_cli.box_capabilities import SUPERVISOR_CAPABILITIES
from alkera_cli.box_status import STATUS_SCHEMA, BoxStatus, status_file_from_env, write_status
from alkera_cli.host.backoff import doubled
from alkera_cli.host.launcher import running_launcher, worker_argv
from alkera_cli.host.paths import ALKERA_HOME
from alkera_cli.host.version_info import build_id
from alkera_cli.org_root import ensure_org_root
from alkera_cli.org_slot import open_org_links, prepare_org_links
from alkera_cli.org_worker_channel import worker_spec
from alkera_cli.org_worker_protocol import (
    Credential,
    Hello,
    ProtocolError,
    Ready,
    Refused,
    Route,
    Status,
    Stop,
    decode_to_supervisor,
    encode,
    read_line,
)
from alkera_cli.supervisor import org_events, org_reap, org_resume, org_units
from alkera_cli.supervisor.crash_loop import CrashLoop
from alkera_cli.supervisor.http import ApiError
from alkera_cli.supervisor.launch import (
    ORG_CONTROLLERS,
    ORGS_CGROUP,
    ORGS_SLICE,
    LaunchPlan,
    WorkerLaunch,
    memory_steps,
    network_steps,
    plan_launch,
    worker_env,
)
from alkera_cli.supervisor.machine_api import MACHINE_CREDENTIAL_HEADER, MachineApi
from alkera_cli.supervisor.org_capacity import (
    DEFAULT_WORKER_MEMORY_MB,
    WORKER_MEMORY_SHARE,
    org_memory_share,
    worker_capacity,
)
from alkera_cli.supervisor.org_credentials import CredentialHandoff, hand_out
from alkera_cli.supervisor.org_events import emit, emit_for
from alkera_cli.supervisor.org_faults import Fault, FaultBook, WorkerStartError, describe
from alkera_cli.supervisor.org_resume import drain_window, restart_window, stop_is_final
from alkera_cli.supervisor.org_rootfs import RootfsError, ensure_org_rootfs, org_rootfs_path
from alkera_cli.supervisor.org_routing import (
    FileRoutingFeed,
    RouteEntry,
    RoutingFeed,
    parse_routing,
)
from alkera_cli.supervisor.org_worker_log import OrgLog, copy_output, org_log_path
from alkera_cli.supervisor.quota import mountpoint_of, quota_steps
from alkera_cli.supervisor.slots import ORG_UID_BASE, Slot, SlotError, SlotTable

logger = logging.getLogger(__name__)

#: The deployment settings a worker needs from the supervisor's environment.
#: Never the machine credential: that travels on the socketpair.
WORKER_ENV_NAMES: Final = (
    "ALKERA_API_URL",
    "ALKERA_GATEWAY_URL",
    "ALKERA_MACHINE_NAME",
    "ALKERA_MACHINE_PROVIDER",
    "ALKERA_MACHINE_PROVIDER_POD_ID",
    "ALKERA_MACHINE_TYPE_CODE",
    ENV_MACHINE_TENANCY,
    "ALKERA_RELEASE_VERSION",
    "ALKERA_SUBAGENTS_ENABLED",
    "ALKERA_CLOUD_DRAIN_CEILING_SECONDS",
    "ALKERA_CLOUD_CHAT_IDLE_MINUTES",
    "ALKERA_CLOUD_MEMORY_PRESSURE_PERCENT",
    "ALKERA_CLOUD_MAX_MIRRORS",
    "ALKERA_SANDBOX_MODE",
    "ALKERA_SANDBOX_HOME",
    "ALKERA_SANDBOX_DEDICATED",
    "ALKERA_SANDBOX_ROOTFS",
    "ALKERA_SANDBOX_PYTHON",
    "ALKERA_SANDBOX_NET",
    "ALKERA_SANDBOX_TOPOLOGY",
    "SANDBOX_POOL_VCPU",
    "SANDBOX_POOL_MEMORY_MB",
    "SANDBOX_DEDICATED_VCPU",
    "SANDBOX_DEDICATED_MEMORY_MB",
    "ALKERA_OPENCODE_BIN",
    "ALKERA_RIPGREP_BIN",
    "ALKERA_LOCALDEV_FORWARD_PORTS",
    "LANG",
    "LC_ALL",
)
DEFAULT_ORGS_ROOT: Final = Path(ORGS_ROOT)
#: Where the box stages the rootfs (``sandbox-prereqs.sh``; the harness's own
#: default, which the supervisor does not import).
DEFAULT_ROOTFS: Final = "/opt/alkera/rootfs/current"
DEFAULT_SLOT_TABLE: Final = Path("/var/lib/alkera-supervisor/slots.json")


Frame = Hello | Credential | Route | Stop


@dataclass(slots=True)
class Worker:
    """One org's running worker, as the supervisor sees it: ids and counts,
    and the two things it does with it."""

    slot: Slot
    alive: Callable[[], bool]
    send: Callable[[Frame], Awaitable[None]]
    started_at: float = 0.0
    #: Its credential: when it is next replaced, and a hand-off not yet taken.
    credential: CredentialHandoff = field(default_factory=CredentialHandoff)
    routed: tuple[str, ...] = ()
    chats_served: int = 0
    chats_busy: int = 0
    #: ``None`` until its status counts what a drain would wait for.
    chats_working: int | None = None
    rss_bytes: int = 0
    idle_since: float | None = None
    stopping: bool = False
    #: Ends the worker at once: only for one that outlived its drain window.
    kill: Callable[[], Awaitable[None]] | None = None
    #: How it was started: where its memory limit is set.
    launch: WorkerLaunch | None = None


#: How long a worker that keeps exiting waits before it is started again.
RESTART_BACKOFF_MIN: Final = 2.0
RESTART_BACKOFF_MAX: Final = 120.0


def probe_isolation(host: host_isolation.Host | None = None) -> IsolationReport:
    """What this box can do to keep orgs apart: every mechanism a worker's
    launch uses, tried the way the launch uses it, once before the claim."""
    return host_isolation.probe(
        host, cgroup=ORGS_CGROUP, controllers=ORG_CONTROLLERS, uid_base=ORG_UID_BASE
    )


def _box_log_dir(env: Mapping[str, str]) -> Path:
    """The box's log directory: ``logs`` under its Alkera home."""
    return Path(env.get("ALKERA_HOME", "").strip() or ALKERA_HOME).expanduser() / "logs"


def _alkera_argv() -> list[str]:
    """The command that starts this same build: its absolute path, never a bare
    name a spawned process would look up on a ``PATH`` the install never set."""
    return [str(running_launcher())]


class Supervisor:
    def __init__(
        self,
        *,
        api: MachineApi,
        feed: RoutingFeed,
        slots: SlotTable,
        orgs_root: Path = DEFAULT_ORGS_ROOT,
        env: Mapping[str, str] | None = None,
        tick_seconds: float = 3.0,
        beat_seconds: float = 20.0,
        idle_grace_seconds: float = 300.0,
        isolation: IsolationReport,
        start_worker: Callable[[str], Awaitable[Worker]] | None = None,
        log_dir: Path | None = None,
        resume_path: Path | None = None,
        status_path: Path | None = None,
    ) -> None:
        self._api = api
        self._feed = feed
        self._slots = slots
        self._orgs_root = orgs_root
        self._env = dict(os.environ if env is None else env)
        self._tick = tick_seconds
        self._beat = beat_seconds
        self._grace = idle_grace_seconds
        self._workers: dict[str, Worker] = {}
        self._machine_id: str | None = None
        self._instance_id = secrets.token_hex(8)
        self._stopping = asyncio.Event()
        self._start = start_worker or self._start_worker
        #: What the box can do to keep orgs apart (``alkera_core.host_isolation``):
        #: how every worker is launched, and whether a second org may come.
        self._isolation = isolation
        self._systemd = isolation.units
        #: Per org: when it may be started again, and the delay it last waited.
        self._backoff: dict[str, tuple[float, float]] = {}
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._listeners: dict[str, asyncio.Task[None]] = {}
        #: Set once the supervisor is stopping: whether the stop is final.
        self._final: bool | None = None
        #: Where each org worker's output is kept, and the tasks copying it.
        self._log_dir = log_dir or _box_log_dir(self._env)
        self._outputs: dict[str, asyncio.Task[None]] = {}
        #: Each org's worker failures: what the beat and the alarm say of them.
        self._crashes = CrashLoop()
        #: Where a restart in place names the orgs it stopped workers for, and
        #: the orgs read from it that still need their worker started.
        self._resume_path = resume_path
        self._resume: set[str] = set()
        self._resume_until = float("inf")
        self._status_path = status_path or status_file_from_env(self._env)
        #: The host seams: systemd commands, the active org units, the removal.
        self._runner: org_units.Runner = org_units.run
        self._unit_state: Callable[[str], str] = org_units.unit_state
        self._active_slots: Callable[[], set[int]] = org_units.active_org_slots
        self._remove_org: Callable[[Path, Slot], None] = org_reap.remove_org_data
        #: Per org: why its worker last failed (``org_faults.py``).
        self._faults = FaultBook()
        self._volume_bytes: int | None = None
        #: The orgs the last routing wanted a worker for.
        self._wanted: frozenset[str] = frozenset()
        self._refusals = 0
        self._next_reap = 0.0
        #: The memory each org was last given (see :meth:`_share_memory`).
        self._shared = 0

    @property
    def workers(self) -> Mapping[str, Worker]:
        return self._workers

    @property
    def failing(self) -> frozenset[str]:
        """The orgs whose worker is failing, as the beat counts them."""
        return self._crashes.failing

    def stop(self) -> None:
        self._stopping.set()

    async def claim(self) -> str:
        self._machine_id = await self._api.claim(self.claim_body())
        logger.info("claimed machine %s", self._machine_id)
        return self._machine_id

    def claim_body(self) -> dict[str, object]:
        """The claim (``MachineClaimRequest`` on the backend; a test holds the
        two together)."""
        env = self._env
        return {
            "provider_pod_id": env.get("ALKERA_MACHINE_PROVIDER_POD_ID", ""),
            "name": env.get("ALKERA_MACHINE_NAME", "") or socket.gethostname(),
            "capacity": self._capacity(),
            "daemon_version": env.get("ALKERA_RELEASE_VERSION", "")[:32],
            "daemon_instance_id": self._instance_id,
            "sandbox": self._sandbox(),
        }

    def _sandbox(self) -> str:
        return "gvisor" if self._env.get("ALKERA_SANDBOX_MODE") == "gvisor" else "none"

    def _capacity(self) -> int:
        try:
            return max(1, min(1000, int(self._env.get("ALKERA_CLOUD_MAX_MIRRORS", "6"))))
        except ValueError:
            return 6

    def resources(self) -> dict[str, int]:
        """What the box holds now, with its workers' share: how placement
        knows how many more orgs it can send here."""
        total = host_resources.effective_memory_bytes() or 0  # the box's cgroup, not its host
        used = min((host_resources.host_memory_in_use() or (0, 0))[0], total)
        disk_used, disk_total = host_resources.disk_bytes(self._orgs_root)
        return {
            "memory_used_bytes": used,
            "memory_limit_bytes": total,
            "disk_used_bytes": disk_used,
            "disk_total_bytes": disk_total,
            "org_workers": len(self._workers),
            "org_worker_capacity": worker_capacity(self._env, memory_total_bytes=total),
            "org_worker_memory_bytes": sum(w.rss_bytes for w in self._workers.values()),
            "org_workers_failing": len(self.failing),
            # Placement sends no new org to a box with no slot left to give.
            "org_slots_free": self._slots.free(),
        }

    def heartbeat_body(self) -> dict[str, object]:
        """The beat (``MachineHeartbeatRequest`` on the backend; a test holds
        the two together)."""
        resources: dict[str, object] = dict(self.resources())
        if self.failing:
            # The orgs it serves none of: placement sends their chats elsewhere.
            resources["org_workers_failing_ids"] = sorted(self.failing)
        return {
            "capacity": self._capacity(),
            "chats_served": sum(w.chats_served for w in self._workers.values()),
            "daemon_version": self._env.get("ALKERA_RELEASE_VERSION", "")[:32] or None,
            "daemon_instance_id": self._instance_id,
            "sandbox": self._sandbox(),
            "capabilities": [*SUPERVISOR_CAPABILITIES, *self._isolation.capabilities()],
            "isolation": self._isolation.heartbeat(),
            "fault": None if (fault := self.fault()) is None else fault.heartbeat(),
            "resources": resources,
            # A stop says so on the beat, in the words the single daemon used:
            # placement sends nothing new, and a restart keeps the box's chats.
            "draining": self._final is True,
            "restarting": True if self._final is False else None,
        }

    def fault(self) -> Fault | None:
        """Why the box serves none of the orgs it was asked to, if it does not."""
        serving = {org for org, worker in self._workers.items() if worker.alive()}
        return self._faults.box_fault(
            wanted=self._wanted, serving=serving, failing=self._crashes.failing
        )

    async def beat(self) -> None:
        if self._machine_id is None:
            return
        await asyncio.to_thread(open_org_links, self._env)
        await self.follow_volume()
        body = self.heartbeat_body()
        await self._api.heartbeat(self._machine_id, body)
        write_status(self._status_path, self.status_report(body))

    def status_report(self, beat: Mapping[str, object]) -> BoxStatus:
        """The roll's status file for a beat the server took. The chat counts
        are left out while a worker has not counted them or the box is
        stopping, so the roll never reads such a box as idle."""
        workers = self._workers.values()
        report: BoxStatus = {
            "schema": STATUS_SCHEMA,
            "pid": os.getpid(),
            "daemon_instance_id": self._instance_id,
            "daemon_version": self._env.get("ALKERA_RELEASE_VERSION", "")[:32] or None,
            "build": build_id(self._env),
            "machine_id": self._machine_id,
            "beat_at": time.time(),
            "chats_held": sum(w.chats_served for w in workers),
            "draining": bool(beat["draining"]),
            "restarting": True if self._final is False else None,
        }
        working = [w.chats_working for w in workers]
        if self._final is None and None not in working:
            report["chats_busy"] = sum(w.chats_busy for w in workers)
            report["chats_working"] = sum(w for w in working if w is not None)
        return report

    def _memory_share(self, *, orgs: int) -> int:
        return org_memory_share(
            self._env, memory_total_bytes=host_resources.effective_memory_bytes() or 0, orgs=orgs
        )

    async def _share_memory(self) -> None:
        """Every running org's memory follows how many orgs share the box now."""
        share = self._memory_share(orgs=len(self._workers))
        if share == self._shared:
            return
        self._shared = share
        for worker in self._workers.values():
            if worker.launch is not None:
                for argv in memory_steps(worker.launch, share, systemd=self._systemd):
                    await self._run(argv)

    async def _run(self, argv: Sequence[str]) -> int:
        return await asyncio.to_thread(self._runner, argv)

    async def _start_worker(self, org_id: str) -> Worker:
        assert self._machine_id is not None
        if not self._isolation.admits(org_id, (held.org_id for held in self._slots.slots())):
            raise WorkerStartError(
                WorkerFault.SECOND_ORG_REFUSED, "this box cannot keep orgs apart and holds another"
            )
        credential, lives = await self._api.worker_credential(org_id)
        slot = self._slots.assign(org_id)
        root = ensure_org_root(self._orgs_root, slot)
        await self._cap_disk(slot, root.path)
        env = worker_env(
            root.path,
            slot,
            passthrough=self._env,
            names=WORKER_ENV_NAMES,
            profile=self._isolation.profile,
        )
        rootfs = await asyncio.to_thread(self._org_rootfs, slot)
        if rootfs is not None:
            env["ALKERA_SANDBOX_ROOTFS"] = str(rootfs)
        mine, theirs = socket.socketpair()
        launch = WorkerLaunch(
            slot=slot,
            org_root=root.path,
            argv=worker_argv(_alkera_argv(), root.path),
            env=env,
            stop_timeout_seconds=unit_stop_timeout_seconds(int(drain_ceiling_seconds(self._env))),
            memory_max_bytes=self._memory_share(orgs=len(self._workers) + 1),
        )
        plan = plan_launch(launch, self._isolation)
        try:
            process = await self._spawn(plan, launch, theirs)
        except BaseException:
            mine.close()
            raise
        finally:
            theirs.close()

        async def kill() -> None:
            # On systemd ``process`` is the systemd-run client: the unit is
            # what serves, and what must end.
            if plan.unit is not None:
                await self._run(("systemctl", "kill", "--signal=KILL", plan.unit))
            with contextlib.suppress(ProcessLookupError):
                process.kill()

        try:
            _reader, writer = await asyncio.open_connection(sock=mine)
            hello = Hello(
                org_id=slot.org_id,
                slot=slot.index,
                machine_id=self._machine_id,
                credential=credential,
            )
            writer.write(encode(hello))
            await writer.drain()

            async def send(frame: Frame) -> None:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    writer.write(encode(frame))
                    await writer.drain()

            worker = Worker(
                slot=slot,
                alive=lambda: process.returncode is None,
                send=send,
                started_at=self._clock(),
                kill=kill,
                launch=launch,
            )
            worker.credential.started(lives, self._clock())
            self._listeners[slot.org_id] = asyncio.create_task(self._listen(worker, _reader))
            pid = await self._worker_pid(plan, process)
            if plan.linked:
                await self._link(launch, pid)
        except BaseException:
            # Nothing of a worker that did not start whole is left serving.
            if (listener := self._listeners.pop(slot.org_id, None)) is not None:
                listener.cancel()
            await kill()
            mine.close()
            raise
        emit_for(slot, box_logs.WORKER_STARTED, pid=pid)
        return worker

    def _org_rootfs(self, slot: Slot) -> Path | None:
        """The staged rootfs as this org's namespace sees it, owned as its own
        root (``alkera_cli/supervisor/org_rootfs.py``); ``None`` on a box with none to give."""
        if self._env.get("ALKERA_SANDBOX_MODE") != "gvisor":
            return None
        source = Path(self._env.get("ALKERA_SANDBOX_ROOTFS") or DEFAULT_ROOTFS)
        target = org_rootfs_path(self._orgs_root, slot)
        try:
            how = ensure_org_rootfs(source, target, slot)
        except (RootfsError, OSError) as exc:
            logger.warning("org slot %d gets no rootfs of its own: %s", slot.index, exc)
            return None
        logger.info("org slot %d's rootfs is %s at %s", slot.index, how, target)
        return target

    async def _cap_disk(self, slot: Slot, root: Path) -> None:
        """The org's share of the volume (:mod:`alkera_cli.supervisor.quota`): the
        whole of it when the box holds only this org."""
        resources = self.resources()
        orgs = orgs_on_box(
            self._isolation.profile,
            tenancy=self._env.get(ENV_MACHINE_TENANCY),
            workers=resources["org_worker_capacity"],
        )
        limit = org_slot_bytes(resources["disk_total_bytes"], orgs=orgs, env=self._env)
        for argv in quota_steps(slot, root, limit_bytes=limit, mountpoint=mountpoint_of(root)):
            if await self._run(argv) != 0:
                logger.warning(
                    "org slot %d shares the volume uncapped: this filesystem has no project "
                    "quotas (%s refused)",
                    slot.index,
                    argv[0],
                )
                return
        logger.info("org slot %d may write %d MiB", slot.index, limit >> 20)

    async def follow_volume(self) -> None:
        """Grow the filesystem into a volume grown under the box (EC2 grows one
        while it runs), then every org's share with it."""
        volume = host_resources.volume_device(self._orgs_root)
        if volume is None or volume.size_bytes == self._volume_bytes:
            return
        self._volume_bytes = volume.size_bytes
        if await self._run(("resize2fs", volume.device)) == 0:
            for slot in self._slots.slots():
                await self._cap_disk(slot, ensure_org_root(self._orgs_root, slot).path)

    async def _spawn(
        self, plan: LaunchPlan, launch: WorkerLaunch, channel: socket.socket
    ) -> asyncio.subprocess.Process:
        """The worker's process as ``plan`` says, its socketpair end on
        standard input."""
        if plan.unit is not None:
            # A unit an earlier worker left goes first; its name is the slot's.
            await org_units.clear_unit(
                plan.unit,
                runner=self._run,
                state=lambda unit: asyncio.to_thread(self._unit_state, unit),
                sleep=self._sleep,
            )
        for step in plan.before:
            if await self._run(step) != 0:
                raise WorkerStartError(
                    WorkerFault.CGROUP_REFUSED,
                    f"could not make org {launch.slot.index}'s cgroup: {' '.join(step[:2])}",
                )
        # Its output goes to its org's log (and on through ``--pipe`` on systemd).
        env = os.environ if plan.env is None else plan.env
        process = await spawn_async(worker_spec(plan.argv, env=env, channel=channel.fileno()))
        self._capture(launch.slot, process)
        return process

    def _capture(self, slot: Slot, process: asyncio.subprocess.Process) -> None:
        """Copy the worker's output into its org's log until it ends, then
        say how it ended there."""
        log = OrgLog(org_log_path(self._log_dir, slot.org_id))
        with contextlib.suppress(OSError):
            log.note(f"org slot {slot.index}'s worker started (pid {process.pid})")

        async def copy() -> None:
            try:
                if process.stdout is not None:
                    await copy_output(process.stdout, log)
                status = await process.wait()
                with contextlib.suppress(OSError):
                    log.note(f"org slot {slot.index}'s worker exited with status {status}")
            finally:
                log.close()

        self._outputs[slot.org_id] = asyncio.get_running_loop().create_task(copy())

    async def _worker_pid(self, plan: LaunchPlan, process: asyncio.subprocess.Process) -> int:
        """The worker's own pid: the spawned process itself, or for a unit the
        unit's main process (``systemd-run`` stays the supervisor's child and
        the unit's runs under systemd)."""
        if plan.unit is None:
            return process.pid
        for _ in range(100):
            pid = await asyncio.to_thread(org_units.show_property, plan.unit, "MainPID")
            if pid.isdigit() and int(pid) > 0:
                return int(pid)
            if process.returncode is not None:
                break
            await asyncio.sleep(0.1)
        raise WorkerStartError(WorkerFault.UNIT_FAILED, f"{plan.unit} never started")

    async def _link(self, launch: WorkerLaunch, pid: int) -> None:
        """The worker's link, once its network namespace exists."""
        mine = os.stat("/proc/self/ns/net").st_ino
        for _ in range(100):
            with contextlib.suppress(OSError):
                if os.stat(f"/proc/{pid}/ns/net").st_ino != mine:
                    break
            await asyncio.sleep(0.1)
        else:
            raise WorkerStartError(WorkerFault.LINK_FAILED, "no network namespace was made")
        first, *rest = network_steps(launch, pid)
        await self._run(first)  # a leftover link; usually absent
        for argv in rest:
            if await self._run(argv) != 0:
                raise WorkerStartError(WorkerFault.LINK_FAILED, f"{argv[1:3]} refused")

    async def _listen(self, worker: Worker, reader: asyncio.StreamReader) -> None:
        slot = worker.slot
        try:
            while (line := await read_line(reader)) is not None:
                frame = decode_to_supervisor(line)
                if isinstance(frame, Status):
                    worker.chats_served = frame.chats_served
                    worker.chats_busy, worker.chats_working = frame.chats_busy, frame.chats_working
                    worker.rss_bytes = frame.rss_bytes
                    if worker.credential.reported(frame, now=self._clock()):
                        await hand_out({slot.org_id: worker}, self._api, now=self._clock())
                elif isinstance(frame, Refused):
                    self._refusals += 1
                    code = org_events.refusal_code(frame.reason)
                    level = "error" if code == "another_org" else "warning"
                    emit_for(
                        slot,
                        box_logs.ORG_ADMISSION_REFUSED,
                        level=level,
                        chat_id=frame.chat_id,
                        reason=code,
                        count=self._refusals,
                    )
                elif isinstance(frame, Ready):
                    self._faults.serving(slot.org_id)
                    self._ready(slot.org_id)
                    emit_for(slot, box_logs.WORKER_READY)
        except ProtocolError as exc:
            emit_for(slot, box_logs.WORKER_PROTOCOL_BROKEN, level="warning", error=str(exc))
            if worker.kill is not None:
                await worker.kill()

    def _ready(self, org_id: str) -> None:
        """A restarted org's worker is serving: the org leaves the list a
        crash would otherwise have to start it from."""
        if org_id in self._resume:
            self._resume.discard(org_id)
            self._remember_for_restart(self._resume)

    async def reconcile(self, entries: Sequence[RouteEntry], *, now: float) -> None:
        """Make the workers match the routing. An org has a worker while any of
        its chats should be held now; the worker is told every chat of its org
        bound to the box (it decides which to hold and when to sleep one), and
        nothing of another org. A worker whose org has no chat to hold and that
        holds none is stopped once that has lasted the grace period. A worker
        that exited is started again after a backoff that grows while it keeps
        exiting."""
        routed: dict[str, set[str]] = {}
        wanted: set[str] = set()
        for entry in entries:
            routed.setdefault(entry.org_id, set()).add(entry.chat_id)
            if entry.desired_state == "open":
                wanted.add(entry.org_id)
        # An org a restart in place stopped is started again whatever its chats
        # read: the box kept their leases and owes their work, and the routing
        # reads a chat on a box that was restarting as asleep. Once up, the
        # worker is kept or let go like any other, by what it holds.
        if now >= self._resume_until:
            self._ready_all()
        wanted |= self._resume
        self._wanted = frozenset(wanted)
        self._reap(now)
        # Placement moves a failing org's chats off the box; once none is
        # routed here, the box stops naming the org and may be given it again.
        for org_id in self.failing - routed.keys() - self._workers.keys():
            self._crashes.stopped(org_id)
        capacity = self.resources()["org_worker_capacity"]
        for org_id in sorted(wanted - self._workers.keys()):
            if now < self._backoff.get(org_id, (0.0, 0.0))[0]:
                continue
            if len(self._workers) >= capacity:
                # Placement sends no new org past the budget; one routed here
                # anyway (a report it had not read yet) waits for a slot.
                logger.warning("the worker budget (%d) is spent; an org waits", capacity)
                break
            try:
                self._workers[org_id] = await self._start(org_id)
            except Exception as exc:
                fault = describe(exc)
                if isinstance(exc, SlotError):
                    emit(box_logs.SLOTS_EXHAUSTED, level="error", org_id=org_id, error=str(exc))
                else:
                    logger.exception("could not start the worker for a routed org")
                emit(
                    box_logs.WORKER_START_FAILED,
                    level="warning",
                    org_id=org_id,
                    reason=fault.code.value,
                    error=fault.summary,
                )
                self._faults.failed(org_id, fault)
                delay = self._wait_longer(org_id, now=now, ran=None)
                self._crashed(org_id, now=now, backoff=delay)
        await self._share_memory()
        wall = time.time()
        for org_id in routed:
            if (held := self._slots.find(org_id)) is not None:
                self._slots.touch(held, wall)
        for org_id, worker in self._workers.items():
            self._crashes.up(org_id, ran=now - worker.started_at)
            if worker.stopping:
                continue
            chats = tuple(sorted(routed.get(org_id, ())))
            if chats != worker.routed:
                worker.routed = chats
                await worker.send(Route(chat_ids=chats))
            if org_id in wanted or worker.chats_served:
                worker.idle_since = None
                continue
            worker.idle_since = worker.idle_since if worker.idle_since is not None else now
            if now - worker.idle_since >= self._grace:
                worker.stopping = True
                emit_for(worker.slot, box_logs.WORKER_STOPPING, final=True)
                await worker.send(Stop())
        await self._refresh_credentials(now)
        if now >= self._next_reap:
            self._next_reap = now + org_reap.REAP_EVERY_SECONDS
            await self._reap_idle_orgs(set(routed) | wanted, wall)

    async def _reap_idle_orgs(self, present: set[str], wall: float) -> None:
        if self._isolation.profile != IsolationProfile.ORG_NAMESPACES:
            # The box is its one org's for its life: nothing is reaped to make
            # room for another.
            return
        for org_id in await org_reap.reap_idle_orgs(
            self._slots,
            busy=present | self._workers.keys() | self._resume,
            wall=wall,
            removable=lambda slot: (
                not self._systemd
                or self._unit_state(f"alkera-org-{slot.index}.service") in org_units.GONE_STATES
            ),
            remove=lambda slot: self._remove_org(self._orgs_root, slot),
        ):
            self._backoff.pop(org_id, None)
            self._crashes.forget(org_id)

    def _reap(self, now: float) -> None:
        for org_id, gone in list(self._workers.items()):
            if gone.alive():
                continue
            del self._workers[org_id]
            if gone.stopping:
                self._backoff.pop(org_id, None)
                self._crashes.stopped(org_id)
                self._faults.serving(org_id)
                continue
            ran = now - gone.started_at
            self._faults.failed(
                org_id, Fault(WorkerFault.EXITED, f"the worker exited after {ran:.0f} s")
            )
            delay = self._wait_longer(org_id, now=now, ran=ran)
            emit_for(gone.slot, box_logs.WORKER_EXITED, level="warning", backoff=delay, ran=ran)
            self._crashed(org_id, now=now, backoff=delay, ran=ran)

    def _wait_longer(self, org_id: str, *, now: float, ran: float | None) -> float:
        """The wait before the org's worker is started again: twice the last,
        or the floor again after a run that settled."""
        _, last = self._backoff.get(org_id, (0.0, 0.0))
        if ran is not None and self._crashes.settled(ran):
            last = 0.0
        delay = doubled(last, floor=RESTART_BACKOFF_MIN, cap=RESTART_BACKOFF_MAX)
        self._backoff[org_id] = (now + delay, delay)
        return delay

    def _crashed(
        self, org_id: str, *, now: float, backoff: float, ran: float | None = None
    ) -> None:
        """An org's worker exited unasked after ``ran`` seconds, or could not
        start: the one rule (:mod:`alkera_cli.supervisor.crash_loop`) says
        whether the beat now counts it failing and whether ops are alarmed."""
        failure = self._crashes.failed(org_id, now=now, ran=ran)
        if failure.began_failing:
            logger.error(
                "the worker of an org is failing (%d failures in a row); its output is in %s",
                failure.streak,
                org_log_path(self._log_dir, org_id),
            )
        if failure.looping is not None:
            held = self._slots.find(org_id)
            last = self._faults.last(org_id)
            emit(
                box_logs.WORKER_CRASH_LOOP,
                level="error",
                slot=None if held is None else held.index,
                org_id=org_id,
                restarts=failure.looping,
                window=self._crashes.window,
                backoff=backoff,
                reason=(last.code if last is not None else WorkerFault.OTHER).value,
            )

    async def _refresh_credentials(self, now: float, *, during_drain: bool = False) -> None:
        """A draining worker is handed one too: it pushes folders on it."""
        await hand_out(self._workers, self._api, now=now, drain=during_drain)

    async def run(self) -> int:
        # Claim and beat first: a box that waited out its earlier workers
        # before it beat read as unreachable, and every chat on it moved.
        await self.claim()
        self._resume = self._take_resume()
        if self._systemd:
            # A supervisor that crashed named nothing: the orgs whose units
            # are still up were serving, and their chats' leases are kept.
            held = {s.index: s.org_id for s in self._slots.slots()}
            self._resume |= {held[i] for i in self._active_slots() if i in held}
        self._remember_for_restart(self._resume)
        if self._systemd:
            await self._outlast_earlier_workers()
        self._resume_until = self._clock() + org_resume.RESUME_HOLD_SECONDS
        last_beat = -self._beat
        while not self._stopping.is_set():
            now = self._clock()
            try:
                await self.reconcile(await self._feed.read(), now=now)
            except (ApiError, OSError, ValueError) as exc:
                logger.warning("could not read the routing: %s", exc)
            if now - last_beat >= self._beat:
                try:
                    await self.beat()
                    last_beat = now
                except (ApiError, OSError) as exc:
                    logger.warning("machine heartbeat failed: %s", exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._tick)
        await self.drain(final=stop_is_final(self._env))
        return 0

    async def drain(self, *, final: bool) -> None:
        """Stop every worker the way the single daemon stopped: a final stop
        hands each chat back within the drain ceiling, a restart keeps them.
        The supervisor beats through it (the beat says which, so placement
        sends nothing new) and keeps the workers' credentials fresh, since a
        drain may run for hours. A worker still running past
        :func:`drain_window` (a restart's :func:`restart_window`) is killed;
        nothing before that is cut."""
        self._final = final
        self._remember_for_restart(set() if final else set(self._workers) | self._resume)
        for worker in self._workers.values():
            worker.stopping = True
            emit_for(worker.slot, box_logs.WORKER_STOPPING, final=final)
            await worker.send(Stop(final=final))
        window = drain_window(self._env) if final else restart_window(self._env)
        deadline = self._clock() + window
        last_beat = -self._beat
        while True:
            now = self._clock()
            alive = [w for w in self._workers.values() if w.alive()]
            if not alive:
                break
            if now >= deadline:
                for worker in alive:
                    emit_for(worker.slot, box_logs.WORKER_KILLED, level="warning")
                    if worker.kill is not None:
                        await worker.kill()
                break
            if now - last_beat >= self._beat:
                with contextlib.suppress(ApiError, OSError):
                    await self.beat()
                    last_beat = now
            await self._refresh_credentials(now, during_drain=True)
            await self._sleep(min(self._tick, max(0.0, deadline - now)))
        await asyncio.gather(*self._listeners.values(), return_exceptions=True)
        await asyncio.gather(*self._outputs.values(), return_exceptions=True)

    def _remember_for_restart(self, orgs: set[str]) -> None:
        if self._resume_path is not None:
            org_resume.remember(self._resume_path, orgs)

    def _take_resume(self) -> set[str]:
        return set() if self._resume_path is None else org_resume.read(self._resume_path)

    def _ready_all(self) -> None:
        """Past the hold, the routing alone says which orgs need a worker."""
        self._resume_until = float("inf")
        self._resume.clear()
        self._remember_for_restart(set())

    async def _outlast_earlier_workers(self) -> None:
        """Workers an earlier supervisor left (it died, or was killed past its
        stop timeout) drain as a restart once their channel closes. Let them
        end on their own within a restart's window, beating as restarting so
        the box keeps its chats, then kill what is left: a stop would make
        each hand its chats back over the whole drain ceiling."""
        self._final = False
        started = self._clock()
        deadline, last_beat, left = started + restart_window(self._env), -self._beat, True
        while self._clock() < deadline and (
            left := bool(await asyncio.to_thread(self._active_slots))
        ):
            if (now := self._clock()) - last_beat >= self._beat:
                with contextlib.suppress(ApiError, OSError):
                    await self.beat()
                    last_beat = now
            await self._sleep(1.0)
        if left:
            await self._run(("systemctl", "kill", "--signal=KILL", ORGS_SLICE))
        self._final = None
        emit(box_logs.EARLIER_WORKERS_OUTLASTED, waited=self._clock() - started, killed=left)


async def run_supervisor(*, routing_file: Path | None) -> int:
    """The supervisor as a box runs it, from the box's environment."""
    env = os.environ
    api_url = env.get("ALKERA_API_URL", "").strip()
    credential = env.get("ALKERA_MACHINE_CREDENTIAL", "").strip()
    if not api_url or not credential:
        logger.error("the supervisor needs ALKERA_API_URL and ALKERA_MACHINE_CREDENTIAL")
        return 2
    await asyncio.to_thread(prepare_org_links, env)
    api = MachineApi(api_url=api_url, credential=credential)
    feed: RoutingFeed = FileRoutingFeed(routing_file) if routing_file is not None else api
    slot_table = Path(env.get("ALKERA_SUPERVISOR_SLOTS", str(DEFAULT_SLOT_TABLE)))
    isolation = await asyncio.to_thread(probe_isolation)
    emit(box_logs.ISOLATION_PROBED, profile=isolation.profile.value)
    for mechanism, why in sorted(isolation.missing.items()):
        logger.info("org isolation: no %s (%s)", mechanism.value, why)
    supervisor = Supervisor(
        api=api,
        feed=feed,
        isolation=isolation,
        slots=SlotTable(slot_table),
        resume_path=slot_table.parent / org_resume.RESUME_FILE,
        orgs_root=Path(env.get("ALKERA_ORGS_ROOT", str(DEFAULT_ORGS_ROOT))),
    )
    loop = asyncio.get_running_loop()
    for number in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(number, supervisor.stop)
    return await supervisor.run()


__all__ = [
    "DEFAULT_ORGS_ROOT",
    "DEFAULT_SLOT_TABLE",
    "DEFAULT_WORKER_MEMORY_MB",
    "MACHINE_CREDENTIAL_HEADER",
    "WORKER_ENV_NAMES",
    "WORKER_MEMORY_SHARE",
    "FileRoutingFeed",
    "MachineApi",
    "RouteEntry",
    "RoutingFeed",
    "Supervisor",
    "Worker",
    "drain_window",
    "parse_routing",
    "run_supervisor",
    "stop_is_final",
    "worker_capacity",
]
